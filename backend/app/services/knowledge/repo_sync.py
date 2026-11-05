"""Git 代码仓库的接入、凭据管理与增量同步。

同步的本质见知识库设计文档：把远端某个 commit 下的受跟踪源码文件物化成一批
``KbDocument(source='repo')``，之后的切片、embedding、检索全部复用文档链路。

传输与凭据：

- SSH（默认）：每仓库一对独立 ed25519 deploy key，私钥 Fernet 加密落库，同步时
  解密写到仓库目录下的 ``id_key``（0600，仓库工作区之外），通过
  ``GIT_SSH_COMMAND`` 注入，不进 URL、不进 ssh-agent，finally 必删。
- 主机指纹 TOFU：首次连接 ``accept-new`` 记录 known_hosts 行，之后严格比对；
  指纹变化同步失败，必须经 retrust 显式更新。
- http(s) 允许两种认证：匿名（内网公开只读仓库）与个人访问令牌。令牌同样
  Fernet 加密落库，同步时解密写到仓库目录下的 ``http_cred``（git
  credential-store 格式，0600），通过 ``GIT_CONFIG_*`` 环境变量指向它注入；
  令牌不进 URL（git 的报错信息因此也永远带不出令牌）、不进命令行参数，finally
  必删。不接受地址内嵌凭据。

git 交互一律 ``create_subprocess_exec`` 参数数组（不经 shell），环境白名单式
构造。与文档索引同一单 worker 口径：模块级锁，重启靠启动重置兜底。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.database import SessionLocal
from app.models.entities import KbDocument, KbRepo, KnowledgeType
from app.models.schemas import DocumentStatus, RepoSyncResult
from app.services.knowledge import code_chunk, vectorstore
from app.services.knowledge.service import KnowledgeService
from app.timeutil import db_now

logger = logging.getLogger("yangvis.reposync")

settings = get_settings()


class RepoError(ValueError):
    """仓库地址/凭据等输入不合法。"""


class RepoSyncError(RuntimeError):
    """同步过程失败（git/网络/索引）。"""


class RepoStatus:
    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    ERROR = "error"


# ---- 地址解析 ---------------------------------------------------------------


@dataclass(frozen=True)
class GitTarget:
    protocol: str  # ssh | http | https
    host: str
    port: Optional[int]
    user: Optional[str]
    path: str  # 仓库路径，/group/repo.git 形态
    canonical_url: str  # 不含凭据的规范化地址（SSH 补默认用户 git）


def _host_allowed(host: str) -> None:
    allowlist = [
        piece.strip().lower()
        for piece in settings.KB_REPO_HOST_ALLOWLIST.split(",")
        if piece.strip()
    ]
    if allowlist and not any(
        host == suffix or host.endswith("." + suffix) for suffix in allowlist
    ):
        raise RepoError(f"主机 {host} 不在允许列表内")


def parse_git_url(raw: str) -> GitTarget:
    """把用户填的地址解析成 :class:`GitTarget`，只放行 ssh 与 http(s)（匿名或令牌）。

    支持 ``ssh://[user@]host[:port]/path`` 与 scp-like ``[user@]host:path``。
    ``file://``、``ext::``、内嵌换行、``-`` 开头的主机段（option 注入）一律拒绝。
    """
    if not raw or any(char in raw for char in "\r\n"):
        raise RepoError("仓库地址为空或包含非法字符")
    url = raw.strip()
    if url.startswith(("-", "ext::")):
        raise RepoError("非法的仓库地址")

    if "://" in url:
        parsed = urlparse(url)
        protocol = parsed.scheme.lower()
        if protocol not in ("ssh", "http", "https"):
            raise RepoError("仅支持 ssh:// 与 http(s):// 地址")
        host = parsed.hostname or ""
        if not host or host.startswith("-"):
            raise RepoError("仓库地址缺少主机名")
        if protocol in ("http", "https") and (parsed.username or parsed.password):
            # http(s) 内嵌凭据会被记进 remote_url、日志与异常，P0 不接受；
            # ssh:// 的 user（通常是 git）是地址的正常组成部分，不能一并拒掉。
            raise RepoError("请不要在地址中内嵌用户名或令牌")
        path = parsed.path or ""
        if not path or path == "/":
            raise RepoError("仓库地址缺少仓库路径")
        user = "git" if protocol == "ssh" and not parsed.username else parsed.username
        netloc = host if not parsed.port else f"{host}:{parsed.port}"
        if protocol == "ssh":
            canonical = f"ssh://{user}@{netloc}{path}"
        else:
            # 非标准端口（内网 GitLab 常见）必须原样保留，丢掉它就会连错地方。
            canonical = f"{protocol}://{netloc}{path}"
        target = GitTarget(protocol, host, parsed.port, user, path, canonical)
    else:
        # scp-like: [user@]host:path（Windows 盘符 C:\ 不是这种形态，要求无反斜杠）
        match = re.fullmatch(
            r"(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?P<path>[^\s].*)", url
        )
        if not match or "\\" in url:
            raise RepoError("无法识别的仓库地址，请使用 ssh:// 或 git@host:path 形式")
        host = match.group("host")
        if host.startswith("-"):
            raise RepoError("非法的主机名")
        user = match.group("user") or "git"
        path = "/" + match.group("path").lstrip("/")
        target = GitTarget("ssh", host, None, user, path, f"{user}@{host}:{path.lstrip('/')}")

    _host_allowed(target.host)
    return target


# ---- 密钥（asyncssh，与运维域同一依赖）--------------------------------------


def generate_keypair_sync() -> Tuple[str, str]:
    """生成一对 ed25519 deploy key，返回 (OpenSSH 私钥 PEM, 公钥行)。

    用 asyncssh 而不是 shell out ssh-keygen：测试环境与 Windows 开发机上不保证
    有后者，且这是纯 CPU 的本地操作。镜像里仍装 openssh-client（git over SSH
    的传输靠 ssh 二进制）。
    """
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    private_pem = key.export_private_key(format_name="openssh").decode("utf-8")
    public_line = key.export_public_key().decode("utf-8").strip()
    return private_pem, public_line


def validate_private_key_sync(private_pem: str) -> Tuple[str, str]:
    """校验粘贴的私钥可读并规范化，返回 (OpenSSH PEM, 公钥行)。

    带 passphrase 的密钥在 ``BatchMode=yes`` 下必然挂起，直接拒绝。
    """
    import asyncssh

    try:
        key = asyncssh.import_private_key(private_pem.strip())
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise RepoError(f"私钥无法识别：{exc}") from exc

    try:
        canonical = key.export_private_key(format_name="openssh").decode("utf-8")
        public_line = key.export_public_key().decode("utf-8").strip()
    except asyncssh.KeyExportError as exc:  # pragma: no cover - 依赖侧异常分支
        raise RepoError("该私钥格式无法导出为 OpenSSH 格式") from exc
    if "ENCRYPTED" in private_pem or _key_passphrase_needed(private_pem):
        raise RepoError("不支持带 passphrase 的私钥，请使用无口令密钥")
    return canonical, public_line


def _key_passphrase_needed(private_pem: str) -> bool:
    # OpenSSH 新格式加密时头部附近带 AES 标记；asyncssh 导入成功基本意味着
    # 无口令，这里只兜 PEM 老格式里的 Proc-Type: 4,ENCRYPTED。
    return "Proc-Type: 4,ENCRYPTED" in private_pem


# ---- 主机指纹 ---------------------------------------------------------------


def known_host_fingerprints(known_hosts_text: str) -> List[str]:
    """从 known_hosts 文本算每个 key 的 ``SHA256:…`` 指纹（与 ssh-keygen -lf 同口径）。"""
    fingerprints: List[str] = []
    for line in known_hosts_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        # @cert-authority / @revoked 标记占第一格；hostlist 与 key 可能挤在一起，
        # 直接找「类型 + base64 blob」这对相邻 token。
        for index, token in enumerate(tokens[:-1]):
            if token in (
                "ssh-rsa",
                "ssh-ed25519",
                "ecdsa-sha2-nistp256",
                "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521",
                "ssh-dss",
            ) and re.fullmatch(r"[A-Za-z0-9+/=]{40,}", tokens[index + 1]):
                digest = hashlib.sha256(base64.b64decode(tokens[index + 1])).digest()
                fingerprints.append(
                    "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
                )
                break
    return fingerprints


# ---- 子进程 -----------------------------------------------------------------


# 只透传这几类环境变量；凭据走文件，环境里只出现文件路径，绝不带密钥内容。
_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)


def _git_env(
    *,
    ssh_command: Optional[str] = None,
    cwd_home: Optional[Path] = None,
    http_credentials: Optional[Path] = None,
) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV}
    if cwd_home is not None:
        # 把 HOME 指到仓库目录：git/ssh 不去翻部署机的 ~/.gitconfig 与
        # ~/.ssh/known_hosts，行为只由我们传入的参数决定。
        env["HOME"] = str(cwd_home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    # 关掉全局/系统配置的意外影响（insteadOf 改写、credential helper 等）。指向空
    # 设备而不是不存在的路径：git 打开读到 EOF 即视为空配置；Windows 上 /dev/null
    # 不存在，NUL 才是等价物（开发机是 Windows，生产是 Linux）。system 一并指空，
    # 这样下面注入的 helper 是唯一候选——不能用「空 VALUE 清空列表」技巧：空串
    # 环境变量在 Windows C 运行时里会被直接删掉，git 会报缺少 VALUE。
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    if http_credentials is not None:
        # 经 GIT_CONFIG_* 注入一个只认我们这个 store 文件的 credential helper。
        # 令牌只在那个 0600 文件里，argv（ps 可见）与 URL（git 报错会原样回显，
        # 进而进我们的 error_msg）都不带令牌。
        cred = http_credentials.as_posix()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = f"store --file={cred}"
    return env


def _ssh_command(key_path: Path, known_hosts_path: Path, *, accept_new: bool) -> str:
    strict = "accept-new" if accept_new else "yes"
    # 路径加双引号（git 按 shell 风格切分 GIT_SSH_COMMAND，认引号）；统一成
    # posix 斜杠形式，避免 Windows 反斜杠在引号里被当成转义符吃掉。
    key_arg = key_path.as_posix()
    known_arg = known_hosts_path.as_posix()
    return (
        f"ssh -i \"{key_arg}\" -o IdentitiesOnly=yes -o BatchMode=yes "
        f"-o StrictHostKeyChecking={strict} "
        f"-o UserKnownHostsFile=\"{known_arg}\" "
        f"-o HashKnownHosts=no -o ConnectTimeout=10 -o LogLevel=ERROR"
    )


async def _run(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RepoSyncError(f"命令超时（{timeout:.0f}s）：{args[0]} {args[1] if len(args) > 1 else ''}") from exc
    return proc.returncode or 0, stdout, stderr


def _git_error(stderr: bytes, prefix: str) -> RepoSyncError:
    text = stderr.decode("utf-8", errors="replace").strip()
    # 错误信息里可能带本地路径，保留；永远不该带密钥内容（走的是文件）。
    return RepoSyncError(f"{prefix}：{text[:400] or '未知错误'}")


def _write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 二进制写死 LF：Windows 文本模式会翻成 CRLF，credential-store 的主机名会
    # 带上尾部 \r 导致整行失配，OpenSSH 私钥同样不容忍 CRLF。
    path.write_bytes(content.encode("utf-8"))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:  # Windows 上 chmod 语义有限，部署目标是 Linux。
        pass


# ---- http(s) 令牌认证 ---------------------------------------------------------

# 令牌配套用户名缺省值：GitHub/GitLab 对用户名不挑，Gitee 等个别平台要真实用户名，
# 用户可在表单里显式覆盖。
DEFAULT_HTTP_USERNAME = "x-access-token"


def validate_http_token(token: Optional[str]) -> str:
    """令牌非空且不含控制字符（credential-store 是行格式，换行会变成字段注入）。

    控制字符检查在 strip 之前：否则 ``"tok\\n"`` 会被 strip 洗白。
    """
    if not token or not token.strip():
        raise RepoError("请提供个人访问令牌")
    if any(ord(char) < 32 for char in token):
        raise RepoError("令牌包含非法控制字符")
    return token.strip()


def normalize_http_username(username: Optional[str]) -> Optional[str]:
    """用户名可缺省（传输时退化为默认值）；同样拒绝控制字符。"""
    if username is None:
        return None
    if any(ord(char) < 32 for char in username):
        raise RepoError("用户名包含非法控制字符")
    cleaned = username.strip()
    return cleaned[:128] or None


def _write_http_credentials(
    path: Path, target: "GitTarget", username: Optional[str], token: str
) -> None:
    """写 git credential-store 文件（一行 URL 形态），按协议+主机(+端口)匹配。

    实测 git 2.5x 的 credential-store 只认 URL 形态（不是 key=value 块），令牌仅存于该文件；
    user/token 走 quote 百分号编码，含 ``@ : / + =`` 也能原样取回；远端 URL 与
    argv 都不含令牌，git 报错因此回显不出它。
    """
    user = quote(username or DEFAULT_HTTP_USERNAME, safe="")
    secret = quote(token, safe="")
    # 显式端口在主机里百分号编码冒号（git 自己 approve 时就写成 %3a）。
    host = target.host if not target.port else f"{target.host}%3a{target.port}"
    _write_secret_file(path, f"{target.protocol}://{user}:{secret}@{host}\n")


def _force_rmtree(path: Path) -> None:
    """删目录的兜底版 rmtree。

    ``.git`` 里的 pack/对象文件常带只读位：Windows 上只读文件删不掉，普通
    rmtree(ignore_errors=True) 会留下半个目录，随后的重新克隆直接撞
    "destination already exists"。错误回调里去只读位再试一次。
    """

    def retry(function, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            function(target)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except OSError as exc:
        logger.warning("failed to remove directory %s: %s", path, exc)


# ---- 目录布局 ----------------------------------------------------------------


def repo_root(owner_id: int, repo_id: int) -> Path:
    return Path(settings.KB_REPO_DATA_DIR) / str(owner_id) / str(repo_id)


def _prepare_root(owner_id: int, repo_id: int) -> Tuple[Path, Path, Path]:
    root = repo_root(owner_id, repo_id)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, stat.S_IRWXU)  # 0700
    except OSError:
        pass
    return root, root / "work", root / "id_key"


def rmtree_repo_dir(owner_id: int, repo_id: int) -> None:
    """删除整个仓库目录（含工作区与可能残留的密钥文件），best-effort。"""
    _force_rmtree(repo_root(owner_id, repo_id))


def gc_orphan_repo_dirs(db: Session) -> int:
    """清掉数据库里已经没有对应仓库行的目录（删类型/项目路径的收尸）。"""
    base = Path(settings.KB_REPO_DATA_DIR)
    if not base.exists():
        return 0
    removed = 0
    for owner_dir in base.iterdir():
        if not owner_dir.is_dir() or not owner_dir.name.isdigit():
            continue
        owner_id = int(owner_dir.name)
        for repo_dir in owner_dir.iterdir():
            if not repo_dir.is_dir() or not repo_dir.name.isdigit():
                continue
            repo_id = int(repo_dir.name)
            exists = db.scalar(
                select(KbRepo.id).where(KbRepo.owner_id == owner_id, KbRepo.id == repo_id)
            )
            if exists is None:
                _force_rmtree(repo_dir)
                removed += 1
    if removed:
        logger.info("garbage-collected %d orphan repo dir(s)", removed)
    return removed


# ---- 测试连接 ----------------------------------------------------------------


async def probe_remote(
    remote_url: str,
    *,
    private_key: Optional[str] = None,
    branch: Optional[str] = None,
    known_hosts_text: Optional[str] = None,
    http_token: Optional[str] = None,
    http_username: Optional[str] = None,
) -> Tuple[Optional[str], str, str, str, str]:
    """``git ls-remote`` 探测。

    返回 ``(默认分支, HEAD sha, host, known_hosts 文本, 指纹)``。SSH 首次用
    accept-new 把主机 key 收进临时 known_hosts；http(s) 后两项为空串。
    http(s) 给了令牌就写临时 credential-store 文件，随整个临时目录一起销毁。
    """
    target = parse_git_url(remote_url)
    import tempfile

    Path(settings.KB_REPO_DATA_DIR).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kbprobe-", dir=settings.KB_REPO_DATA_DIR) as tmp:
        tmp_dir = Path(tmp)
        env = _git_env(cwd_home=tmp_dir)
        cred_path: Optional[Path] = None
        if target.protocol == "ssh":
            if http_token:
                raise RepoError("SSH 地址不使用访问令牌")
            if not private_key:
                raise RepoError("SSH 地址必须提供 deploy 私钥")
            key_path = tmp_dir / "id_key"
            known_path = tmp_dir / "known_hosts"
            canonical, _ = await asyncio.to_thread(validate_private_key_sync, private_key)
            _write_secret_file(key_path, canonical)
            if known_hosts_text:
                known_path.write_text(known_hosts_text, encoding="utf-8")
                accept_new = False
            else:
                accept_new = True
            env = _git_env(
                ssh_command=_ssh_command(key_path, known_path, accept_new=accept_new),
                cwd_home=tmp_dir,
            )
        elif http_token:
            cred_path = tmp_dir / "http_cred"
            _write_http_credentials(
                cred_path,
                target,
                normalize_http_username(http_username),
                validate_http_token(http_token),
            )
            env = _git_env(cwd_home=tmp_dir, http_credentials=cred_path)

        args = ["git", "ls-remote", "--symref", target.canonical_url, "HEAD"]
        code, stdout, stderr = await _run(
            args, env=env, timeout=settings.KB_REPO_CLONE_TIMEOUT
        )
        if code != 0:
            raise _git_error(stderr, "连接 Git 仓库失败")
        output = stdout.decode("utf-8", errors="replace")
        default_branch = None
        head_sha = ""
        for line in output.splitlines():
            if line.startswith("ref: refs/heads/"):
                default_branch = line.split("ref: refs/heads/", 1)[1].split("\t", 1)[0]
            else:
                parts = line.split("\t", 1)
                if parts and parts[0].strip():
                    head_sha = parts[0].strip()
        if not head_sha:
            raise RepoSyncError("ls-remote 未返回 HEAD")

        known_text = ""
        fingerprint = ""
        if target.protocol == "ssh":
            known_text = known_path.read_text(encoding="utf-8") if known_path.exists() else ""
            fingerprint = ", ".join(known_host_fingerprints(known_text))
        return default_branch, head_sha, target.host, known_text, fingerprint


# ---- RepoService：行级 CRUD --------------------------------------------------


class RepoService:
    """绑定 owner 的仓库台账操作；越权读作 404（LookupError），同全站口径。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self.knowledge = KnowledgeService(db, owner_id)

    def _scope_query(self, stmt):
        return stmt.where(KbRepo.owner_id == self._owner_id)

    def get(self, repo_id: int) -> KbRepo:
        repo = self._db.scalar(
            self._scope_query(select(KbRepo)).where(KbRepo.id == repo_id)
        )
        if repo is None:
            raise LookupError("仓库不存在")
        return repo

    def get_by_type(self, type_id: int) -> Optional[KbRepo]:
        return self._db.scalar(
            self._scope_query(select(KbRepo)).where(KbRepo.type_id == type_id)
        )

    def list_for_project(self, project_id: int) -> List[KbRepo]:
        type_ids = self.knowledge.type_ids_of_project(project_id)
        if not type_ids:
            return []
        return list(
            self._db.scalars(
                self._scope_query(select(KbRepo))
                .where(KbRepo.type_id.in_(type_ids))
                .order_by(KbRepo.id.desc())
            ).all()
        )

    def list_all(self) -> List[KbRepo]:
        return list(
            self._db.scalars(
                self._scope_query(select(KbRepo)).order_by(KbRepo.id.desc())
            ).all()
        )

    @staticmethod
    def default_name(target: GitTarget) -> str:
        name = target.path.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name[:128] or "repo"

    def _unique_type_name(self, project_id: int, wanted: str) -> str:
        existing = {
            row[0]
            for row in self._db.execute(
                select(KnowledgeType.name).where(
                    KnowledgeType.owner_id == self._owner_id,
                    KnowledgeType.project_id == project_id,
                )
            ).all()
        }
        if wanted not in existing:
            return wanted
        suffix = 2
        while f"{wanted}-{suffix}" in existing:
            suffix += 1
        return f"{wanted}-{suffix}"

    def create(self, payload) -> KbRepo:
        target = parse_git_url(payload.remote_url)
        auth_type = payload.auth_type
        if auth_type not in ("ssh_key", "none", "http_token"):
            raise RepoError("未知的认证方式")

        private_enc = None
        public_key = None
        http_token_enc = None
        http_username = None
        if target.protocol in ("http", "https"):
            if auth_type == "ssh_key":
                raise RepoError("http(s) 地址不能使用 SSH 认证")
            if payload.private_key:
                raise RepoError("http(s) 仓库不需要 SSH 私钥")
            if auth_type == "http_token":
                http_token_enc = encrypt(validate_http_token(payload.http_token))
                http_username = normalize_http_username(payload.http_username)
            elif payload.http_token:
                # 匿名方式却带了令牌：拒绝而不是静默丢弃，避免用户以为令牌生效。
                raise RepoError("匿名访问不需要令牌，请选择「HTTPS 令牌」认证")
        elif auth_type == "ssh_key":
            if payload.http_token:
                raise RepoError("SSH 仓库不使用访问令牌")
            if not payload.private_key:
                raise RepoError("SSH 仓库必须提供 deploy 私钥")
            canonical, public_line = validate_private_key_sync(payload.private_key)
            private_enc = encrypt(canonical)
            public_key = public_line
        else:
            raise RepoError("SSH 地址必须使用 deploy key 认证")

        project = self.knowledge.get_project(payload.project_id)
        name = (payload.name or "").strip() or self.default_name(target)
        type_name = self._unique_type_name(project.id, name[:128])
        kb_type = KnowledgeType(
            owner_id=self._owner_id,
            project_id=project.id,
            name=type_name,
            description=f"代码仓库：{target.canonical_url}",
        )
        self._db.add(kb_type)
        self._db.flush()

        repo = KbRepo(
            owner_id=self._owner_id,
            type_id=kb_type.id,
            name=name[:128],
            auth_type=auth_type,
            remote_url=target.canonical_url,
            branch=(payload.branch or "").strip() or None,
            private_key_enc=private_enc,
            public_key=public_key,
            http_token_enc=http_token_enc,
            http_username=http_username,
            known_hosts=payload.known_hosts or None,
            host_fingerprint=payload.host_fingerprint or None,
            exclude_globs=(payload.exclude_globs or "").strip() or None,
            status=RepoStatus.PENDING,
        )
        self._db.add(repo)
        self._db.commit()
        self._db.refresh(repo)
        return repo

    def update(self, repo_id: int, payload) -> KbRepo:
        repo = self.get(repo_id)
        if payload.name is not None:
            stripped = payload.name.strip()
            if stripped:
                repo.name = stripped[:128]
        if payload.branch is not None:
            repo.branch = payload.branch.strip() or None
        if payload.exclude_globs is not None:
            repo.exclude_globs = payload.exclude_globs.strip() or None
        # remote_url 不可改，协议一开始就确定：凭据分支按它走。
        target = parse_git_url(repo.remote_url)
        if payload.auth_type is not None:
            if payload.auth_type not in ("ssh_key", "none", "http_token"):
                raise RepoError("未知的认证方式")
            repo.auth_type = payload.auth_type

        # SSH 私钥：缺省（None）=不变；非空=校验后替换。
        if payload.private_key:
            if target.protocol in ("http", "https"):
                raise RepoError("http(s) 仓库不使用 SSH 私钥")
            canonical, public_line = validate_private_key_sync(payload.private_key)
            repo.private_key_enc = encrypt(canonical)
            repo.public_key = public_line
            repo.auth_type = "ssh_key"

        # 访问令牌：None=不变；非空=校验后替换；空串=清空（配合切到匿名访问）。
        if payload.http_token is not None:
            if target.protocol in ("http", "https"):
                if payload.http_token:
                    repo.http_token_enc = encrypt(
                        validate_http_token(payload.http_token)
                    )
                    repo.auth_type = "http_token"
                else:
                    repo.http_token_enc = None
                    repo.http_username = None
                    if repo.auth_type == "http_token":
                        repo.auth_type = "none"
            elif payload.http_token:
                raise RepoError("SSH 仓库不使用访问令牌")
        if (
            payload.http_username is not None
            and target.protocol in ("http", "https")
            and repo.http_token_enc is not None
        ):
            # 只有还持有令牌时用户名才有意义；空串=回到默认用户名。
            repo.http_username = normalize_http_username(payload.http_username)

        # 协议与认证方式必须自洽，不能存出「SSH 地址却没有 key」这类同步必炸的行。
        if target.protocol in ("http", "https"):
            if repo.auth_type == "ssh_key":
                raise RepoError("http(s) 地址不能使用 SSH 认证")
            if repo.auth_type == "http_token":
                if repo.http_token_enc is None:
                    raise RepoError("请先提供个人访问令牌")
            else:
                # 切到匿名：令牌不允许残留。
                repo.auth_type = "none"
                repo.http_token_enc = None
                repo.http_username = None
        elif repo.auth_type != "ssh_key" or repo.private_key_enc is None:
            raise RepoError("SSH 地址必须保留或重新提供 deploy 私钥")
        self._db.commit()
        self._db.refresh(repo)
        return repo

    def retrust(self, repo: KbRepo, known_hosts_text: str, fingerprint: str) -> None:
        repo.known_hosts = known_hosts_text
        repo.host_fingerprint = fingerprint[:255]
        repo.error_msg = None
        self._db.commit()

    def delete(self, repo_id: int) -> Tuple[int, List[int]]:
        """删仓库 = 删它绑定的类型（文档/分片级联全走既有路径）。返回 doc_ids。"""
        repo = self.get(repo_id)
        type_id = repo.type_id
        doc_ids = self.knowledge.delete_type(type_id)
        # 正常情况下 delete_type 的级联已经把仓库行删掉（见 service 内清理）；
        # 这里不依赖该顺序，显式兜一次。
        self._db.execute(delete(KbRepo).where(KbRepo.id == repo.id))
        self._db.commit()
        return repo.id, doc_ids

    def project_id_of(self, repo: KbRepo) -> Optional[int]:
        return self._db.scalar(
            select(KnowledgeType.project_id).where(KnowledgeType.id == repo.type_id)
        )

    def to_item(self, repo: KbRepo) -> dict:
        result = repo.last_sync_result
        return {
            "id": repo.id,
            "type_id": repo.type_id,
            "project_id": self.project_id_of(repo),
            "name": repo.name,
            "auth_type": repo.auth_type,
            "remote_url": repo.remote_url,
            "branch": repo.branch,
            # 私钥或令牌任一在库即视为已配置凭据；具体类型看 auth_type。
            "has_credential": bool(repo.private_key_enc or repo.http_token_enc),
            "public_key": repo.public_key,
            "host_fingerprint": repo.host_fingerprint,
            "exclude_globs": repo.exclude_globs,
            "status": repo.status,
            "error_msg": repo.error_msg,
            "head_commit": repo.head_commit,
            "last_sync_at": repo.last_sync_at,
            "last_sync_result": RepoSyncResult(**result) if result else None,
            "created_at": repo.created_at,
            "updated_at": repo.updated_at,
        }


