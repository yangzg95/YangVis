"""火山方舟 Ark 的图片与视频生成客户端。

只讲这一家的话，但**不把地址写死**：``base_url`` 与 ``model_name`` 全部来自
``model_config``（用途 ``image`` / ``video``），换前缀、换型号、换密钥都在设置页
改，不改代码。路径常量留在这里，因为它们是方舟接口形状的一部分——公开文档用的是
``/api/v3``，而有些套餐/网关会挂在别的前缀下，那种情况只改 ``base_url``。

两种调用形状完全不同，所以不共用一个函数：

* **图片**是同步的：一次 ``POST /images/generations`` 直接把 ``data[].url`` 带回来；
* **视频**是异步任务：``POST /contents/generations/tasks`` 只回一个 ``id``，之后
  靠 GET 查、靠 DELETE 取消（同一个路径，``queued`` 状态下是取消、终态下是删记录）。

因此本模块只负责「发一次请求、把响应讲清楚」，不碰重试也不碰排队——那些是
:mod:`app.services.media.jobs` 的事。客户端每次现建而不缓存，与
``services/providers.py`` 同一取舍：配置是按用户存的、随时可改。

产物 URL **一律不能直接给浏览器用**：文档写明视频链接 24 小时后失效、任务记录
7 天后查不到，所以调用方拿到 URL 必须马上经 :func:`app.services.media.store`
转存到本地磁盘。
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from app.config import Settings, get_settings
from app.crypto import decrypt
from app.models.entities import ModelConfig
from app.services.media import (
    ROLE_FIRST_FRAME,
    ROLE_LAST_FRAME,
    ROLE_REFERENCE_IMAGE,
)

logger = logging.getLogger("yangvis.media.ark")

# 模块级快照：测试里把它换成 SimpleNamespace 就能把请求指到本地 mock。
settings: Settings = get_settings()

TASKS_PATH = "/contents/generations/tasks"
IMAGES_PATH = "/images/generations"

# 一页只查一条，用于「地址 + 密钥」连通性探测：这是个只读接口，不产生任何抵扣。
PROBE_PAGE_NUM = 1
PROBE_PAGE_SIZE = 1


class ArkError(RuntimeError):
    """上游拒绝了请求，或者根本连不上。

    ``retriable`` 决定作业层是重排还是直接判死：429/5xx/网络抖动值得再试；
    400/401/403/404 与内容审核都是「同样重试还会同样失败」，重试只会白烧配额。
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


# ---- 请求形状 ---------------------------------------------------------------


@dataclass(frozen=True)
class VideoSubmission:
    """一次提交的结果：上游任务 id + 实际发出去的请求体。

    ``request`` 是原样发出去、去掉凭证的那份 JSON，作业层把它存进
    ``media_job.request``——渲染出来不对，要能看出当时喂的是哪段提示词。
    """

    task_id: str
    request: Dict[str, Any]


@dataclass(frozen=True)
class ImageResult:
    url: str
    size: str = ""
    request: Dict[str, Any] = field(default_factory=dict)
    usage: Optional[dict] = None


@dataclass(frozen=True)
class TaskState:
    """查询一次视频任务得到的状态快照。

    ``status`` 直接取上游的字面值（``queued``/``running``/``succeeded``/``failed``/
    ``cancelled``/``expired``），作业层原样写回 ``media_job.status``，不做二次翻译——
    词表在 :mod:`app.services.media` 里就是照它定的。
    """

    task_id: str
    status: str
    video_url: str = ""
    last_frame_url: str = ""
    error_code: str = ""
    error_message: str = ""
    usage: Optional[dict] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def image_data_url(raw: bytes, mime: str) -> str:
    """把本地图片转成方舟接受的小写 ``data:image/png;base64,...`` 形式。

    默认走这条路而不是公网 URL：``/uploads`` 不带鉴权，而且本地开发时它对手上的
    服务器根本不可达。上限按文档的 30MB 来，转完超过就早失败，别让请求体把内存吃掉。
    """
    if not raw:
        raise ArkError("参考图为空，无法内联进请求体")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def build_video_content(
    prompt: str,
    images: Sequence[Tuple[str, str]] = (),
    videos: Sequence[Tuple[str, str]] = (),
) -> List[dict]:
    """把提示词与参考素材拼成 ``content`` 数组。

    两者都以 ``(role, url)`` 传入，由调用方决定每档模式该给几个、给什么 role——这一层
    不认识「镜头」，只认识方舟的入参形状。

    图片与视频**不是同一个键**：图片走 ``image_url``（可以是 base64），视频走
    ``video_url`` 且只接受公网 URL 或 ``asset://`` 素材 ID，方舟不收视频的 base64。
    """
    content: List[dict] = [{"type": "text", "text": prompt}]
    for role, url in images:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    for role, url in videos:
        content.append({"type": "video_url", "video_url": {"url": url}, "role": role})
    return content


