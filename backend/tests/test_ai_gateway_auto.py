"""Auto 模型的判定与候选解析。

上半部分是纯函数（请求特征、标签词表、回答解析），下半部分用 SQLite 内存库跑
候选池的筛选与排序——分类那一次出站请求在这里一律用假的 classifier 注入，真实
的端到端转发在 ``tests/test_ai_gateway_proxy.py``。
"""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.schemas import (
    AiChannelCreate,
    AiChannelUpdate,
    AiModelRouteCreate,
    AiModelRouteUpdate,
)
from app.services.ai_gateway import service as gateway
from app.services.ai_gateway import auto
from app.services.ai_gateway.auto import (
    RouteCandidate,
    classify_messages,
    is_auto_model,
    parse_task_kind,
    required_tags,
    resolve_auto,
    task_prompt_text,
)
from app.services.ai_gateway.service import AiGatewayService, list_public_models

wait = asyncio.run

# ---- 保留名与标签的定义 -----------------------------------------------------


def test_auto_name_is_matched_leniently():
    assert is_auto_model("auto")
    assert is_auto_model(" Auto ")
    assert not is_auto_model("gpt-4o")
    assert not is_auto_model("automatic")
    assert not is_auto_model(None)


def test_auto_definition_needs_at_least_one_tag():
    with pytest.raises(ValueError):
        AiGatewayService._auto_tags(True, None)
    with pytest.raises(ValueError):
        AiGatewayService._auto_tags(True, ["  "])
    # 不开 Auto 就没有这份定义，留着的标签也不会被读到。
    assert AiGatewayService._auto_tags(False, ["chat", "code"]) is None


def test_auto_tags_are_normalized_and_whitelisted():
    assert AiGatewayService._auto_tags(True, [" Chat ", "chat", "code"]) == ["chat", "code"]

    with pytest.raises(ValidationError):
        AiModelRouteCreate(model_name="m", channel_id=1, auto_tags=["viison"])


# ---- 请求特征 → 硬性要求 ----------------------------------------------------


def test_required_tags_for_plain_chat():
    body = {"messages": [{"role": "user", "content": "你好"}]}
    assert required_tags(gateway.CHAT_ENDPOINT, body) == ["chat"]


def test_required_tags_see_images_and_tools():
    with_image = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是什么"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                ],
            }
        ]
    }
    assert required_tags(gateway.CHAT_ENDPOINT, with_image) == ["chat", "vision"]

    with_tools = {
        "messages": [{"role": "user", "content": "查一下天气"}],
        "tools": [{"type": "function", "function": {"name": "weather"}}],
    }
    assert required_tags(gateway.CHAT_ENDPOINT, with_tools) == ["chat", "tools"]

    # 多轮里把工具结果发回去请求下一跳的，请求体里没有 tools，但一样只有
    # 会调工具的模型接得了。
    tool_reply = {"messages": [{"role": "tool", "tool_call_id": "1", "content": "晴"}]}
    assert required_tags(gateway.CHAT_ENDPOINT, tool_reply) == ["chat", "tools"]

    # 空 tools 不算要求函数调用：不少客户端无论如何都带上这个键。
    assert required_tags(gateway.CHAT_ENDPOINT, {"tools": []}) == ["chat"]


def test_embeddings_require_the_embedding_capability():
    assert required_tags(gateway.EMBEDDINGS_ENDPOINT, {"input": "文本"}) == ["embedding"]


def test_garbage_bodies_do_not_crash_the_rules():
    for body in (None, {}, {"messages": "not-a-list"}, {"messages": [None, {"content": 5}]}):
        assert isinstance(required_tags(gateway.CHAT_ENDPOINT, body or {}), list)


# ---- 分类的问与答 -----------------------------------------------------------


