"""AI 网关的单元测试。

分两层：上半部分是不依赖数据库的纯函数（密钥派生、正文截断、SSE 旁路解析），
下半部分用 SQLite 内存库跑管理服务与路由解析——转发路径本身要连真实上游，
不在这里覆盖。
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.entities import AiApiKey, AiCallLog, AiChannel, AiModelRoute
from app.models.schemas import (
    AiApiKeyUpdate,
    AiChannelCreate,
    AiChannelUpdate,
    AiModelRouteCreate,
)
from app.services import ai_gateway_proxy as proxy
from app.services.ai_gateway import (
    DISPLAY_PREFIX_LENGTH,
    KEY_PREFIX,
    TRUNCATE_MARK,
    AiGatewayService,
    display_prefix,
    extract_bearer_key,
    find_api_key,
    generate_api_key,
    hash_api_key,
    list_public_models,
    resolve_candidates,
    truncate_text,
    write_call_log,
)
from app.services.ai_gateway_proxy import (
    CallRecord,
    StreamAccumulator,
    apply_usage,
    extract_chat_content,
    extract_usage,
)

# ---- 密钥 -------------------------------------------------------------------


def test_generate_api_key_is_unique_and_hashed():
    first_plain, first_hash, first_prefix = generate_api_key()
    second_plain, second_hash, _ = generate_api_key()

    assert first_plain.startswith(KEY_PREFIX)
    assert first_plain != second_plain
    assert first_hash != second_hash
    # 摘要必须能由明文重新算出来，否则代理侧永远查不到这把钥匙。
    assert first_hash == hash_api_key(first_plain)
    assert first_prefix == display_prefix(first_plain)
    assert len(first_prefix) == DISPLAY_PREFIX_LENGTH


def test_hash_api_key_ignores_surrounding_space():
    # 调用方在 header 里多敲一个空格是常事，不该因此判成无效密钥。
    plain = "sk-yv-abcdef"
    assert hash_api_key(plain) == hash_api_key(f"  {plain}\n")


def test_extract_bearer_key():
    assert extract_bearer_key("Bearer sk-yv-abc") == "sk-yv-abc"
    assert extract_bearer_key("bearer sk-yv-abc") == "sk-yv-abc"
    assert extract_bearer_key("sk-yv-abc") is None
    assert extract_bearer_key("Basic dXNlcjpwYXNz") is None
    assert extract_bearer_key("Bearer   ") is None
    assert extract_bearer_key(None) is None


# ---- 正文截断 ---------------------------------------------------------------


def test_truncate_text():
    assert truncate_text(None) is None
    assert truncate_text("短文本") == "短文本"

    long_text = "x" * 20000
    truncated = truncate_text(long_text, max_chars=100)
    assert truncated.startswith("x" * 100)
    assert truncated.endswith(TRUNCATE_MARK)
    assert len(truncated) == 100 + len(TRUNCATE_MARK)

    # 上限为 0 表示「不截断」，而不是「全部丢掉」。
    assert truncate_text(long_text, max_chars=0) == long_text


# ---- 响应解析 ---------------------------------------------------------------


def test_extract_chat_content_from_plain_string():
    payload = {"choices": [{"message": {"role": "assistant", "content": "你好"}}]}
    assert extract_chat_content(payload) == "你好"


def test_extract_chat_content_joins_multimodal_parts():
    payload = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "前半"}, {"type": "text", "text": "后半"}]}}
        ]
    }
    assert extract_chat_content(payload) == "前半后半"


def test_extract_chat_content_survives_malformed_payload():
    assert extract_chat_content(None) == ""
    assert extract_chat_content({"choices": []}) == ""
    assert extract_chat_content({"choices": [{}]}) == ""


def test_extract_usage_and_fallback_sum():
    record = CallRecord(request_id="r1", endpoint="chat/completions", key=None)
    apply_usage(record, extract_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}))
    # 部分厂商只给分项不给总数，这里必须自己加上。
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (3, 4, 7)

    apply_usage(record, None)
    assert record.total_tokens == 7


def test_failover_status_excludes_client_errors():
    # 400 换谁都一样，重试只是拖长失败时间；限流与 5xx 才值得换通道。
    assert 400 not in proxy.FAILOVER_STATUS
    assert 422 not in proxy.FAILOVER_STATUS
    assert {401, 403, 404, 429, 500, 502, 503, 504} <= proxy.FAILOVER_STATUS


# ---- SSE 旁路解析 -----------------------------------------------------------


def test_stream_accumulator_handles_split_chunks():
    accumulator = StreamAccumulator()
    # 刻意在 JSON 中间切断：TCP 分包不会照顾行边界。
    chunks = [
        b'data: {"choices":[{"delta":{"con',
        b'tent":"\xe4\xbd\xa0"}}]}\n\ndata: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}',
        b']}\n',
        b'data: {"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n',
        b"data: [DONE]\n\n",
    ]
    for chunk in chunks:
        accumulator.feed(chunk)

    assert accumulator.text == "你好"
    assert accumulator.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def test_stream_accumulator_ignores_noise():
    accumulator = StreamAccumulator()
    accumulator.feed(b": keep-alive\n\nevent: ping\ndata: not-json\n\n")
    assert accumulator.text == ""
    assert accumulator.usage is None


# ---- 服务层（SQLite 内存库） ------------------------------------------------


@pytest.fixture()
def db():
    # StaticPool + 单连接：内存库默认每个连接各有一份数据，测试会看不到自己写的行。
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _channel(db, name="deepseek", **kwargs) -> AiChannel:
    service = AiGatewayService(db)
    return service.create_channel(
        AiChannelCreate(name=name, base_url="https://api.example.com/v1", api_key="upstream-key", **kwargs)
    )


def test_channel_api_key_never_leaks_and_masked_update_keeps_it(db):
    service = AiGatewayService(db)
    channel = _channel(db)

    item = service.channel_item(channel)
    assert item.api_key.startswith("sk-****")
    assert "upstream-key" not in item.api_key

    # 只改备注时把掩码原样提交回来，不能把能用的 key 抹掉。
    service.update_channel(channel.id, AiChannelUpdate(remark="改了个备注", api_key=item.api_key))
    assert service.get_channel(channel.id).last_test_ok is False
    service.record_channel_test(channel, ok=True, message="ok")

    service.update_channel(channel.id, AiChannelUpdate(api_key=item.api_key))
    assert service.get_channel(channel.id).last_test_ok is True, "回传掩码不该作废已有凭据"

    service.update_channel(channel.id, AiChannelUpdate(api_key="brand-new-key"))
    assert service.get_channel(channel.id).last_test_ok is False, "换凭据后旧测试结论必须失效"


def test_route_requires_an_existing_channel(db):
    service = AiGatewayService(db)
    with pytest.raises(LookupError):
        service.create_route(AiModelRouteCreate(model_name="gpt-4o", channel_id=9999))


def test_resolve_candidates_orders_by_priority_and_skips_disabled(db):
    service = AiGatewayService(db)
    primary = _channel(db, name="primary")
    backup = _channel(db, name="backup")
    stopped = _channel(db, name="stopped")

    service.create_route(AiModelRouteCreate(model_name="gpt-4o", channel_id=backup.id, priority=200))
    service.create_route(AiModelRouteCreate(model_name="gpt-4o", channel_id=primary.id, priority=100))
    service.create_route(
        AiModelRouteCreate(model_name="gpt-4o", channel_id=stopped.id, priority=50, enabled=False)
    )

    candidates = resolve_candidates(db, "gpt-4o")
    # 停用的那条优先级最高（50），但绝不能出现在候选里。
    assert [(c.channel.name, c.upstream_model) for c in candidates] == [
        ("primary", "gpt-4o"),
        ("backup", "gpt-4o"),
    ]

    # 通道停用后它名下的路由立刻不可用，即使路由本身还开着。
    service.update_channel(primary.id, AiChannelUpdate(enabled=False))
    assert [c.channel.name for c in resolve_candidates(db, "gpt-4o")] == ["backup"]
    assert list_public_models(db) == ["gpt-4o"]


def test_route_maps_public_name_to_upstream_name(db):
    service = AiGatewayService(db)
    channel = _channel(db, name="ark")
    service.create_route(
        AiModelRouteCreate(
            model_name="gpt-4o", channel_id=channel.id, upstream_model="doubao-pro-32k"
        )
    )
    candidate = resolve_candidates(db, "gpt-4o")[0]
    assert candidate.upstream_model == "doubao-pro-32k"
    # 上游密钥要能取出来（转发时换掉调用方那把网关钥匙），且只从密文解出。
    assert candidate.upstream_key == "upstream-key"


def test_key_lifecycle(db):
    service = AiGatewayService(db)
    row, plain = service.create_key("客服系统", "给客服用")

    assert plain.startswith(KEY_PREFIX)
    # 列表里只有前缀 + 掩码。
    assert service.key_item(row).key_prefix.endswith("****")
    assert plain not in service.key_item(row).key_prefix

    assert find_api_key(db, plain).id == row.id
    assert find_api_key(db, "sk-yv-not-a-real-key") is None

    service.update_key(row.id, AiApiKeyUpdate(enabled=False))
    assert service.get_key(row.id).enabled is False

    service.delete_key(row.id)
    assert find_api_key(db, plain) is None


def test_deleting_a_channel_removes_its_routes(db):
    service = AiGatewayService(db)
    channel = _channel(db)
    service.create_route(AiModelRouteCreate(model_name="gpt-4o", channel_id=channel.id))

    service.delete_channel(channel.id)
    # 死路由必须一起清掉，否则配置看着在、调用却报「模型未配置」。
    assert service.list_routes() == []


def test_logs_filters_stats_and_purge(db):
    service = AiGatewayService(db)
    key, _ = service.create_key("测试密钥", None)

    def log(**kwargs) -> AiCallLog:
        row = AiCallLog(
            request_id=kwargs.pop("request_id"),
            key_id=key.id,
            key_name=key.name,
            endpoint="chat/completions",
            **kwargs,
        )
        write_call_log(db, row)
        return row

    log(request_id="a", model="gpt-4o", channel_id=1, channel_name="primary", success=True,
        status_code=200, prompt_tokens=10, completion_tokens=5, total_tokens=15, latency_ms=800,
        request_body=json.dumps({"model": "gpt-4o"}), response_body="回答")
    log(request_id="b", model="gpt-4o", channel_id=2, channel_name="backup", success=False,
        status_code=502, latency_ms=120, error="上游不可达")
    log(request_id="c", model="deepseek-chat", channel_id=1, channel_name="primary", success=True,
        status_code=200, total_tokens=7, latency_ms=300)

    items, total = service.list_logs(page=1, page_size=10)
    assert total == 3
    # 列表按 id 倒序，最新的一条在最前面；大字段不在列表里。
    assert [item.request_id for item in items] == ["c", "b", "a"]
    assert not hasattr(items[0], "request_body")

    _, total = service.list_logs(success=False)
    assert total == 1
    _, total = service.list_logs(model="gpt-4o")
    assert total == 2
    _, total = service.list_logs(keyword="上游不可达")
    assert total == 1
    _, total = service.list_logs(channel_id=1)
    assert total == 2

    detail = service.get_log(items[0].id)
    assert detail.request_id == "c"

    stats = service.stats(days=7)
    assert stats.totals.calls == 3
    assert stats.totals.failed == 1
    assert stats.totals.total_tokens == 22
    assert {bucket.name for bucket in stats.by_channel} == {"primary", "backup"}
    assert {bucket.name for bucket in stats.by_model} == {"gpt-4o", "deepseek-chat"}
    assert stats.by_key[0].name == "测试密钥"
    assert stats.daily[0].calls == 3

    assert service.purge_logs(before_days=1) == 0
    assert service.list_logs()[1] == 3


def test_write_call_log_swallows_db_errors(db, monkeypatch):
    """留痕失败不能让调用方的请求变成 5xx。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db, "commit", boom)
    write_call_log(db, AiCallLog(request_id="x", endpoint="chat/completions"))