# ---- 客户端内部 -------------------------------------------------------------


def _headers(config: ModelConfig) -> Dict[str, str]:
    api_key = decrypt(config.api_key_enc) if config.api_key_enc else ""
    if not api_key:
        raise ArkError(
            "该模型配置没有 api_key：方舟的媒体接口必须带密钥",
            code="NoApiKey",
            status_code=401,
        )
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _url(config: ModelConfig, path: str) -> str:
    """base_url 已入库时去掉尾斜杠，这里再兜一次，免得拼出 ``//images``。"""
    return f"{(config.base_url or '').rstrip('/')}{path}"


def _describe_error(response: httpx.Response, *, expects_model: bool = True) -> ArkError:
    """把方舟的错误响应翻成中文，并判定值不值得重试。

    错误体是 OpenAI 兼容形状 ``{"error": {code, message, type}}``，但也见过顶层
    ``{"code","message"}``，以及网关直接回一坨纯文本——三种都试过，取不到就退化成
    「HTTP 状态码 + 原文截断」，绝不因为解析失败而丢掉原始信息。

    ``expects_model`` 决定 404 说哪一句：探测、查任务、取消任务这三类请求**根本没带
    模型**，对它们讲「去核对 Model ID」是把人往错的方向支走——那种 404 基本都是
    base_url 里多粘了一段接口路径。
    """
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
    # 内容安全：改提示词是唯一出路，重试只是白烧一次配额。
    if any(token in lowered for token in ("contentrisk", "sensitive", "audit", "risk_control")):
        return ArkError(
            f"提示词未通过内容安全审核（{code or 'ContentRisk'}）：换掉真人姓名、品牌或敏感描述后重试",
            code=code,
            status_code=status_code,
        )
    if status_code == 429 or "rate limit" in lowered or "qps" in lowered or "concurrency" in lowered:
        return ArkError(
            f"触发上游限流，稍后自动重试（{code or status_code}）",
            code=code,
            status_code=status_code,
            retriable=True,
        )
    if status_code in (401, 403):
        return ArkError(
            f"认证失败：api_key 无效或没有 {code or '该模型'} 的权限",
            code=code,
            status_code=status_code,
        )
    if status_code == 404 or "not exist" in lowered or "invalidmodel" in lowered:
        if not expects_model:
            return ArkError(
                f"地址或路径不对（{code or 'NotFound'}）：这一类请求不带模型，"
                "base_url 只要填到版本号那一层（例如 …/api/v3），后面那段接口路径由系统自己拼；"
                "查任务时若路径没问题，就是这条任务已过了上游的记录期",
                code=code,
                status_code=status_code,
            )
        return ArkError(
            f"模型或地址不对（{code or 'NotFound'}）：请核对 Model ID 与 base_url，"
            "方舟这里要的是带日期后缀的 Model ID 而不是模型名称",
            code=code,
            status_code=status_code,
        )
    if status_code >= 500:
        # 原文照带：非 JSON 的错误体（网关直接回一坨 HTML）在这种情况下就是唯一的
        # 线索，只报「上游异常」等于让用户和排障的人都只能干等。
        detail = f"：{message[:200]}" if message else ""
        return ArkError(
            f"上游服务异常（{status_code}），稍后自动重试{detail}",
            code=code,
            status_code=status_code,
            retriable=True,
        )
    return ArkError(
        f"调用失败（{status_code} {code}）：{message[:200] or '上游没有给出原因'}",
        code=code,
        status_code=status_code,
    )