# ---- 同步 -------------------------------------------------------------------

# 与 _doc_index_locks 同一形态、同一单 worker 口径。
_repo_locks: Dict[int, asyncio.Lock] = {}
_running: set[int] = set()


def is_sync_running(repo_id: int) -> bool:
    return repo_id in _running


async def _git_origin_url(work_dir: Path, env) -> Optional[str]:
    code, stdout, _ = await _run(
        ["git", "-C", str(work_dir), "remote", "get-url", "origin"],
        env=env,
        timeout=30.0,
    )
    return stdout.decode().strip() if code == 0 else None


async def _fresh_clone(
    target: GitTarget, work_dir: Path, env: dict, branch: Optional[str]
) -> Tuple[int, bytes, bool]:
    """浅克隆到 work_dir。指定分支克隆失败时退回远端默认分支（分支可能被改名）。

    返回 ``(returncode, stderr, used_remote_default)``：第二次仍失败时调用方把
    stderr 抛给用户；``used_remote_default`` 为 True 表示最终落在远端默认分支上，
    调用方应重新解析并持久化分支名。
    """
    base_args = ["git", "clone", "--depth", "1", "--single-branch"]
    args = base_args + (["--branch", branch] if branch else [])
    args += [target.canonical_url, str(work_dir)]
    code, _, stderr = await _run(
        args, env=env, timeout=settings.KB_REPO_CLONE_TIMEOUT
    )
    if code == 0 or not branch:
        return code, stderr, False
    logger.warning(
        "clone of branch %r failed, retrying with remote default branch: %s",
        branch,
        stderr.decode("utf-8", errors="replace").strip()[:300],
    )
    _force_rmtree(work_dir)
    args = base_args + [target.canonical_url, str(work_dir)]
    code, _, stderr = await _run(
        args, env=env, timeout=settings.KB_REPO_CLONE_TIMEOUT
    )
    return code, stderr, code == 0


