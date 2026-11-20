"""语音合成（TTS）客户端：把一句文本合成成一段音频。

支持两种协议，按 ``model_config.base_url`` 自动分流，配置页不需要额外的协议开关：

* **openspeech 套餐协议**（Agent Plan 个人版语音模型，文档
  ``agent-plan-personal-voice-model``）：URL 形如
  ``https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional``。
  鉴权是 ``X-Api-Key`` 而不是 ``Authorization: Bearer``，模型由请求头
  ``X-Api-Resource-Id`` 指定（请求体里没有 model 字段），音色字段叫 ``speaker``，
  响应是**逐行 JSON 流**：``code=0`` 的行在 ``data`` 里带一块 base64 音频，
  ``code=20000000`` 表示结束，其余非零码是业务错误。
* **OpenAI 兼容协议**（方舟 ``/audio/speech`` 一类端点）：Bearer 鉴权，
  成功直接回音频二进制，失败才回 JSON 错误体。

两种形状都与 :mod:`app.services.media.ark` 的图片/视频任务不同，所以独立成模块。
与 ark 客户端同一组约定：地址来自 ``model_config``、客户端每次现建不缓存、
本模块只讲「发一次请求、把响应讲清楚」，不碰排队与重试。TTS 在发布作业的后台
任务里逐句调用，整句失败就让整条发布作业失败，不做静默降级——一条缺了半句
人声的成片比明确失败更难排查。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict

import httpx

from app.config import Settings, get_settings
from app.crypto import decrypt
from app.models.entities import ModelConfig

logger = logging.getLogger("yangvis.media.tts")

# 模块级快照：测试里把它换成 SimpleNamespace 就能把超时与闸门指到测试值。
settings: Settings = get_settings()

# ---- OpenAI 兼容协议 ---------------------------------------------------------

SPEECH_PATH = "/audio/speech"
DEFAULT_RESPONSE_FORMAT = "mp3"
SUPPORTED_RESPONSE_FORMATS = frozenset({"mp3", "wav", "ogg", "flac", "aac", "pcm"})

# ---- openspeech 套餐协议 -----------------------------------------------------

OPENSPEECH_HOST = "openspeech.bytedance.com"
OPENSPEECH_TTS_PATH = "/api/v3/plan/tts/unidirectional"
# 文档里 HTTP TTS 靠这个请求头选模型，请求体不允许带 model。目前套餐只有这一个
# TTS 资源；request_extra 里显式给 resource_id 时以它为准（将来上新资源不用改代码）。
PLAN_DEFAULT_RESOURCE_ID = "seed-tts-2.0"
# 文档示例的采样率；最终混音前 composer 还会统一 aresample，这里只决定上游编码。
PLAN_DEFAULT_SAMPLE_RATE = 24000
# 流的终止码：读到它就收尾，前面 code=0 的各块已经把音频拼完。
PLAN_DONE_CODE = 20000000
PLAN_DATA_CODE = 0

# 连通性探测合成的固定短句：两个字节的文本，成本可以忽略，却能验通密钥、型号、
# 音色三件套——TTS 没有「不花钱的只读探测」，这是这类配置唯一诚实的探法。
PROBE_TEXT = "测试"


class TtsError(RuntimeError):
    """上游拒绝了请求、连不上，或者回来的东西不是音频。

    保留 ``retriable`` 与 :class:`app.services.media.ark.ArkError` 同一口径，虽然
    发布作业当前不做自动重试——语义先定齐，以后把 TTS 挪进作业循环时不用改错误类。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status_code: int = 0,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retriable = retriable

    def friendly(self) -> str:
        return str(self)


def _api_key(config: ModelConfig) -> str:
    api_key = decrypt(config.api_key_enc) if config.api_key_enc else ""
    if not api_key:
        raise TtsError(
            "该模型配置没有 api_key：TTS 接口必须带密钥",
            code="NoApiKey",
            status_code=401,
        )
    return api_key


