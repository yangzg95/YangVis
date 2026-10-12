"""MCP 业务包：服务定义的存取与配置导入导出，加上工作流用的连接束。

两个模块按「存」和「连」分工：``servers`` 管 ``McpServer`` 的读写、按 owner 隔离、
密钥的 Fernet 落库与 dict 粒度掩码、以及 mcp.json 形态配置的解析与导出；
``tools`` 管一次工作流运行期间的连接束——每个服务各建一条传输加一个
``ClientSession``，经 langchain-mcp-adapters 转成 LangChain 工具后聚合、运行结束
统一关闭。拆开是因为改的理由不同：加一种传输形态动 ``tools``，改凭据掩码或导入
导出口径动 ``servers``。

依赖单向：``tools`` 接 ``servers`` 的三个 TRANSPORT 常量，``servers`` 谁都不接。

门面按 knowledge 那刀的口径转发 ``servers`` 的公开名（域的那张脸），``tools`` 只露
子模块名，调用方按 ``app.services.mcp.tools`` 取名字——它是同级的第二个实现模块，
不是入口类的附件。两个 logger 名 ``yangvis.mcp_servers`` / ``yangvis.mcp_tools``
保持原样，它们是日志通道不是模块路径。

包名与第三方 ``mcp`` 库同名，但两边不会互相遮蔽：包的全路径是
``app.services.mcp``，而 ``tools`` 里 ``from mcp import ClientSession`` 走的是绝对
导入，拿到的始终是顶层那个第三方包。
"""
from app.services.mcp import tools  # noqa: F401
from app.services.mcp.servers import (  # noqa: F401
    TRANSPORT_HTTP,
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    McpServerService,
    export_document,
    merge_secret,
    parse_mcp_config,
    read_secret,
    validate_transport_fields,
)
