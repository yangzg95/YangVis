"""YApi 开放接口客户端：只封装同步需要的三个接口。

YApi 的业务错误同样返回 HTTP 200，错误信息放在 ``errcode``/``errmsg`` 里，
所以判定成功不能只看状态码。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger("yangvis.yapi")

_TOKEN_IN_URL = re.compile(r"(?i)(token=)[^&\s\"']+")


class YapiError(RuntimeError):
    """YApi 不可达、鉴权失败或返回业务错误。"""


def _redact(text: str) -> str:
    return _TOKEN_IN_URL.sub(r"\1***", text)


class _RedactTokenFilter(logging.Filter):
    """httpx 的请求日志会把 URL（含 token 查询参数）打出来。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact(str(arg)) if isinstance(arg, httpx.URL) else arg
                for arg in record.args
            )
        return True


def _install_redaction_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(item, _RedactTokenFilter) for item in httpx_logger.filters):
        httpx_logger.addFilter(_RedactTokenFilter())


def normalize_base_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise YapiError("YApi 地址不能为空")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise YapiError("YApi 地址必须以 http:// 或 https:// 开头")
    if not parts.netloc:
        raise YapiError("YApi 地址缺少主机名")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class YapiClient:
    def __init__(
        self,
        base_url: str,
        project_id: int,
        token: str,
        *,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        _install_redaction_filter()
        self.base_url = normalize_base_url(base_url)
        self.project_id = project_id
        self._token = token
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> "YapiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- 接口 -----------------------------------------------------------------

    def list_menu(self) -> List[Dict[str, Any]]:
        """项目下的分类及每个分类里的接口（含 path/method，用来判重）。"""
        data = self._request(
            "GET",
            "/api/interface/list_menu",
            params={"project_id": self.project_id, "token": self._token},
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def add_category(self, name: str, description: str) -> int:
        data = self._request(
            "POST",
            "/api/interface/add_cat",
            data={
                "project_id": self.project_id,
                "token": self._token,
                "name": name,
                "desc": description,
            },
        )
        category_id = _extract_id(data)
        if category_id is None:
            raise YapiError(f"创建分类「{name}」后没有拿到分类 ID")
        return category_id

    def save_interface(self, payload: Dict[str, Any]) -> None:
        body = dict(payload)
        body["token"] = self._token
        body["project_id"] = self.project_id
        self._request("POST", "/api/interface/save", json=body)

    # -- 请求 -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise YapiError(f"连接 YApi 失败：{_redact(str(exc))}") from exc

        if response.status_code >= 400:
            raise YapiError(
                f"YApi 返回 HTTP {response.status_code} {response.reason_phrase}".strip()
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise YapiError("YApi 返回的不是 JSON，请检查地址是否指向 YApi 站点") from exc
        if not isinstance(body, dict):
            raise YapiError("YApi 响应格式不正确")

        errcode = body.get("errcode", 0)
        if errcode not in (0, "0", None):
            message = body.get("errmsg") or body.get("message") or "未知错误"
            raise YapiError(f"YApi 返回错误（{errcode}）：{message}")
        return body.get("data")


def _extract_id(data: Any) -> Optional[int]:
    if isinstance(data, dict):
        raw = data.get("_id", data.get("id"))
    else:
        raw = data
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