async def _materialize_worktree(
    *, target: GitTarget, work_dir: Path, env: dict, branch: Optional[str]
) -> bool:
    """让 work_dir 停在远端最新提交：优先增量 fetch，任何异常都退回全新克隆。

    退回克隆覆盖三类自愈场景：工作区损坏、origin 地址漂移、远端分支被改名
    （后者在克隆时再退一次默认分支）。浅克隆很便宜，正适合拿重克隆当兜底。

    返回 ``used_remote_default``：最终是按远端默认分支克隆的（说明记录的分支名
    已失效），调用方需要重新解析并持久化当前分支。
    """
    if work_dir.exists():
        origin = await _git_origin_url(work_dir, env)
        if origin == target.canonical_url:
            ref = branch or "HEAD"
            code, _, stderr = await _run(
                ["git", "-C", str(work_dir), "fetch", "--depth", "1", "origin", ref],
                env=env,
                timeout=settings.KB_REPO_CLONE_TIMEOUT,
            )
            if code == 0:
                code, _, stderr = await _run(
                    ["git", "-C", str(work_dir), "reset", "--hard", "FETCH_HEAD"],
                    env=env,
                    timeout=60.0,
                )
            if code == 0:
                return False
            logger.warning(
                "incremental fetch failed, falling back to a fresh clone: %s",
                stderr.decode("utf-8", errors="replace").strip()[:300],
            )

    _force_rmtree(work_dir)
    code, stderr, used_remote_default = await _fresh_clone(
        target, work_dir, env, branch
    )
    if code != 0:
        raise _git_error(stderr, "克隆仓库失败")
    return used_remote_default


