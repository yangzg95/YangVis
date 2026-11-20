"""TTS 客户端：请求形状、错误翻译、二进制响应与大小闸门。

全程 ``httpx.MockTransport``，不出网也不花钱——与 ``test_media_ark.py`` 同一姿势，
这一层钉死的是**发出去的 JSON 形状**（方舟按它计费）和**错误怎么分类**。
TTS 成功响应是音频二进制而不是 JSON，所以这里不用 ark 那套 Upstream 路由表，
直接一个 handler 按用例预备的响应回答。异步测试也沿用全仓约定：同步用例里
``asyncio.run(...)``，不引入 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.crypto import encrypt
from app.models.entities import ModelConfig
from app.models.schemas import ModelPurpose
from app.services.media import tts

SPEECH_PATH = "/api/v3/audio/speech"

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x21" + b"0" * 64


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(
        tts,
        "settings",
        SimpleNamespace(TTS_HTTP_TIMEOUT=5.0, TTS_AUDIO_MAX_BYTES=1024),
    )


@pytest.fixture()
def config() -> ModelConfig:
    return ModelConfig(
        owner_id=1,
        purpose=ModelPurpose.TTS.value,
        title="测试语音模型",
        model_name="doubao-seed-tts-unit-test",
        base_url="https://ark.cn-beijing.volces.com/api/v3/",
        api_key_enc=encrypt("sk-unit-test"),
        request_extra={"resource_id": "volc.service.resource"},
    )


class Harness:
    """最小 MockTransport 夹具：记下请求，按预备的响应回答。"""

    def __init__(self, monkeypatch, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._response

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[0].content)


def _audio_response(audio: bytes = MP3) -> httpx.Response:
    return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})


@pytest.fixture()
def server(monkeypatch):
    def make(response: httpx.Response) -> Harness:
        return Harness(monkeypatch, response)

    return make


# ---- 请求拼装 ---------------------------------------------------------------


def test_synthesize_posts_openai_shape_and_returns_bytes(config, server):
    harness = server(_audio_response())

    audio = asyncio.run(tts.synthesize(config, text="你好世界", voice="zh_female_1"))

    assert audio == MP3
    request = harness.requests[0]
    assert request.method == "POST"
    assert request.url.path == SPEECH_PATH
    assert request.headers["authorization"] == "Bearer sk-unit-test"
    body = harness.body
    assert body["model"] == "doubao-seed-tts-unit-test"
    assert body["input"] == "你好世界"
    assert body["voice"] == "zh_female_1"
    assert body["response_format"] == "mp3"
    assert body["speed"] == 1.0


def test_request_extra_merges_but_explicit_params_win(config, server):
    harness = server(_audio_response())

    asyncio.run(tts.synthesize(config, text="一句台词", voice="zh_male_1"))

    body = harness.body
    # 配置里的厂商私有参数透传进来。
    assert body["resource_id"] == "volc.service.resource"
    # 显式参数不允许被 request_extra 顶掉。
    assert body["model"] == "doubao-seed-tts-unit-test"
    assert body["voice"] == "zh_male_1"


def test_empty_inputs_rejected_before_http(config, server):
    harness = server(_audio_response())

    with pytest.raises(tts.TtsError, match="合成文本为空"):
        asyncio.run(tts.synthesize(config, text="   ", voice="zh_female_1"))
    with pytest.raises(tts.TtsError, match="没有指定音色"):
        asyncio.run(tts.synthesize(config, text="有文本", voice=" "))
    with pytest.raises(tts.TtsError, match="音频格式"):
        asyncio.run(
            tts.synthesize(config, text="有文本", voice="v1", response_format="flacx")
        )
    # 参数闸门前置：一次 HTTP 都不该发出去。
    assert harness.requests == []


# ---- 错误翻译 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code, payload, expected",
    [
        (401, {"error": {"code": "AuthenticationError", "message": "bad key"}}, "认证失败"),
        (404, {"error": {"message": "model not exist"}}, "模型或地址不对"),
        (
            400,
            {"error": {"code": "InvalidVoice", "message": "voice not found"}},
            "音色 ID 不被该模型接受",
        ),
        (429, {"error": {"message": "rate limit"}}, "限流"),
        (500, {"message": "internal"}, "上游服务异常"),
    ],
)
def test_error_status_translated(config, server, status_code, payload, expected):
    harness = server(httpx.Response(status_code, json=payload))

    with pytest.raises(tts.TtsError, match=expected) as exc_info:
        asyncio.run(tts.synthesize(config, text="台词", voice="v1"))
    assert exc_info.value.status_code == status_code
    assert len(harness.requests) == 1


def test_json_body_on_200_is_treated_as_error(config, server):
    # 某些网关把错误包成 200：只看状态码会把 JSON 当音频存盘。
    harness = server(
        httpx.Response(
            200,
            json={"error": {"message": "wrapped failure"}},
            headers={"content-type": "application/json"},
        )
    )

    with pytest.raises(tts.TtsError, match="wrapped failure"):
        asyncio.run(tts.synthesize(config, text="台词", voice="v1"))
    assert len(harness.requests) == 1


def test_empty_audio_rejected(config, server):
    server(httpx.Response(200, content=b"", headers={"content-type": "audio/mpeg"}))

    with pytest.raises(tts.TtsError, match="空音频"):
        asyncio.run(tts.synthesize(config, text="台词", voice="v1"))


def test_oversized_audio_rejected(config, server):
    server(_audio_response(audio=b"x" * 2048))  # 闸门在夹具里被压到 1024 字节

    with pytest.raises(tts.TtsError, match="超过"):
        asyncio.run(tts.synthesize(config, text="台词", voice="v1"))


def test_missing_api_key_is_a_client_side_error(server):
    no_key = ModelConfig(
        owner_id=1,
        purpose=ModelPurpose.TTS.value,
        title="没密钥",
        model_name="m",
        base_url="https://ark.example/api/v3",
        api_key_enc=None,
    )
    harness = server(_audio_response())

    with pytest.raises(tts.TtsError, match="api_key"):
        asyncio.run(tts.synthesize(no_key, text="台词", voice="v1"))
    assert harness.requests == []


# ---- 连通性探测 -------------------------------------------------------------


def test_probe_succeeds_with_real_synthesis(config, server):
    harness = server(_audio_response())

    ok, message = asyncio.run(tts.probe(config, "zh_female_1"))

    assert ok is True
    assert "连接成功" in message
    # 探测文本是固定短句，验的是密钥+型号+音色三件套。
    assert harness.body["input"] == tts.PROBE_TEXT
    assert harness.body["voice"] == "zh_female_1"


def test_probe_without_voice_does_not_call_upstream(config, server):
    harness = server(_audio_response())

    ok, message = asyncio.run(tts.probe(config, ""))

    assert ok is False
    assert "音色" in message
    assert harness.requests == []


def test_probe_reports_upstream_failure(config, server):
    server(httpx.Response(401, json={"error": {"message": "bad key"}}))

    ok, message = asyncio.run(tts.probe(config, "zh_female_1"))

    assert ok is False
    assert "认证失败" in message


# ---- openspeech 套餐协议（Agent Plan 语音模型）------------------------------

PLAN_URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"


@pytest.fixture()
def plan_config() -> ModelConfig:
    return ModelConfig(
        owner_id=1,
        purpose=ModelPurpose.TTS.value,
        title="套餐语音模型",
        # 套餐协议请求体没有 model 字段，模型靠 X-Api-Resource-Id 头指定，
        # model_name 在这里只是给人看的记录。
        model_name="seed-tts-2.0",
        base_url=PLAN_URL,
        api_key_enc=encrypt("plan-key"),
    )


def _stream_line(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode()


def _plan_stream(chunks: list[bytes]) -> bytes:
    """把若干音频块编码成文档形状的 JSON 行流：每块一行 code=0，最后一行终止码。"""
    lines = [
        _stream_line({"code": 0, "data": base64.b64encode(chunk).decode()})
        for chunk in chunks
    ]
    lines.append(_stream_line({"code": tts.PLAN_DONE_CODE}))
    return b"".join(lines)


def test_plan_endpoint_is_detected_by_host_and_path(plan_config, config):
    assert tts._is_plan_endpoint(plan_config) is True
    # 方舟视觉/OpenAI 兼容地址不能误判进套餐分支。
    assert tts._is_plan_endpoint(config) is False


def test_plan_synthesize_uses_plan_headers_and_req_params(plan_config, server):
    harness = server(
        httpx.Response(
            200,
            content=_plan_stream([b"chunk-1-", b"chunk-2"]),
            headers={"content-type": "application/json"},
        )
    )

    audio = asyncio.run(
        tts.synthesize(plan_config, text="你好", voice="zh_female_vv_uranus_bigtts")
    )

    # base64 分块按到达顺序拼回完整音频。
    assert audio == b"chunk-1-chunk-2"
    request = harness.requests[0]
    assert request.url == PLAN_URL
    assert request.headers["x-api-key"] == "plan-key"
    assert request.headers["x-api-resource-id"] == "seed-tts-2.0"
    # 套餐鉴权不是 Bearer，也没有 model 字段——这是与 OpenAI 分支最关键的形状差异。
    assert "authorization" not in request.headers
    body = harness.body
    assert "model" not in body
    assert body["req_params"]["text"] == "你好"
    assert body["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"
    assert body["req_params"]["audio_params"] == {"format": "mp3", "sample_rate": 24000}


def test_plan_request_extra_overrides_resource_and_sample_rate(server):
    config = ModelConfig(
        owner_id=1,
        purpose=ModelPurpose.TTS.value,
        title="套餐语音模型",
        model_name="seed-tts-2.0",
        base_url=PLAN_URL,
        api_key_enc=encrypt("plan-key"),
        request_extra={"resource_id": "seed-tts-2.0", "sample_rate": 16000},
    )
    harness = server(
        httpx.Response(200, content=_plan_stream([b"x"]),
                       headers={"content-type": "application/json"})
    )

    asyncio.run(tts.synthesize(config, text="一句", voice="spk-1"))

    assert harness.requests[0].headers["x-api-resource-id"] == "seed-tts-2.0"
    assert harness.body["req_params"]["audio_params"]["sample_rate"] == 16000


def test_plan_business_error_line_raises(plan_config, server):
    stream = b"".join(
        [
            _stream_line({"code": 0, "data": base64.b64encode(b"partial").decode()}),
            _stream_line({"code": 4001, "message": "speaker not found"}),
        ]
    )
    server(httpx.Response(200, content=stream, headers={"content-type": "application/json"}))

    with pytest.raises(tts.TtsError, match="speaker not found") as exc_info:
        asyncio.run(tts.synthesize(plan_config, text="一句", voice="bad-speaker"))
    assert exc_info.value.code == "4001"
    assert exc_info.value.retriable is False


def test_plan_http_level_auth_failure(plan_config, server):
    server(httpx.Response(401, json={"code": 40100, "message": "invalid api key"}))

    with pytest.raises(tts.TtsError, match="认证失败"):
        asyncio.run(tts.synthesize(plan_config, text="一句", voice="spk-1"))


def test_plan_stream_ending_without_done_code_yields_what_arrived(plan_config, server):
    # 服务端没发终止码就断流：已到的分块照样拼得出音频，不当作错误——连接级异常
    # 由 httpx 自己抛，这里只验正常分块的解析不依赖终止码先行。
    stream = _stream_line({"code": 0, "data": base64.b64encode(b"only").decode()})
    server(httpx.Response(200, content=stream, headers={"content-type": "application/json"}))

    assert asyncio.run(tts.synthesize(plan_config, text="一句", voice="spk-1")) == b"only"


def test_plan_probe_sends_probe_text_and_reports_protocol(plan_config, monkeypatch):
    captured: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_plan_stream([b"ok"]),
            headers={"content-type": "application/json"},
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    ok, message = asyncio.run(tts.probe(plan_config, "spk-1"))

    assert ok is True
    assert "openspeech 套餐" in message
    # 探测文本与 speaker 都要进请求体。
    assert captured["body"]["req_params"]["text"] == tts.PROBE_TEXT
    assert captured["body"]["req_params"]["speaker"] == "spk-1"