def test_overview_counts(db):
    service = AiGatewayService(db)
    channel = _channel(db)
    service.create_route(AiModelRouteCreate(model_name="gpt-4o", channel_id=channel.id))
    service.create_key("k1", None)

    overview = service.overview()
    assert (overview.channel_count, overview.channel_enabled) == (1, 1)
    assert (overview.route_count, overview.route_enabled) == (1, 1)
    assert (overview.key_count, overview.key_enabled) == (1, 1)
    assert overview.log_count == 0
    assert overview.base_path == "/v1"


def test_call_record_truncates_payloads(db, monkeypatch):
    """入库前必须截断：一次调用塞进几 MB 正文会把日志表撑爆。"""
    record = CallRecord(
        request_id="r",
        endpoint="chat/completions",
        key=None,
        model="gpt-4o",
        request_body="x" * 50000,
        response_body="y" * 50000,
    )
    entity = record.to_entity()
    assert entity.request_body.endswith(TRUNCATE_MARK)
    assert len(entity.request_body) < 50000


def test_stream_accumulator_feeds_record(db):
    record = CallRecord(request_id="r", endpoint="chat/completions", key=None, stream=True)
    accumulator = StreamAccumulator()
    accumulator.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    apply_usage(record, accumulator.usage)
    record.response_body = accumulator.text
    assert record.response_body == "hi"
