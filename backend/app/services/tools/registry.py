"""本地工具注册表：把后端代码里的 Python 函数注册成模型可调用的工具。

本地工具与 MCP 服务是对话可选工具的两个来源：本地工具以**代码为唯一事实源**，
不入库、没有后台 CRUD——在 ``services/tools/builtins/`` 里丢一个文件，写一个带
类型注解和 Google 风格 docstring 的函数，加上 ``@local_tool(title=...)``，重启
即被自动发现。目录接口（``routers/tools.py``）只读地展示这里注册了什么。

几条硬约定：

- 函数名就是给模型的工具名，只允许 ``^[a-z0-9_]+$``；langchain 自己不校验名字，
  这道正则由我们在注册时卡住，重名/与内置工具撞名直接报错。
- 面向模型的参数必须有类型注解；需要执行上下文时把**第一个**参数声明为
  :class:`ToolContext`（按类型识别），它不会出现在模型可见的参数表里。
- 工具需要读写库时，绝不要复用请求级 session：异步工具里用
  ``await asyncio.to_thread(...)`` + 自开 ``app.database.SessionLocal()``
  （同步工具本身已在线程池执行）。``ToolContext`` 因此只给 owner/会话 id。
"""
from __future__ import annotations

import collections.abc
import importlib
import inspect
import json
import logging
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, get_type_hints

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

from app.services.agent_run import TOOL_OUTPUT_LIMIT

logger = logging.getLogger("yangvis.local_tools")

# 与工作流 MCP 通道（agent_run.TOOL_OUTPUT_LIMIT）同一口径：同一个工具在对话与
# 工作流里的返回不该一边截断一边不截断。
_TRUNCATE_NOTICE = "…（工具输出过长，已截断）"

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_NAME_MAX = 64

