"""AI 网关业务包：管理侧的配置与留痕，加上对外转发的兼容层。

两个模块的分工是「管」和「通」：``service`` 管通道、模型路由、统一密钥、调用
日志与统计，写操作全部由 ``require_admin`` 把关；``proxy`` 管对外那套 OpenAI
兼容端点的转发——挑候选通道、故障转移、流式回放、把上游用量落进调用日志。
拆开是因为它们改的理由不同：加一种端点形态动 ``proxy``，调密钥规则或统计口径
动 ``service``。

依赖单向：``proxy`` 接 ``service`` 的候选解析与落日志，``service`` 谁都不接。

跟 knowledge/slides 那两个包比，这里的门面多转发了一些：``service`` 除了入口类
``AiGatewayService`` 还有一把平铺的公开函数与常量（密钥格式、URL 归一化、候选
解析、留痕写入），两个 router 和两个测试文件都在直接取它们，所以整串重导出，
调用方仍是从包名取名字。``proxy`` 只作为**子模块名**露出来，router 用
``proxy.X`` 属性访问，门面不替它转发。logger 名 ``yangvis.ai_gateway_proxy``
保持原样——那是日志通道名，不是模块路径。
"""
from app.services.ai_gateway.service import (  # noqa: F401
    DISPLAY_PREFIX_LENGTH,
    KEY_PREFIX,
    TRUNCATE_MARK,
    AiGatewayService,
    RouteCandidate,
    display_prefix,
    extract_bearer_key,
    find_api_key,
    generate_api_key,
    hash_api_key,
    list_public_models,
    normalize_base_url,
    public_base_url,
    resolve_candidates,
    truncate_text,
    write_call_log,
)
from app.services.ai_gateway import proxy  # noqa: F401
