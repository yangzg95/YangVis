"""智能办公 · 百度网盘（xpan 开放平台）的通用能力：绑定、上传、下载、删除。

每个用户在页面上 OAuth 绑定**自己的**网盘（一人一绑，与全局的按用户隔离
约定一致）。文件操作本身与具体业务无关：各模块往 :meth:`NetdiskService.upload_file`
传一个相对应用目录的路径（各自约定子目录，如简历模块用 ``resumes/<id>_<文件名>``），
拿回 ``(fs_id, 完整路径)`` 存到自己的表里；下载凭 fs_id，删除凭路径。

不引入官方 SDK（2022 年的 openapi-generator 产物，依赖老旧、体积大），按
demo（pythonsdk_20220616）里的端点用项目已有的 httpx 直连。关键约定：

- 文件只能落在网盘的 ``/apps/<APP_NAME>/`` 应用目录下；
- 请求 dlink 下载时必须带 header ``User-Agent: pan.baidu.com``，否则 403；
- access_token 有效期约 30 天，临期（<5 分钟）时用 refresh_token 自动换新；
- 两个 token 以 Fernet 密文落库，与模型 API key 同一约定。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.errors import (
    BusinessError,
    CODE_NETDISK_API_ERROR,
    CODE_NETDISK_NOT_BOUND,
    CODE_NETDISK_NOT_CONFIGURED,
)
from app.models.entities import NetdiskAccount
from app.models.schemas import NetdiskStatus

logger = logging.getLogger("yangvis.netdisk")


class _TokenRedactingFilter(logging.Filter):
    """把 httpx 请求日志 URL 里的 access_token 打码。

    xpan 的接口设计要求 access_token 走 query 参数，而 httpx 的 INFO 日志会
    打印完整 URL——不过滤的话，用户的网盘令牌会原样留在应用日志里。
    """

    _PATTERN = re.compile(r"access_token=[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if "access_token=" in str(record.msg) or any(
            "access_token=" in str(arg) for arg in (record.args or ())
        ):
            record.msg = self._PATTERN.sub("access_token=***", record.getMessage())
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_TokenRedactingFilter())

_OAUTH_HOST = "https://openapi.baidu.com"
_PAN_HOST = "https://pan.baidu.com"
_UPLOAD_HOST = "https://d.pcs.baidu.com"

# 分片上传的片长（xpan 上限 4MB）；片数 = ceil(size / 4MB)，每片一个 MD5。
_SLICE_BYTES = 4 * 1024 * 1024

# access_token 剩余有效期低于这个值就提前刷新，避免请求做到一半过期。
_REFRESH_MARGIN = timedelta(minutes=5)

_HTTP_TIMEOUT = 60.0

# xpan 常见 errno 的中文提示；未列出的带 errno 原样返回，方便排查。
_ERRNO_MESSAGES = {
    -6: "网盘授权已失效，请重新绑定",
    -7: "网盘文件不存在或已被移动",
    2: "网盘接口参数错误",
    31034: "网盘接口访问过于频繁，请稍后再试",
    42001: "网盘授权已失效，请重新绑定",
}


def _now() -> datetime:
    # 实体里的 DateTime 是 naive（MySQL 的 DATETIME 不带时区）；统一 naive UTC。
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_filename(filename: str) -> str:
    # 网盘路径段里不允许出现 '/' 与控制字符；清洗后为空就用 "_" 兜底。
    return re.sub(r'[/\\\x00-\x1f]', "_", filename).strip() or "_"


class NetdiskService:
    """当前用户的网盘绑定与文件操作。未配置 / 未绑定时抛 :class:`BusinessError`。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._settings = get_settings()

    # -- 配置与绑定状态 ------------------------------------------------------

    @property
    def _configured(self) -> bool:
        return bool(
            self._settings.BAIDU_NETDISK_APP_KEY
            and self._settings.BAIDU_NETDISK_SECRET_KEY
            and self._settings.BAIDU_NETDISK_APP_NAME
        )

    def _require_configured(self) -> None:
        if not self._configured:
            raise BusinessError(
                CODE_NETDISK_NOT_CONFIGURED,
                "百度网盘功能未启用：请在后端 .env 配置 "
                "BAIDU_NETDISK_APP_KEY / SECRET_KEY / APP_NAME",
            )

    def get_account(self) -> Optional[NetdiskAccount]:
        return self._db.scalar(
            select(NetdiskAccount).where(NetdiskAccount.owner_id == self._owner_id)
        )

    def is_bound(self) -> bool:
        return self._configured and self.get_account() is not None

    def status_view(self) -> NetdiskStatus:
        account = self.get_account()
        return NetdiskStatus(
            configured=self._configured,
            bound=account is not None,
            baidu_name=account.baidu_name if account else None,
            expires_at=account.expires_at if account else None,
        )

    def auth_url(self) -> str:
        self._require_configured()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.BAIDU_NETDISK_APP_KEY,
                "redirect_uri": self._settings.BAIDU_NETDISK_REDIRECT_URI,
                "scope": "basic,netdisk",
                "display": "page",
            }
        )
        return f"{_OAUTH_HOST}/oauth/2.0/authorize?{query}"

    # -- 绑定 / 解绑 -----------------------------------------------------------

    async def bind(self, code: str) -> NetdiskAccount:
        """用授权码换 token 并落库（upsert）。code 失效等错误转成可读提示。"""
        self._require_configured()
        data = await self._get_json(
            f"{_OAUTH_HOST}/oauth/2.0/token",
            params={
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self._settings.BAIDU_NETDISK_APP_KEY,
                "client_secret": self._settings.BAIDU_NETDISK_SECRET_KEY,
                "redirect_uri": self._settings.BAIDU_NETDISK_REDIRECT_URI,
            },
        )
        self._check_oauth_error(data, "授权码换取令牌失败")

        account = self.get_account()
        if account is None:
            account = NetdiskAccount(owner_id=self._owner_id)
            self._db.add(account)
        self._store_tokens(account, data)

        # 顺带拉一次用户信息：既验证 token 确实可用，也拿到展示用的百度账号名。
        info = await self._get_json(
            f"{_PAN_HOST}/rest/2.0/xpan/nas",
            params={
                "method": "uinfo",
                "access_token": decrypt(account.access_token_enc),
            },
        )
        self._check_errno(info, "获取网盘用户信息失败")
        account.baidu_uid = str(info.get("uk") or "")
        account.baidu_name = info.get("baidu_name") or None

        self._db.commit()
        self._db.refresh(account)
        return account

    def _store_tokens(self, account: NetdiskAccount, data: dict) -> None:
        account.access_token_enc = encrypt(str(data["access_token"]))
        account.refresh_token_enc = encrypt(str(data["refresh_token"]))
        expires_in = int(data.get("expires_in") or 0)
        account.expires_at = _now() + timedelta(seconds=expires_in)

    def unbind(self) -> None:
        """解除绑定。只删本地凭据，不动网盘里已同步的文件。"""
        account = self.get_account()
        if account is None:
            raise BusinessError(CODE_NETDISK_NOT_BOUND, "尚未绑定百度网盘")
        self._db.delete(account)
        self._db.commit()

    # -- token -----------------------------------------------------------------

    async def _access_token(self) -> str:
        account = self.get_account()
        if account is None:
            raise BusinessError(
                CODE_NETDISK_NOT_BOUND, "尚未绑定百度网盘，请先在页面上完成绑定"
            )
        if account.expires_at is not None and account.expires_at <= _now() + _REFRESH_MARGIN:
            await self._refresh(account)
        return decrypt(account.access_token_enc)

    async def _refresh(self, account: NetdiskAccount) -> None:
        data = await self._get_json(
            f"{_OAUTH_HOST}/oauth/2.0/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": decrypt(account.refresh_token_enc),
                "client_id": self._settings.BAIDU_NETDISK_APP_KEY,
                "client_secret": self._settings.BAIDU_NETDISK_SECRET_KEY,
            },
        )
        self._check_oauth_error(data, "网盘授权刷新失败，请重新绑定")
        self._store_tokens(account, data)
        self._db.commit()
        logger.info("refreshed netdisk access token for owner %s", self._owner_id)

    # -- 文件操作 ---------------------------------------------------------------

    def _remote_path(self, rel_path: str) -> str:
        """把相对应用目录的路径拼成完整网盘路径，逐段清洗。

        ``.`` / ``..`` 段替换成 ``_``，保证结果不会逃出 ``/apps/<APP_NAME>/``。"""
        app_name = self._settings.BAIDU_NETDISK_APP_NAME
        segments = []
        for seg in rel_path.split("/"):
            if not seg.strip():
                continue
            seg = _safe_filename(seg)
            segments.append("_" if seg in (".", "..") else seg)
        if not segments:
            raise BusinessError(CODE_NETDISK_API_ERROR, "网盘目标路径为空")
        return f"/apps/{app_name}/" + "/".join(segments)

    async def upload_file(self, rel_path: str, raw: bytes) -> Tuple[int, str]:
        """xpan 三步上传（precreate → superfile2 分片 → create），返回 (fs_id, path)。

        ``rel_path`` 相对应用目录 ``/apps/<APP_NAME>/``，由调用方约定自己的
        子目录（如简历模块传 ``resumes/12_zhangsan.pdf``）。"""
        self._require_configured()
        token = await self._access_token()
        path = self._remote_path(rel_path)
        filename = path.rsplit("/", 1)[-1]

        slices = [raw[i : i + _SLICE_BYTES] for i in range(0, len(raw), _SLICE_BYTES)] or [b""]
        block_list = json.dumps([hashlib.md5(part).hexdigest() for part in slices])

        pre = await self._post_form(
            f"{_PAN_HOST}/rest/2.0/xpan/file",
            params={"method": "precreate", "access_token": token},
            data={
                "path": path,
                "size": str(len(raw)),
                "isdir": "0",
                "autoinit": "1",
                "rtype": "3",
                "block_list": block_list,
            },
        )
        self._check_errno(pre, "网盘上传预创建失败")
        upload_id = pre.get("uploadid")
        if not upload_id:
            raise BusinessError(
                CODE_NETDISK_API_ERROR, "网盘上传预创建失败：响应里没有 uploadid"
            )

        for seq, part in enumerate(slices):
            piece = await self._post_form(
                f"{_UPLOAD_HOST}/rest/2.0/pcs/superfile2",
                params={
                    "method": "upload",
                    "access_token": token,
                    "type": "tmpfile",
                    "path": path,
                    "uploadid": upload_id,
                    "partseq": str(seq),
                },
                files={"file": (filename, part, "application/octet-stream")},
            )
            self._check_errno(piece, f"网盘上传第 {seq + 1}/{len(slices)} 个分片失败")

        created = await self._post_form(
            f"{_PAN_HOST}/rest/2.0/xpan/file",
            params={"method": "create", "access_token": token},
            data={
                "path": path,
                "size": str(len(raw)),
                "isdir": "0",
                "uploadid": upload_id,
                "rtype": "3",
                "block_list": block_list,
            },
        )
        self._check_errno(created, "网盘创建文件失败")
        fs_id = created.get("fs_id")
        if fs_id is None:
            raise BusinessError(CODE_NETDISK_API_ERROR, "网盘创建文件失败：响应里没有 fs_id")
        return int(fs_id), path

    async def open_download(self, fs_id: int) -> Tuple[str, dict]:
        """取带鉴权的下载地址与必须的请求头。

        返回的 URL 里含有 access_token，只能留在服务端做代理下载，绝不下发
        给浏览器（302 会把 token 泄进浏览器历史与任何中间日志）。
        """
        token = await self._access_token()
        data = await self._get_json(
            f"{_PAN_HOST}/rest/2.0/xpan/multimedia",
            params={
                "method": "filemetas",
                "access_token": token,
                "fsids": json.dumps([fs_id]),
                "dlink": "1",
            },
        )
        self._check_errno(data, "获取网盘下载地址失败")
        items = data.get("list") or []
        dlink = items[0].get("dlink") if items else None
        if not dlink:
            raise BusinessError(
                CODE_NETDISK_API_ERROR, "获取网盘下载地址失败：文件可能已被删除"
            )
        sep = "&" if "?" in dlink else "?"
        return f"{dlink}{sep}access_token={token}", {"User-Agent": "pan.baidu.com"}

    async def delete_file(self, path: str) -> None:
        """best-effort 删除：任何失败只记日志，绝不阻塞简历本身的删除。"""
        try:
            token = await self._access_token()
            resp = await self._post_form(
                f"{_PAN_HOST}/rest/2.0/xpan/file",
                params={"method": "filemanager", "opera": "delete", "access_token": token},
                data={"async": "0", "filelist": json.dumps([{"path": path}])},
            )
            self._check_errno(resp, "删除网盘文件失败")
        except Exception as exc:  # noqa: BLE001 - best-effort，失败不值得冒头
            logger.warning("netdisk delete %s failed: %s", path, exc)

    # -- 底层 HTTP --------------------------------------------------------------

    @staticmethod
    def _check_oauth_error(data: dict, prefix: str) -> None:
        # OAuth 端点的错误格式是 {"error": ..., "error_description": ...}，
        # 与 xpan 业务接口的 errno 风格不同，单独处理。
        if "error" not in data:
            return
        description = data.get("error_description") or data.get("error")
        raise BusinessError(CODE_NETDISK_API_ERROR, f"{prefix}：{description}")

    @staticmethod
    def _check_errno(data: dict, prefix: str) -> None:
        errno = data.get("errno", 0)
        if errno in (None, 0):
            return
        message = _ERRNO_MESSAGES.get(errno)
        if message is None:
            detail = data.get("errmsg") or data.get("error_msg") or ""
            message = f"网盘接口返回错误（errno={errno}）{detail}".strip()
        raise BusinessError(CODE_NETDISK_API_ERROR, f"{prefix}：{message}")

    async def _get_json(self, url: str, *, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BusinessError(CODE_NETDISK_API_ERROR, f"访问百度网盘失败：{exc}") from exc
        return self._parse(resp)

    async def _post_form(
        self,
        url: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(url, params=params, data=data, files=files)
        except httpx.HTTPError as exc:
            raise BusinessError(CODE_NETDISK_API_ERROR, f"访问百度网盘失败：{exc}") from exc
        return self._parse(resp)

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise BusinessError(
                CODE_NETDISK_API_ERROR,
                f"百度网盘返回了无法解析的响应（HTTP {resp.status_code}）",
            ) from exc
        if not isinstance(data, dict):
            raise BusinessError(
                CODE_NETDISK_API_ERROR,
                f"百度网盘返回了意料之外的响应（HTTP {resp.status_code}）",
            )
        return data