def test_task_prompt_text_takes_the_last_user_turn():
    body = {
        "messages": [
            {"role": "user", "content": "第一段"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": [{"type": "text", "text": "帮我改这段报错"}]},
        ]
    }
    assert task_prompt_text(body) == "帮我改这段报错"
    assert task_prompt_text({"messages": [{"role": "assistant", "content": "只有助手"}]}) is None


def test_parse_task_kind_only_accepts_the_vocabulary():
    assert parse_task_kind("reasoning") == "reasoning"
    assert parse_task_kind(" Code ") == "code"
    assert parse_task_kind("我觉得这题应该按 general 处理") == "general"
    # 认不出来的回答一律退回「按优先级选」，不做模糊归类。
    assert parse_task_kind("这看起来是一个技术类问题") is None
    assert parse_task_kind(None) is None


def test_classify_messages_truncates_the_input(monkeypatch):
    monkeypatch.setattr(auto.settings, "AI_GATEWAY_AUTO_CLASSIFY_CHARS", 10)
    messages = classify_messages("一" * 50)
    assert messages[0]["content"] == auto.CLASSIFY_SYSTEM
    assert messages[1]["content"] == "一" * 10


# ---- 候选池（SQLite 内存库）-------------------------------------------------


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


def _seed(db, *specs) -> AiGatewayService:
    """按 (通道名, 模型名, priority, 是否参与 Auto, 标签) 铺一套配置。"""
    service = AiGatewayService(db)
    channels = {}
    for name, model_name, priority, eligible, tags in specs:
        if name not in channels:
            channels[name] = service.create_channel(
                AiChannelCreate(name=name, base_url=f"https://{name}.test/v1", api_key="k")
            ).id
        service.create_route(
            AiModelRouteCreate(
                model_name=model_name,
                channel_id=channels[name],
                priority=priority,
                auto_eligible=eligible,
                auto_tags=tags,
            )
        )
    return service


def test_auto_is_reserved_for_a_real_route(db):
    """一条真叫 auto 的路由永远不会被命中，得在保存时就拦住。"""
    service = _seed(db, ("a", "one", 100, False, None))
    channel = service.list_channels()[0]

    for name in ("auto", " Auto "):
        with pytest.raises(ValueError):
            service.create_route(AiModelRouteCreate(model_name=name, channel_id=channel.id))

    service.update_route(
        service.list_routes("one")[0].id, AiModelRouteUpdate(model_name="gpt-4o")
    )
    assert service._public_name(" gpt-4o ") == "gpt-4o"


def test_auto_stays_out_of_the_model_list_until_someone_opts_in(db):
    service = _seed(db, ("fast", "small", 100, False, None))
    assert list_public_models(db) == ["small"]

    service.create_route(
        AiModelRouteCreate(
            model_name="big", channel_id=service.list_channels()[0].id,
            auto_eligible=True, auto_tags=["chat"],
        )
    )
    assert list_public_models(db) == ["auto", "big", "small"]

    # 关掉之后 auto 也跟着消失：列出来只会让调用方选完拿到 404。
    route = service.list_routes("big")[0]
    service.update_route(route.id, AiModelRouteUpdate(auto_eligible=False))
    assert list_public_models(db) == ["big", "small"]


def test_disabled_route_or_channel_leaves_the_pool(db):
    service = _seed(
        db,
        ("a", "one", 100, True, ["chat"]),
        ("b", "two", 200, True, ["chat"]),
    )
    assert [c.upstream_model for c in auto._feasible(db, ["chat"])] == ["one", "two"]

    service.update_channel(service.list_channels()[0].id, AiChannelUpdate(enabled=False))
    assert [c.upstream_model for c in auto._feasible(db, ["chat"])] == ["two"]


def test_resolve_auto_filters_by_declared_capability(db):
    _seed(
        db,
        ("text", "plain-model", 100, True, ["chat"]),
        ("eyes", "vision-model", 200, True, ["chat", "vision"]),
    )
    body = {"messages": [{"role": "user", "content": "你好"}]}
    candidates, decision = wait(resolve_auto(db, gateway.CHAT_ENDPOINT, body))
    assert [c.upstream_model for c in candidates] == ["plain-model", "vision-model"]
    assert decision["required"] == ["chat"]
    assert decision["source"] == "priority"

    image = {
        "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}
        ]
    }
    candidates, decision = wait(resolve_auto(db, gateway.CHAT_ENDPOINT, image))
    assert [c.upstream_model for c in candidates] == ["vision-model"]

    embeddings = wait(resolve_auto(db, gateway.EMBEDDINGS_ENDPOINT, {"input": "x"}))
    assert embeddings[0] == []
    assert embeddings[1]["required"] == ["embedding"]
    assert embeddings[1]["matched"] == 0


def test_task_tag_beats_priority_but_only_within_the_matched_group(db):
    _seed(
        db,
        ("cheap", "fast-model", 100, True, ["chat", "general"]),
        ("strong", "code-model", 300, True, ["chat", "code"]),
    )
    body = {"messages": [{"role": "user", "content": "这个报错怎么修"}]}

    async def classifier(candidate, text):
        return "code"

    candidates, decision = wait(resolve_auto(db, gateway.CHAT_ENDPOINT, body, classifier))
    assert [c.upstream_model for c in candidates] == ["code-model", "fast-model"]
    assert decision["task"] == "code"
    assert decision["source"] == "llm"