async def sync_repository(owner_id: int, repo_id: int) -> RepoSyncResult:
    """同步一个仓库。调用方必须持有该 repo 的锁（见 :func:`run_repo_sync`）。"""
    started = time.monotonic()
    with SessionLocal() as db:
        service = KnowledgeService(db, owner_id)
        repo = db.scalar(
            select(KbRepo).where(KbRepo.owner_id == owner_id, KbRepo.id == repo_id)
        )
        if repo is None:
            raise RepoSyncError("仓库不存在")
        config = service.require_embedding_config()

        repo.status = RepoStatus.SYNCING
        repo.error_msg = None
        db.commit()

        root, work_dir, key_path = _prepare_root(owner_id, repo_id)
        known_hosts_path = root / "known_hosts"
        cred_path = root / "http_cred"
        env = _git_env(cwd_home=root)
        ssh_accept_new = False
        target = parse_git_url(repo.remote_url)
        try:
            if repo.auth_type == "ssh_key":
                if not repo.private_key_enc:
                    raise RepoSyncError("仓库缺少 deploy 私钥")
                _write_secret_file(key_path, decrypt(repo.private_key_enc))
                if repo.known_hosts:
                    known_hosts_path.write_text(repo.known_hosts, encoding="utf-8")
                else:
                    # 没走过测试连接就直接接入：首克隆现场 TOFU。
                    ssh_accept_new = True
                env = _git_env(
                    ssh_command=_ssh_command(
                        key_path, known_hosts_path, accept_new=ssh_accept_new
                    ),
                    cwd_home=root,
                )
            elif repo.auth_type == "http_token":
                if not repo.http_token_enc:
                    raise RepoSyncError("仓库缺少访问令牌")
                _write_http_credentials(
                    cred_path,
                    target,
                    repo.http_username,
                    decrypt(repo.http_token_enc),
                )
                env = _git_env(cwd_home=root, http_credentials=cred_path)

            try:
                used_remote_default = await _materialize_worktree(
                    target=target,
                    work_dir=work_dir,
                    env=env,
                    branch=repo.branch,
                )

                # 首次连接现场收 host key。
                if ssh_accept_new and known_hosts_path.exists():
                    known_text = known_hosts_path.read_text(encoding="utf-8")
                    if known_text:
                        repo.known_hosts = known_text
                        fingerprints = known_host_fingerprints(known_text)
                        if fingerprints:
                            repo.host_fingerprint = ", ".join(fingerprints)[:255]

                code, stdout, stderr = await _run(
                    ["git", "-C", str(work_dir), "rev-parse", "HEAD"],
                    env=env,
                    timeout=30.0,
                )
                if code != 0:
                    raise _git_error(stderr, "读取 HEAD 失败")
                head_sha = stdout.decode().strip()

                # 首次同步解析默认分支；分支改名自愈后也要把新名字落库，否则下次
                # 同步还会先拿旧名 fetch 失败、再走一遍重克隆。
                if not repo.branch or used_remote_default:
                    code, ref, _ = await _run(
                        ["git", "-C", str(work_dir), "symbolic-ref", "--short", "HEAD"],
                        env=env,
                        timeout=30.0,
                    )
                    current_branch = ref.decode().strip() if code == 0 else ""
                    if current_branch and current_branch != repo.branch:
                        if repo.branch:
                            logger.warning(
                                "repo %s branch renamed remotely: %r -> %r",
                                repo_id,
                                repo.branch,
                                current_branch,
                            )
                        repo.branch = current_branch

                entries = await _ls_tree(work_dir, env, head_sha)
                user_globs = code_chunk.parse_user_globs(repo.exclude_globs)
                result = await _reconcile(
                    service=service,
                    repo=repo,
                    work_dir=work_dir,
                    git_env=env,
                    entries=entries,
                    user_globs=user_globs,
                    config=config,
                )
                result.commit = head_sha
                result.duration_ms = int((time.monotonic() - started) * 1000)

                repo.head_commit = head_sha
                repo.last_sync_at = db_now()
                repo.last_sync_result = result.model_dump()
                repo.status = RepoStatus.READY
                repo.error_msg = None
                db.commit()
                logger.info(
                    "synced repo %s for owner %s @ %s: +%s ~%s -%s skip%s, %s chunks",
                    repo_id,
                    owner_id,
                    head_sha[:8],
                    result.added,
                    result.changed,
                    result.removed,
                    result.skipped,
                    result.indexed_chunks,
                )
                return result
            finally:
                # 私钥/令牌文件无论成败都必须消失。
                key_path.unlink(missing_ok=True)
                cred_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.exception("repo sync failed: owner=%s repo=%s", owner_id, repo_id)
            repo.status = RepoStatus.ERROR
            repo.error_msg = str(exc)[:512]
            db.commit()
            key_path.unlink(missing_ok=True)
            cred_path.unlink(missing_ok=True)
            raise