# 已经被对话内置工具占用的名字（时间/知识库检索/运维工具箱）。本地工具与它们
# 撞名会让模型面对两个同名工具，注册时直接拒绝。
RESERVED_TOOL_NAMES = frozenset(
    {
        # current_datetime 自己也是注册制本地工具（builtins/datetime_tools.py），
        # 不在保留集合里。
        "search_knowledge_base",
        "list_servers",
        "run_server_command",
        "run_readonly_command",
        "propose_command",
        "list_databases",
        "run_database_query",
        "run_query",
        "list_tables",
        "describe_table",
        "redis_command",
    }
)


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的执行上下文。只给身份，不给请求级 session。"""

    owner_id: int
    conversation_id: int


@dataclass(frozen=True)
class ToolParamSpec:
    name: str
    description: str
    json_type: str
    required: bool
    default: Any = None


@dataclass(frozen=True)
class ToolSpec:
    key: str
    title: str
    description: str
    fn: Callable[..., Any]
    is_async: bool
    takes_ctx: bool
    params: List[ToolParamSpec]
    args_schema: type[BaseModel]
    source: str


_REGISTRY: Dict[str, ToolSpec] = {}
_DISCOVERED: set[str] = set()

# app 包目录：来源定位据此截成 services/tools/...（不带 app/ 前缀）。
_APP_ROOT = Path(__file__).resolve().parents[2]


# ---- docstring / 注解解析 -----------------------------------------------------


def _parse_docstring(fn: Callable[..., Any]) -> tuple[str, Dict[str, str]]:
    """拆 Google 风格 docstring：首段作描述，``Args:`` 段取参数说明。

    只认最朴素的 ``name: 说明`` 行；延续行、Returns 段等一律忽略——内置工具的
    参数说明都在一行内，复杂文档留给代码注释。
    """
    raw = inspect.getdoc(fn)
    if not raw:
        raise ValueError(f"工具函数 {fn.__name__} 缺少 docstring（首段要写给模型看的说明）")

    lines = raw.splitlines()
    summary_lines: List[str] = []
    param_docs: Dict[str, str] = {}
    # head：docstring 首段（描述）；args：Args 段；tail：之后的一切（忽略）。
    phase = "head"
    for line in lines:
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:"):
            phase = "args"
            continue
        if phase == "head":
            if not stripped:
                if summary_lines:
                    phase = "tail"
            else:
                summary_lines.append(stripped)
        elif phase == "args":
            # 下一个同级段落（顶格的非空行，如 Returns:）结束 Args 段。
            if line and not line[0].isspace() and stripped:
                phase = "tail"
                continue
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.+?)\s*$", line)
            if match:
                param_docs[match.group(1)] = match.group(2)

    description = " ".join(summary_lines).strip()
    if not description:
        raise ValueError(f"工具函数 {fn.__name__} 的 docstring 首段为空")
    return description, param_docs


def _json_type(annotation: Any) -> str:
    """Python 注解 → 目录展示用的 JSON 类型粗映射，无法辨认的按 string 兜底。"""
    origin = getattr(annotation, "__origin__", None)
    if annotation is int:
        return "integer"
    if annotation is bool:
        return "boolean"
    if annotation is float:
        return "number"
    if annotation in (list, List) or origin in (list, collections.abc.Sequence):
        return "array"
    if annotation in (dict, Dict) or origin is dict:
        return "object"
    return "string"


# ---- 注册 ---------------------------------------------------------------------


def _build_spec(fn: Callable[..., Any], title: str) -> ToolSpec:
    key = fn.__name__
    if not _NAME_RE.fullmatch(key) or len(key) > _NAME_MAX:
        raise ValueError(
            f"本地工具名 {key!r} 不合法：只能用小写字母、数字、下划线，且不超过 "
            f"{_NAME_MAX} 个字符"
        )
    if key in RESERVED_TOOL_NAMES:
        raise ValueError(f"本地工具名 {key} 与对话内置工具冲突，请换一个名字")
    if not title.strip():
        raise ValueError(f"本地工具 {key} 缺少中文标题 title")

    description, param_docs = _parse_docstring(fn)
    try:
        hints = get_type_hints(fn)
    except Exception as exc:  # noqa: BLE001 - 注解里有解析不了的前向引用也要报明白
        raise ValueError(f"本地工具 {key} 的类型注解无法解析：{exc}") from exc

    signature = inspect.signature(fn)
    takes_ctx = False
    params: List[ToolParamSpec] = []
    schema_fields: Dict[str, Any] = {}

    for index, (name, parameter) in enumerate(signature.parameters.items()):
        annotation = hints.get(name)
        if index == 0 and annotation is ToolContext:
            takes_ctx = True
            continue
        if name == "self":
            continue
        if annotation is None:
            raise ValueError(
                f"本地工具 {key} 的参数 {name!r} 缺少类型注解：模型可见的每个参数"
                "都必须标注类型（int/str/float/bool/list/dict）"
            )

        has_default = parameter.default is not inspect.Parameter.empty
        param_description = param_docs.get(name, "")
        params.append(
            ToolParamSpec(
                name=name,
                description=param_description,
                json_type=_json_type(annotation),
                required=not has_default,
                default=parameter.default if has_default else None,
            )
        )
        field_info = Field(description=param_description) if param_docs else Field()
        if has_default:
            field_info = Field(default=parameter.default, description=param_description)
        schema_fields[name] = (annotation, field_info)

    args_schema = create_model(f"{key}_Args", **schema_fields)

    try:
        source_file = inspect.getsourcefile(fn) or ""
        source_rel = Path(source_file).resolve().relative_to(_APP_ROOT)
        source = f"{source_rel.as_posix()}:{fn.__code__.co_firstlineno}"
    except (OSError, ValueError):
        source = "<unknown>"

    return ToolSpec(
        key=key,
        title=title.strip(),
        description=description,
        fn=fn,
        is_async=inspect.iscoroutinefunction(fn),
        takes_ctx=takes_ctx,
        params=params,
        args_schema=args_schema,
        source=source,
    )


def local_tool(*, title: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册一个本地工具。函数名即工具名，title 是后台目录与界面展示用的中文名。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = _build_spec(fn, title)
        if spec.key in _REGISTRY:
            raise ValueError(
                f"本地工具重名：{spec.key}（{spec.source} 与 "
                f"{_REGISTRY[spec.key].source}）"
            )
        _REGISTRY[spec.key] = spec
        logger.debug("registered local tool %s (%s)", spec.key, spec.source)
        return fn

    return decorator


def discover() -> None:
    """导入 builtins 包下的全部模块，触发装饰器注册。重复调用是空操作。"""
    package = importlib.import_module("app.services.tools.builtins")
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name in _DISCOVERED:
            continue
        importlib.import_module(f"app.services.tools.builtins.{module_info.name}")
        _DISCOVERED.add(module_info.name)


def validate_registry() -> int:
    """启动校验：发现全部内置工具（注册期校验随之跑完），返回在册工具数。"""
    discover()
    return len(_REGISTRY)


def specs() -> List[ToolSpec]:
    """全部在册工具，按 key 排序，保证目录稳定。"""
    discover()
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def validate_keys(keys: Sequence[str]) -> None:
    """保存会话选择时的轻校验：只查 key 是否在册，不构建工具。"""
    discover()
    missing = [key for key in dict.fromkeys(keys) if key not in _REGISTRY]
    if missing:
        raise ValueError(
            f"本地工具已不可用：{'、'.join(missing)}，请在工具选择中移除"
        )


# ---- 装配 ---------------------------------------------------------------------


def stringify_output(result: Any) -> str:
    """工具返回统一成字符串：非字符串 JSON 序列化，超长截断。"""
    if isinstance(result, str):
        text = result
    elif result is None:
        text = ""
    else:
        text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > TOOL_OUTPUT_LIMIT:
        text = text[:TOOL_OUTPUT_LIMIT] + _TRUNCATE_NOTICE
    return text


def _as_langchain_tool(spec: ToolSpec, ctx: ToolContext) -> BaseTool:
    """按 takes_ctx × 同步/异步四种组合包成 LangChain 工具，模型面不带 ctx。"""
    fn = spec.fn

    if spec.takes_ctx and spec.is_async:

        async def _coroutine(**kwargs: Any) -> str:
            return stringify_output(await fn(ctx, **kwargs))

        run = None
        coroutine = _coroutine
    elif spec.takes_ctx:

        def _run(**kwargs: Any) -> str:
            return stringify_output(fn(ctx, **kwargs))

        run = _run
        coroutine = None
    elif spec.is_async:

        async def _plain_coroutine(**kwargs: Any) -> str:
            return stringify_output(await fn(**kwargs))

        run = None
        coroutine = _plain_coroutine
    else:

        def _plain_run(**kwargs: Any) -> str:
            return stringify_output(fn(**kwargs))

        run = _plain_run
        coroutine = None

    return StructuredTool.from_function(
        func=run,
        coroutine=coroutine,
        name=spec.key,
        description=spec.description,
        args_schema=spec.args_schema,
    )


def build_local_tools(keys: Sequence[str], ctx: ToolContext) -> List[BaseTool]:
    """按会话选择的 key 列表构建 LangChain 工具（去重保序）。未知 key 直接报错。"""
    discover()
    unique = list(dict.fromkeys(keys))
    missing = [key for key in unique if key not in _REGISTRY]
    if missing:
        raise ValueError(
            f"本地工具已不可用：{'、'.join(missing)}，请在工具选择中移除后重试"
        )
    return [_as_langchain_tool(_REGISTRY[key], ctx) for key in unique]


def cap_tool_output(tool: BaseTool) -> BaseTool:
    """给外部工具（MCP）套一层与本地工具相同的输出截断，工具名/schema 不变。"""

    async def _coroutine(**kwargs: Any) -> str:
        result = await tool.ainvoke(kwargs)
        return stringify_output(result)

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def ensure_unique_tool_names(tools: Sequence[BaseTool]) -> None:
    """装配末的最后一道查重：本地/MCP/内置工具在同一生效集合里不许重名。"""
    seen: set[str] = set()
    clashes: List[str] = []
    for tool in tools:
        if tool.name in seen:
            clashes.append(tool.name)
        seen.add(tool.name)
    if clashes:
        raise ValueError(
            f"工具名冲突：{'、'.join(sorted(set(clashes)))}"
            "（可能是本地工具与 MCP 服务提供的工具撞名），"
            "请调整 MCP 服务名或更换本地工具后重试"
        )
