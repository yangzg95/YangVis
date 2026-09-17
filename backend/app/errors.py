"""带有业务含义错误码的应用级异常。"""
from __future__ import annotations

# 保留错误码，与 SPA 保持一致。
CODE_EMBEDDING_NOT_READY = 4001
CODE_INDEX_MODEL_MISMATCH = 4002
CODE_KB_EMPTY = 4003
CODE_CHAT_NOT_READY = 4004

# 智能运维
CODE_OPS_FORBIDDEN = 4101
CODE_OPS_CONNECT_FAILED = 4102
CODE_OPS_EXEC_FAILED = 4103

# 智能办公 · 百度网盘
CODE_NETDISK_NOT_CONFIGURED = 4201
CODE_NETDISK_NOT_BOUND = 4202
CODE_NETDISK_API_ERROR = 4203


class BusinessError(Exception):
    """带有前端可识别的错误码的业务拒绝（非程序缺陷）。

    特意以 HTTP 200 + 响应体中非零 ``code`` 的形式返回。SPA 的 axios 拦截器
    只在 2xx 响应里检查信封结构；4xx 会落入通用的网络错误分支，丢失 code，
    而「去配置一个 embedding model」这个流程恰好需要 code 来做判断。
    """

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.msg = message
        super().__init__(message)