async def _request_json(
    config: ModelConfig,
    method: str,
    path: str,
    *,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: Optional[float] = None,
    expects_model: bool = True,
) -> dict:
    """发一次 JSON 请求并解析成 dict；非 2xx 一律抛 :class:`ArkError`。

    不在这儿重试：图片是同步生成、视频是提交任务，两者的重试口径完全不同，
    统一放到作业层按 ``ArkError.retriable`` 决定。

    ``expects_model`` 转给错误翻译：请求体里带 model 的那几档，404 才可能是 Model ID
    写错；查任务、取消任务、探测这些不带模型的调用要说的是「地址或路径不对」。
    """
    url = _url(config, path)
    client_settings = timeout if timeout is not None else settings.MEDIA_HTTP_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=client_settings) as client:
            response = await client.request(
                method, url, headers=_headers(config), json=payload, params=params
            )
    except httpx.TimeoutException as exc:
        raise ArkError(
            f"上游响应超时（{client_settings:.0f}s）：{exc}",
            code="Timeout",
            retriable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise ArkError(f"无法连接方舟：{exc}", code="NetworkError", retriable=True) from exc

    if response.status_code >= 300:
        raise _describe_error(response, expects_model=expects_model)
    try:
        body = response.json()
    except ValueError as exc:
        raise ArkError(
            f"方舟返回的不是 JSON：{response.text[:200]!r}", code="BadResponse"
        ) from exc
    if not isinstance(body, dict):
        raise ArkError("方舟返回的 JSON 顶层不是对象", code="BadResponse")
    return body


# ---- 图片（同步）-----------------------------------------------------------


async def generate_image(
    config: ModelConfig,
    *,
    prompt: str,
    size: str = "2K",
    images: Sequence[str] = (),
    watermark: bool = False,
) -> ImageResult:
    """文生图 / 图生图，同步拿到图片 URL。

    ``images`` 是参考图（URL 或 data URL），非空时就是图生图——首帧的「风格锚点」
    和人物一致性靠这一手，不用先把图传到某个公网地方。
    """
    payload: Dict[str, Any] = {
        "model": config.model_name,
        "prompt": prompt,
        "size": size,
        "watermark": watermark,
    }
    if len(images) == 1:
        payload["image"] = images[0]
    elif images:
        payload["image"] = list(images)

    body = await _request_json(config, "POST", IMAGES_PATH, payload=payload)
    items = body.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ArkError("图片生成没有返回 data[]", code="BadResponse")
    url = str(items[0].get("url") or "")
    if not url:
        raise ArkError("图片生成返回的 data[] 里没有 url", code="BadResponse")

    logger.info("ark image generated: model=%s size=%s refs=%s", config.model_name, size, len(images))
    return ImageResult(
        url=url,
        size=str(items[0].get("size") or ""),
        request=payload,
        usage=body.get("usage") if isinstance(body.get("usage"), dict) else None,
    )


# ---- 视频（异步任务）-------------------------------------------------------


async def submit_video(
    config: ModelConfig,
    *,
    content: Sequence[dict],
    params: Optional[dict] = None,
) -> VideoSubmission:
    """创建视频生成任务，只回任务 id。

    ``params`` 是画幅/分辨率/时长那一档（键名与方舟请求体同名），由作业层从项目
    ``params`` 快照里来，这里不猜默认值——1.0 系和 2.x 的默认画幅不一样，写死一种
    就是给另一种埋雷。
    """
    payload: Dict[str, Any] = {"model": config.model_name, "content": list(content)}
    payload.update(params or {})

    body = await _request_json(config, "POST", TASKS_PATH, payload=payload)
    task_id = str(body.get("id") or "")
    if not task_id:
        raise ArkError("创建视频任务没有返回任务 id", code="BadResponse")

    logger.info(
        "ark video task created: id=%s model=%s params=%s",
        task_id,
        config.model_name,
        {key: value for key, value in payload.items() if key != "content"},
    )
    return VideoSubmission(task_id=task_id, request=payload)


async def get_video_task(config: ModelConfig, task_id: str) -> TaskState:
    """查一次任务状态。"""
    body = await _request_json(
        config, "GET", f"{TASKS_PATH}/{task_id}", expects_model=False
    )
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    return TaskState(
        task_id=str(body.get("id") or task_id),
        status=str(body.get("status") or ""),
        video_url=str(content.get("video_url") or ""),
        last_frame_url=str(content.get("last_frame_url") or ""),
        error_code=str(error.get("code") or ""),
        error_message=str(error.get("message") or ""),
        usage=body.get("usage") if isinstance(body.get("usage"), dict) else None,
        raw=body,
    )


async def cancel_video_task(config: ModelConfig, task_id: str) -> None:
    """取消排队中的任务；已终态的任务这条调用等于删记录。"""
    await _request_json(
        config, "DELETE", f"{TASKS_PATH}/{task_id}", expects_model=False
    )
    logger.info("ark video task cancelled: id=%s", task_id)


# ---- 连通性探测与下载 ------------------------------------------------------


async def probe(config: ModelConfig) -> Tuple[bool, str]:
    """设置页的「测试连通性」。

    走的是任务列表这个只读接口：它验得出 base_url 与 api_key，**验不出 model_name**
    ——媒体任务没有「不花钱又能验证模型」的办法（拿真模型提交一条最小视频就等于
    扣一次抵扣）。所以通过时要如实说明，别让用户以为这一步已经把型号验证过了。
    """
    try:
        body = await _request_json(
            config,
            "GET",
            TASKS_PATH,
            params={"page_num": PROBE_PAGE_NUM, "page_size": PROBE_PAGE_SIZE},
            expects_model=False,
        )
    except ArkError as exc:
        logger.warning("ark probe failed for config %s: %s", config.id, exc)
        return False, exc.friendly()
    except Exception as exc:  # noqa: BLE001 - 探测不该把任何异常抛给界面
        logger.warning("ark probe errored for config %s: %s", config.id, exc)
        return False, f"调用失败：{str(exc)[:200]}"
    total = body.get("total")
    return True, (
        f"连接成功（密钥与地址可用；Model ID 要在第一次真实生成时才会校验），"
        f"当前有 {total if total is not None else '?'} 条历史任务"
    )


async def download(url: str, *, max_bytes: int, timeout: Optional[float] = None) -> bytes:
    """把上游产物拉回内存（再由 store 落盘）。

    流式读但按 ``max_bytes`` 硬截：方舟的限制是视频输入 <200MB、图片 <30MB，输出
    远不到这个量级，超了说明这个 URL 指向的不是我们预期的产物，宁可判失败也不要
    把它灌进单实例的内存里。
    """
    seconds = timeout if timeout is not None else settings.MEDIA_DOWNLOAD_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=seconds, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 300:
                    raise ArkError(
                        f"产物下载失败（HTTP {response.status_code}）：上游链接可能已过期，请重新渲染",
                        code="DownloadFailed",
                        # 链接过期是判死；网络抖动也一律先判死，重渲比留一个半截文件便宜。
                        status_code=response.status_code,
                    )
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > max_bytes:
                        raise ArkError(
                            f"产物超过 {max_bytes // 1024 // 1024}MB 上限，已放弃下载",
                            code="DownloadTooLarge",
                        )
    except ArkError:
        raise
    except httpx.TimeoutException as exc:
        raise ArkError(f"产物下载超时（{seconds:.0f}s）：{exc}", code="DownloadTimeout", retriable=True) from exc
    except httpx.HTTPError as exc:
        raise ArkError(f"产物下载连接失败：{exc}", code="DownloadNetwork", retriable=True) from exc

    if not buffer:
        raise ArkError("产物是空的", code="DownloadEmpty")
    return bytes(buffer)