def test_nobody_declared_a_task_kind_so_no_question_is_asked(db):
    """池子里全是能力标签时，问模型也排不出先后——一次额外的出站请求都不该发。"""
    _seed(
        db,
        ("cheap", "fast-model", 100, True, ["chat"]),
        ("strong", "code-model", 300, True, ["chat"]),
    )

    async def spy(candidate, text):
        raise AssertionError("没有任务类型可比，不该发分类请求")

    candidates, decision = wait(
        resolve_auto(db, gateway.CHAT_ENDPOINT, {"messages": [{"role": "user", "content": "解题"}]}, spy)
    )
    assert [c.upstream_model for c in candidates] == ["fast-model", "code-model"]
    assert decision["source"] == "priority"


def test_classifier_sees_the_priority_first_candidate(db):
    _seed(
        db,
        ("cheap", "fast-model", 100, True, ["chat", "code"]),
        ("strong", "code-model", 300, True, ["chat", "code"]),
    )
    seen = []

    async def classifier(candidate, text):
        seen.append((candidate.upstream_model, text))
        return "general"

    wait(
        resolve_auto(
            db, gateway.CHAT_ENDPOINT, {"messages": [{"role": "user", "content": "写个函数"}]},
            classifier,
        )
    )
    # 任务类型还没定，探针只能按 priority 取第一条——它是候选池里最便宜的那条。
    assert seen == [("fast-model", "写个函数")]


@pytest.mark.parametrize("failure", [RuntimeError("上游连不上"), None])
def test_classification_failure_degrades_to_priority(db, failure):
    _seed(
        db,
        ("cheap", "fast-model", 100, True, ["chat", "general"]),
        ("strong", "code-model", 300, True, ["chat", "code"]),
    )

    async def classifier(candidate, text):
        if isinstance(failure, RuntimeError):
            raise failure
        return None  # 上游答非所问

    candidates, decision = wait(
        resolve_auto(
            db, gateway.CHAT_ENDPOINT, {"messages": [{"role": "user", "content": "解题"}]},
            classifier,
        )
    )
    assert [c.upstream_model for c in candidates] == ["fast-model", "code-model"]
    assert decision["task"] is None
    assert decision["source"] == "llm_failed" if failure else "llm_unclear"


def test_llm_stage_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(auto.settings, "AI_GATEWAY_AUTO_LLM", False)
    _seed(
        db,
        ("cheap", "fast-model", 100, True, ["chat", "general"]),
        ("strong", "code-model", 300, True, ["chat", "code"]),
    )

    async def classifier(candidate, text):  # pragma: no cover - 不该被调用
        raise AssertionError("关掉 LLM 后不该发分类请求")

    candidates, decision = wait(
        resolve_auto(
            db, gateway.CHAT_ENDPOINT, {"messages": [{"role": "user", "content": "解题"}]},
            classifier,
        )
    )
    assert [c.upstream_model for c in candidates] == ["fast-model", "code-model"]
    assert decision["source"] == "priority"


def test_embeddings_are_never_classified(db):
    _seed(db, ("vec", "embed-model", 100, True, ["embedding"]))

    async def classifier(candidate, text):  # pragma: no cover - 不该被调用
        raise AssertionError("向量化没有「任务类型」这一说")

    candidates, decision = wait(
        resolve_auto(db, gateway.EMBEDDINGS_ENDPOINT, {"input": "一段文本"}, classifier)
    )
    assert [c.upstream_model for c in candidates] == ["embed-model"]
    assert decision["source"] == "priority"


def test_route_definition_survives_an_edit(db):
    service = _seed(db, ("a", "one", 100, True, ["chat", "vision"]))
    route = service.list_routes("one")[0]
    assert route.auto_eligible is True and route.auto_tags == ["chat", "vision"]

    updated = service.update_route(route.id, AiModelRouteUpdate(auto_tags=["chat"]))
    assert updated.auto_tags == ["chat"]

    # 只改开关时也要满足「进了池子就有定义」，所以清空标签必须连带把 Auto 关掉。
    with pytest.raises(ValueError):
        service.update_route(route.id, AiModelRouteUpdate(auto_tags=[]))
    off = service.update_route(route.id, AiModelRouteUpdate(auto_eligible=False))
    assert off.auto_tags is None
    assert auto._feasible(db, ["chat"]) == []


def test_candidate_carries_the_upstream_key(db):
    _seed(db, ("a", "one", 100, True, ["chat"]))
    candidate = auto._feasible(db, ["chat"])[0]
    assert isinstance(candidate, RouteCandidate)
    assert candidate.upstream_key == "k"


def test_task_tags_probe_helper_only_looks_at_task_kinds():
    def candidate(tags):
        return SimpleNamespace(route=SimpleNamespace(auto_tags=tags))

    assert auto._task_tags_in_pool([candidate(["chat", "vision"])]) is False
    assert auto._task_tags_in_pool([candidate(["chat"]), candidate(["code"])]) is True