async def _ls_tree(
    work_dir: Path, env: dict, head_sha: str
) -> List[Tuple[str, str, str]]:
    """``ls-tree -r -z`` → [(mode, blob_sha, path)]；只留普通 blob。"""
    code, stdout, stderr = await _run(
        ["git", "-C", str(work_dir), "ls-tree", "-r", "-z", head_sha],
        env=env,
        timeout=120.0,
    )
    if code != 0:
        raise _git_error(stderr, "读取仓库树失败")
    entries: List[Tuple[str, str, str]] = []
    for record in stdout.split(b"\x00"):
        if not record:
            continue
        meta, _, path_bytes = record.partition(b"\t")
        parts = meta.decode().split()
        if len(parts) != 3:
            continue
        mode, kind, sha = parts
        if kind != "blob":
            continue  # submodule(commit) 跳过
        if mode == "120000":
            continue  # 符号链接跳过
        path = path_bytes.decode("utf-8", errors="replace")
        if not _safe_rel_path(path):
            logger.warning("skipping suspicious path from ls-tree: %r", path)
            continue
        entries.append((mode, sha, path))
    return entries


def _safe_rel_path(path: str) -> bool:
    if path.startswith(("/", "\\")):
        return False
    parts = re.split(r"[\\/]", path)
    return not any(part in ("", "..") for part in parts)