def _is_plan_endpoint(config: ModelConfig) -> bool:
    """按地址判断协议：openspeech 套餐端点走私有协议，其余按 OpenAI 兼容处理。

    同时认域名与完整路径两种写法——配置保存时不会剥这个路径（它不在
    ``normalize_base_url`` 的后缀表里），原样保留下来这里直接 POST。
    """
    base = (config.base_url or "").rstrip("/")
    return OPENSPEECH_HOST in base or base.endswith(OPENSPEECH_TTS_PATH)


# ---- OpenAI 兼容分支 ---------------------------------------------------------


def _openai_headers(config: ModelConfig) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_api_key(config)}",
    }


def _openai_url(config: ModelConfig) -> str:
    """base_url 入库时已归一到版本号那一层；这里再兜一次尾斜杠。"""
    return f"{(config.base_url or '').rstrip('/')}{SPEECH_PATH}"


def _describe_openai_error(response: httpx.Response) -> TtsError:
    """把 OpenAI 形状的错误响应翻成中文。错误体与 ark 同形（``{"error": {code, message}}``），
    但也见过顶层 ``{"code","message"}`` 与纯文本网关页，三种都兜。"""
    code = ""
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
            message = str(error.get("message") or "")
        if not code:
            code = str(body.get("code") or body.get("error_code") or "")
        if not message:
            message = str(body.get("message") or "")
        if not (code or message):
            message = response.text[:300]
    else:
        message = response.text[:300]

    status_code = response.status_code
    lowered = f"{code} {message}".lower()
    if any(token in lowered for token in ("contentrisk", "sensitive", "audit", "risk_control")):
        return TtsError(
            f"配音文本未通过内容安全审核（{code or 'ContentRisk'}）：改掉敏感表达后重试",
            code=code,
            status_code=status_code,
        )
    if status_code == 429 or "rate limit" in lowered or "qps" in lowered:
        return TtsError(
            f"触发上游限流（{code or status_code}），稍后重新发起合成",
            code=code,
            status_code=status_code,
            retriable=True,
        )
    if status_code in (401, 403):
        return TtsError(
            f"认证失败：api_key 无效或没有 {code or '该 TTS 模型'} 的权限",
            code=code,
            status_code=status_code,
        )
    if status_code == 404 or "not exist" in lowered or "invalidmodel" in lowered:
        return TtsError(
            f"模型或地址不对（{code or 'NotFound'}）：核对 TTS 的 Model ID 与 base_url"
            "（填到 /api/v3 这一层，/audio/speech 由系统自己拼）",
            code=code,
            status_code=status_code,
        )
    if status_code == 400 and ("voice" in lowered or "speaker" in lowered):
        return TtsError(
            f"音色 ID 不被该模型接受（{code or 'BadVoice'}）：{message[:160]}",
            code=code,
            status_code=status_code,
        )
    if status_code >= 500:
        detail = f"：{message[:200]}" if message else ""
        return TtsError(
            f"上游服务异常（{status_code}），稍后重新发起{detail}",
            code=code,
            status_code=status_code,
            retriable=True,
        )
    return TtsError(
        f"TTS 调用失败（{status_code} {code}）：{message[:200] or '上游没有给出原因'}",
        code=code,
        status_code=status_code,
    )


