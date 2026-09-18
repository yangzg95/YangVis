"""通过 pydantic-settings 管理应用配置。"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量 / .env 文件加载的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用
    APP_NAME: str = "yangvis"
    PORT: int = 18099
    CONTEXT_PATH: str = "/tool"

    # 数据库
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = "password"
    DB_NAME: str = "noetix_tool"

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_TIMEOUT: float = 20.0

    # 知识库入库相关限制。
    KB_MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB
    KB_EMBED_BATCH_SIZE: int = 16
    # 余弦分数低于该阈值的命中结果会在拼 prompt 之前被丢弃。
    # 基于弱相关的文本作答，比什么都不说更糟糕。
    KB_SCORE_THRESHOLD: float = 0.3

    # 认证（自建；见 docs/智能客服-RAG-设计方案.md §7）
    #
    # AUTH_JWT_SECRET 故意不给可用的默认值：值为空时 create_app() 会在启动阶段
    # 立刻失败。一旦提供默认值，它迟早会在某个环境里被原样留下，那时任何人都能
    # 自行签发管理员 token。
    AUTH_JWT_SECRET: str = ""
    AUTH_JWT_ALGORITHM: str = "HS256"
    AUTH_TOKEN_TTL: int = 43200  # 秒，12 小时

    # 可选的初始管理员，仅在 sys_user 表为空时使用。
    BOOTSTRAP_ADMIN_USERNAME: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

    # 用于保护已存储的模型 API key 的 Fernet 密钥。规则与 JWT secret 相同：
    # 不设默认值，因为一旦它被悄悄轮换，所有已存储的 key 都将无法解密。
    ENCRYPTION_KEY: str = ""

    # 调用用户自行配置的模型服务商时的出站请求设置。
    MODEL_HTTP_TIMEOUT: float = 30.0

    # 智能运维
    OPS_SSH_TIMEOUT: float = 15.0
    OPS_CMD_TIMEOUT: float = 20.0
    # AI 运维助手的单条命令超时。du/find 这类盘点命令在大磁盘上动辄一两分钟，
    # 用交互通道的 20 秒会被误杀；它只作用于 AI 通道，交互终端不受影响。
    OPS_AGENT_CMD_TIMEOUT: float = 120.0
    # 单次命令回传给模型的输出上限。终端里人看多少都行，但把几 MB 的日志塞进
    # 上下文既烧钱又会把有用的信息挤掉。
    OPS_OUTPUT_LIMIT: int = 8192
    OPS_SQL_ROW_LIMIT: int = 200
    # WebSocket 入场票的有效期。它只需要活到握手完成，给 60 秒已经很宽裕。
    OPS_WS_TICKET_TTL: int = 60
    # AI 提议写操作后等待用户点确认的时间。超时按「未批准」处理。
    OPS_CONFIRM_TIMEOUT: float = 180.0
    # SFTP 远程文件浏览器。上传按块流式读入（Starlette 对超 1MB 的 multipart
    # 自动落临时盘），这个上限是边传边计数的字节上限，不再是内存上限；
    # 单请求内存占用约为 OPS_SFTP_CHUNK_SIZE × OPS_SFTP_PIPELINE_REQUESTS。
    OPS_SFTP_MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024 * 1024
    # 单次列目录的条目上限，超出截断并标记 truncated，防止巨型目录拖垮面板。
    OPS_SFTP_LIST_LIMIT: int = 2000
    # 上传/下载流式传输的块大小。不得调大：OpenSSH 收到超过 256KB 的单条
    # SFTP 消息会直接断开连接（写路径靠 asyncssh 按服务端 write_len 自动再
    # 切分兜底）。
    OPS_SFTP_CHUNK_SIZE: int = 256 * 1024
    # SFTP 读写窗口内的并发请求数。和 CHUNK_SIZE 一起决定单条传输的内存上界
    # （默认 8 × 256KB = 2MB），也决定高延迟链路下的吞吐。
    OPS_SFTP_PIPELINE_REQUESTS: int = 8

    # 智能办公 · 百度网盘（xpan 开放平台）。三项任一为空白 = 网盘功能未启用，
    # 前端会隐藏绑定入口。AppKey/SecretKey 在开放平台控制台申请；APP_NAME 是
    # 控制台里的应用目录名，用户的文件只能落在网盘 /apps/<APP_NAME>/ 之下。
    BAIDU_NETDISK_APP_KEY: str = ""
    BAIDU_NETDISK_SECRET_KEY: str = ""
    BAIDU_NETDISK_APP_NAME: str = ""
    # oob = 用户在授权页手动复制授权码回来粘贴，不需要公网回调地址。
    BAIDU_NETDISK_REDIRECT_URI: str = "oob"

    @property
    def database_url(self) -> str:
        # 凭据做 percent-encode：密码里若包含 '@'、'/' 或 ':'，否则会被解析成
        # host 的一部分，报出来的是让人摸不着头脑的 DNS 失败而不是认证错误。
        user = quote_plus(self.DB_USER)
        password = quote_plus(self.DB_PASSWORD)
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