async def _read_tree_file(work_dir: Path, env: dict, sha: str) -> bytes:
    code, stdout, stderr = await _run(
        ["git", "-C", str(work_dir), "cat-file", "blob", sha],
        env=env,
        timeout=60.0,
    )
    if code != 0:
        raise _git_error(stderr, f"读取 blob {sha[:8]} 失败")
    return stdout


async def _reconcile(
    *,
    service: KnowledgeService,
    repo: KbRepo,
    work_dir: Path,
    git_env: dict,
    entries: Sequence[Tuple[str, str, str]],
    user_globs: Sequence[str],
    config,
) -> RepoSyncResult:
    """三路对账：added / changed / removed / unchanged（后者零 embedding）。"""
    result = RepoSyncResult()

    existing = service.repo_doc_index(repo.id)  # path -> (doc, blob_hash)
    incoming = {path: sha for _, sha, path in entries}
    # 新命中排除规则（用户新加 glob、或内置规则随版本变严）的在树文件也要当
    # removed 处理；否则下面的 unchanged 短路会让旧账永久残留。
    excluded_now = {
        path for path in incoming if code_chunk.is_excluded(path, user_globs)
    }

    # removed：账上有、树上没有；或账上有、但现在命中排除规则。
    for path, (doc, _sha) in existing.items():
        if path not in incoming or path in excluded_now:
            doc_id = doc.id
            service.delete_document(doc_id)
            await vectorstore.delete_by_doc(service.collection, service.owner_id, doc_id)
            result.removed += 1

    candidates = entries
    truncated = len(entries) > settings.KB_REPO_MAX_FILES
    for position, (_mode, sha, path) in enumerate(candidates):
        if position >= settings.KB_REPO_MAX_FILES:
            result.skipped += 1
            continue
        prior = existing.get(path)
        if prior is not None and prior[1] == sha:
            continue  # unchanged：省钱的关键，blob 没变就不碰 embedding。

        try:
            if len(path) > 512:
                raise code_chunk.SourceSkip("路径超过 512 字符")
            raw = await _read_tree_file(work_dir, git_env, sha)
            language, text = code_chunk.inspect_source(
                path,
                raw,
                max_file_bytes=settings.KB_REPO_MAX_FILE_BYTES,
                user_globs=user_globs,
            )
        except code_chunk.SourceSkip as exc:
            logger.debug("repo %s skip %s: %s", repo.id, path, exc)
            result.skipped += 1
            # 旧版本里这文件曾被索引、新版本变成该跳过（生成文件等）：删掉旧账。
            if prior is not None:
                doc_id = prior[0].id
                service.delete_document(doc_id)
                await vectorstore.delete_by_doc(
                    service.collection, service.owner_id, doc_id
                )
                result.removed += 1
            continue
        except RepoSyncError:
            raise

        if prior is None:
            doc = service.create_repo_document(
                type_id=repo.type_id,
                repo_id=repo.id,
                rel_path=path,
                content=text,
                size=len(raw),
                blob_hash=sha,
                language=language,
            )
            result.added += 1
        else:
            doc = prior[0]
            service.update_repo_document(
                doc, content=text, size=len(raw), blob_hash=sha, language=language
            )
            result.changed += 1

        # 与批量重建同口径：逐文件加文档锁，异常只炸这一篇，不拖垮整次同步。
        try:
            async with service.doc_lock(doc.id):
                count = await service.index_document(doc, config)
            result.indexed_chunks += count
        except Exception as exc:  # noqa: BLE001 - 单文件失败落成该文件 error
            logger.warning("indexing repo file %s failed: %s", path, exc)
            service.set_document_status(doc, DocumentStatus.ERROR, error=str(exc))
            result.skipped += 1

    if truncated:
        logger.warning(
            "repo %s exceeded KB_REPO_MAX_FILES=%s", repo.id, settings.KB_REPO_MAX_FILES
        )
    return result