async def _synthesize_openai(
    config: ModelConfig,
    *,
    text: str,
    voice: str,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    speed: float = 1.0,
) -> bytes:
    if response_format not in SUPPORTED_RESPONSE_FORMATS:
        raise TtsError(
            f"不支持的音频格式 {response_format!r}，可选：{sorted(SUPPORTED_RESPONSE_FORMATS)}",
            code="BadFormat",
        )

    # request_extra 先铺底，显参数后盖过去：透传口只能补厂商私有参数，顶不掉主参数。
    payload: Dict[str, Any] = dict(config.request_extra or {})
    payload.update(
        {
            "model": config.model_name,
            "input": text,
            "voice": voice,
            "response_format": response_format,
            "speed": speed,
        }
    )

    try:
        async with httpx.AsyncClient(timeout=settings.TTS_HTTP_TIMEOUT) as client:
            response = await client.post(
                _openai_url(config), headers=_openai_headers(config), json=payload
            )
    except httpx.TimeoutException as exc:
        raise TtsError(
            f"TTS 响应超时（{settings.TTS_HTTP_TIMEOUT:.0f}s）：{exc}",
            code="Timeout",
            retriable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise TtsError(f"无法连接 TTS 服务：{exc}", code="NetworkError", retriable=True) from exc

    content_type = response.headers.get("content-type", "")
    if response.status_code >= 300 or "application/json" in content_type:
        # 200 却回 JSON 也是错：某些网关把错误包成 200，只看状态码会把 JSON 当音频存盘。
        raise _describe_openai_error(response)

    audio = response.content
    _guard_audio(audio)
    logger.info(
        "tts synthesized (openai): model=%s voice=%s chars=%s bytes=%s",
        config.model_name,
        voice,
        len(text),
        len(audio),
    )
    return audio


# ---- openspeech 套餐分支 -----------------------------------------------------


def _plan_headers(config: ModelConfig, resource_id: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        # 文档规定的套餐鉴权：专属 API Key 放这个头，不是 Authorization。
        "X-Api-Key": _api_key(config),
        "X-Api-Resource-Id": resource_id,
        # 文档示例带头：要求回传 token 用量，没有副作用，保持与官方示例同一形状。
        "X-Control-Require-Usage-Tokens-Return": "*",
    }


def _describe_plan_http_error(status_code: int, raw: str) -> TtsError:
    """流式开始之前的 HTTP 级失败（401/404/429/5xx），体一般是单个 JSON。"""
    code = ""
    message = raw[:200]
    try:
        body = json.loads(raw)
        if isinstance(body, dict):
            code = str(body.get("code") or "")
            message = str(body.get("message") or body.get("msg") or raw[:200])
    except (ValueError, TypeError):
        pass
    if status_code in (401, 403):
        return TtsError(f"认证失败：Agent Plan 专属 API Key 无效或无权限（{code}）",
                        code=code, status_code=status_code)
    if status_code == 429:
        return TtsError(f"触发套餐限流（{code}），稍后重新发起合成",
                        code=code, status_code=status_code, retriable=True)
    if status_code >= 500:
        return TtsError(f"语音服务异常（{status_code}），稍后重新发起：{message}",
                        code=code, status_code=status_code, retriable=True)
    return TtsError(f"TTS 调用失败（HTTP {status_code} {code}）：{message}",
                    code=code, status_code=status_code)


async def _synthesize_plan(config: ModelConfig, *, text: str, voice: str) -> bytes:
    """openspeech 套餐 HTTP 单向合成：JSON 行流，base64 分块拼回完整 mp3。"""
    extra = config.request_extra or {}
    resource_id = str(extra.get("resource_id") or PLAN_DEFAULT_RESOURCE_ID)
    sample_rate = int(extra.get("sample_rate") or PLAN_DEFAULT_SAMPLE_RATE)
    payload = {
        "req_params": {
            "text": text,
            # 套餐协议的音色字段叫 speaker，不叫 voice。
            "speaker": voice,
            "audio_params": {"format": "mp3", "sample_rate": sample_rate},
        }
    }
    url = (config.base_url or "").rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=settings.TTS_HTTP_TIMEOUT) as client:
            async with client.stream(
                "POST", url, headers=_plan_headers(config, resource_id), json=payload
            ) as response:
                if response.status_code >= 300:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    raise _describe_plan_http_error(response.status_code, raw)

                buffer = bytearray()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError as exc:
                        raise TtsError(
                            f"TTS 返回了无法解析的响应行：{line[:120]}",
                            code="BadResponse",
                        ) from exc
                    code = int(item.get("code", -1))
                    if code == PLAN_DATA_CODE:
                        data = item.get("data")
                        if data:
                            buffer += base64.b64decode(data)
                    elif code == PLAN_DONE_CODE:
                        break
                    else:
                        # 业务错误码：同样的输入重试还是同样的失败，不标 retriable。
                        detail = str(item.get("message") or item.get("msg") or "上游没有给出原因")
                        raise TtsError(
                            f"语音合成上游错误（code={code}）：{detail[:200]}",
                            code=str(code),
                        )
    except TtsError:
        raise
    except httpx.TimeoutException as exc:
        raise TtsError(
            f"TTS 响应超时（{settings.TTS_HTTP_TIMEOUT:.0f}s）：{exc}",
            code="Timeout",
            retriable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise TtsError(f"无法连接语音服务：{exc}", code="NetworkError", retriable=True) from exc

    audio = bytes(buffer)
    _guard_audio(audio)
    logger.info(
        "tts synthesized (openspeech plan): resource=%s speaker=%s chars=%s bytes=%s",
        resource_id,
        voice,
        len(text),
        len(audio),
    )
    return audio


def _guard_audio(audio: bytes) -> None:
    if not audio:
        raise TtsError("TTS 返回了空音频", code="EmptyAudio")
    if len(audio) > settings.TTS_AUDIO_MAX_BYTES:
        raise TtsError(
            f"单句音频超过 {settings.TTS_AUDIO_MAX_BYTES // 1024 // 1024}MB 上限，已放弃",
            code="AudioTooLarge",
        )


# ---- 公共入口 ---------------------------------------------------------------


async def synthesize(
    config: ModelConfig,
    *,
    text: str,
    voice: str,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    speed: float = 1.0,
) -> bytes:
    """合成一句，返回音频字节（两种协议统一输出 mp3 bytes）。

    ``voice`` 必填且由调用方保证非空：不同型号的音色字表不一样，这一层不替用户
    猜默认音色。文本上限交给调用方在入队前裁剪，这里只挡空串。
    """
    clean_text = (text or "").strip()
    if not clean_text:
        raise TtsError("合成文本为空", code="EmptyText")
    clean_voice = (voice or "").strip()
    if not clean_voice:
        raise TtsError(
            "没有指定音色：在项目渲染参数里填音色 ID（套餐协议即 speaker），"
            "或在服务端配置 TTS_DEFAULT_VOICE",
            code="NoVoice",
        )

    if _is_plan_endpoint(config):
        return await _synthesize_plan(config, text=clean_text, voice=clean_voice)
    return await _synthesize_openai(
        config,
        text=clean_text,
        voice=clean_voice,
        response_format=response_format,
        speed=speed,
    )


async def probe(config: ModelConfig, voice: str) -> tuple[bool, str]:
    """设置页的「测试连通性」：真合成一句短文本，验通密钥、型号与音色。

    TTS 没有免费只读接口，探测本身产生极少量计费，通过的提示语里如实说明。
    ``voice`` 为空时不发请求——没有音色的探测一定失败，不如先把配置方法讲清楚。
    """
    clean_voice = (voice or "").strip()
    if not clean_voice:
        return False, (
            "语音探测需要音色 ID：先在服务端环境变量 TTS_DEFAULT_VOICE 配一个该模型"
            "支持的音色（套餐协议即 speaker，例如 zh_female_vv_uranus_bigtts），"
            "或在短视频项目的渲染参数里指定"
        )
    try:
        audio = await synthesize(config, text=PROBE_TEXT, voice=clean_voice)
    except TtsError as exc:
        logger.warning("tts probe failed for config %s: %s", config.id, exc)
        return False, exc.friendly()
    except Exception as exc:  # noqa: BLE001 - 探测不该把任何异常抛给界面
        logger.warning("tts probe errored for config %s: %s", config.id, exc)
        return False, f"调用失败：{str(exc)[:200]}"
    protocol = "openspeech 套餐" if _is_plan_endpoint(config) else "OpenAI 兼容"
    return True, f"连接成功（{protocol}）：试合成音频 {len(audio)} 字节（探测会产生极少量计费）"