# ---- 后台任务外壳 ------------------------------------------------------------


async def run_repo_sync(owner_id: int, repo_id: int) -> bool:
    """同步入口（API 与测试都走这里）：锁占用返回 False，不排队。"""
    if repo_id in _running:
        return False
    lock = _repo_locks.setdefault(repo_id, asyncio.Lock())
    async with lock:
        if repo_id in _running:
            return False
        _running.add(repo_id)
        try:
            await asyncio.wait_for(
                sync_repository(owner_id, repo_id),
                timeout=settings.KB_REPO_SYNC_TIMEOUT,
            )
        finally:
            _running.discard(repo_id)
    return True


async def repo_sync_task(owner_id: int, repo_id: int) -> None:
    """BackgroundTask 形态：异常自己兜住，结果已落在仓库行的状态上。"""
    try:
        await run_repo_sync(owner_id, repo_id)
    except asyncio.TimeoutError:
        logger.error("repo sync timed out: owner=%s repo=%s", owner_id, repo_id)
        with SessionLocal() as db:
            _mark_error(db, owner_id, repo_id, "同步总超时，请缩小仓库范围或稍后重试")
    except Exception as exc:  # noqa: BLE001 - sync_repository 已落状态，这里防逃逸
        logger.warning("repo_sync_task suppressed: %s", exc)


def _mark_error(db: Session, owner_id: int, repo_id: int, message: str) -> None:
    repo = db.scalar(
        select(KbRepo).where(KbRepo.owner_id == owner_id, KbRepo.id == repo_id)
    )
    if repo is not None and repo.status == RepoStatus.SYNCING:
        repo.status = RepoStatus.ERROR
        repo.error_msg = message[:512]
        db.commit()


def reset_stale_repo_syncs(db: Session) -> int:
    """启动时把卡在 syncing 的仓库行标成可重试的 error（与 reset_stale_indexing 同理）。"""
    stale = list(db.scalars(select(KbRepo).where(KbRepo.status == RepoStatus.SYNCING)).all())
    for repo in stale:
        repo.status = RepoStatus.ERROR
        repo.error_msg = "同步任务因服务进程中断而中止，请重新同步"
    if stale:
        db.commit()
        logger.warning("reset %s repo sync(s) stuck in syncing", len(stale))
    return len(stale)
