"""本地工具注册表（``services/tools/registry.py``）。

钉住四件事：注册期校验（命名/保留名/重名/注解/docstring/标题）、从函数签名与
Google docstring 生成的参数规格与 pydantic schema、装配出的 LangChain 工具在
sync/async × 是否吃 ctx 四种组合下都能跑且输出统一字符串化+截断，以及只读目录
接口的鉴权与信封。

临时注册的工具一律在 finally 里从注册表摘掉，不能污染其他文件的进程内状态。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.models.schemas import CurrentUser
from app.routers import tools as tools_router
from app.services.agent_run import TOOL_OUTPUT_LIMIT
from app.services.tools import (
    ToolContext,
    build_local_tools,
    cap_tool_output,
    ensure_unique_tool_names,
    local_tool,
    specs,
    validate_keys,
    validate_registry,
)
from app.services.tools import registry

wait = asyncio.run


# ---- 内置目录：发现、字段、来源、排序 -----------------------------------------


def test_discover_finds_the_builtin_tools_with_stable_sorted_keys():
    count = validate_registry()
    keys = [spec.key for spec in specs()]
    assert count >= 3
    assert keys == sorted(keys)
    assert {"current_datetime", "generate_uuid", "random_integer"} <= set(keys)


def test_builtin_specs_carry_title_docstring_params_and_source():
    by_key = {spec.key: spec for spec in specs()}
    uuid_spec = by_key["generate_uuid"]

    assert uuid_spec.title == "生成 UUID"
    assert uuid_spec.is_async is False
    assert uuid_spec.takes_ctx is False
    assert uuid_spec.description.startswith("生成一个或多个随机 UUID")
    # 来源定位到 services/tools 下，不带 app/ 前缀；3.8+ 起装饰函数的
    # co_firstlineno 落在装饰器那一行。
    assert uuid_spec.source == "services/tools/builtins/random_tools.py:13"

    (param,) = uuid_spec.params
    assert param.name == "count"
    assert param.json_type == "integer"
    assert param.required is False
    assert param.default == 1
    assert "1 到 100" in param.description

    integer_spec = by_key["random_integer"]
    params = {p.name: p for p in integer_spec.params}
    assert set(params) == {"min_value", "max_value"}
    assert params["min_value"].default == 0 and params["max_value"].default == 100
    assert "区间下限" in params["min_value"].description


def test_builtin_args_schema_exposes_model_facing_fields_with_defaults():
    spec = {s.key: s for s in specs()}["generate_uuid"]
    assert set(spec.args_schema.model_fields) == {"count"}
    assert spec.args_schema().count == 1
    assert spec.args_schema(count=3).count == 3


def test_current_datetime_spec_and_beijing_wall_time_output():
    spec = {s.key: s for s in specs()}["current_datetime"]
    assert spec.title == "查询当前时间"
    assert spec.params == []
    assert spec.is_async is False
    assert spec.takes_ctx is False
    assert spec.source == "services/tools/builtins/datetime_tools.py:17"

    (tool,) = build_local_tools(["current_datetime"], ToolContext(1, 2))
    output = wait(tool.ainvoke({}))
    beijing = datetime.now(timezone(timedelta(hours=8)))
    parsed = datetime.strptime(output[:19], "%Y-%m-%d %H:%M:%S")
    assert len(output) == 23
    # 固定 UTC+8：与运行机器的时区无关，允许两分钟计时误差。
    assert abs((parsed - beijing.replace(tzinfo=None)).total_seconds()) < 120
    assert output == f"{parsed:%Y-%m-%d %H:%M:%S} 星期{'一二三四五六日'[parsed.weekday()]}"


# ---- 注册期校验 ---------------------------------------------------------------


def _plain_tool() -> callable:
    def sample_tool(x: int = 1) -> int:
        """一个示例工具。

        Args:
            x: 入参。
        """
        return x

    return sample_tool


def test_registration_rejects_illegal_names():
    fn = _plain_tool()
    fn.__name__ = "Bad-Name"
    with pytest.raises(ValueError, match="不合法"):
        registry._build_spec(fn, "标题")

    fn = _plain_tool()
    fn.__name__ = "a" * 65
    with pytest.raises(ValueError, match="不合法"):
        registry._build_spec(fn, "标题")


def test_registration_rejects_reserved_names():
    fn = _plain_tool()
    fn.__name__ = "run_server_command"
    with pytest.raises(ValueError, match="内置工具冲突"):
        registry._build_spec(fn, "标题")


def test_registration_requires_title_docstring_and_annotations():
    with pytest.raises(ValueError, match="缺少中文标题"):
        registry._build_spec(_plain_tool(), "   ")

    def no_docstring(x: int = 1) -> int:
        return x

    with pytest.raises(ValueError, match="缺少 docstring"):
        registry._build_spec(no_docstring, "标题")

    def missing_annotation(x):  # noqa: ANN001 - 故意不标类型
        """有描述。"""
        return x

    with pytest.raises(ValueError, match="缺少类型注解"):
        registry._build_spec(missing_annotation, "标题")


def test_duplicate_registration_names_both_sources():
    @local_tool(title="第一个")
    def dup_demo_tool(x: int = 1) -> int:
        """第一个同名工具。"""
        return x

    try:
        def dup_demo_tool() -> int:  # type: ignore[no-redef]
            """第二个同名工具。"""
            return 2

        with pytest.raises(ValueError, match="本地工具重名：dup_demo_tool"):
            local_tool(title="第二个")(dup_demo_tool)
    finally:
        registry._REGISTRY.pop("dup_demo_tool", None)


def test_json_type_mapping_and_required_flags():
    def multi_type_tool(
        a: int, b: bool, c: float, d: list, e: dict, f: str = "默认"  # noqa: ANN001
    ) -> str:
        """多种参数类型。

        Args:
            a: 整数。
            b: 布尔。
            c: 小数。
            d: 列表。
            e: 对象。
            f: 字符串。
        """
        return ""

    spec = registry._build_spec(multi_type_tool, "类型探针")
    params = {p.name: p for p in spec.params}
    assert [params[name].json_type for name in ("a", "b", "c", "d", "e", "f")] == [
        "integer",
        "boolean",
        "number",
        "array",
        "object",
        "string",
    ]
    assert all(params[name].required for name in ("a", "b", "c", "d", "e"))
    assert params["f"].required is False and params["f"].default == "默认"
    assert params["a"].description == "整数。"


# ---- ctx：识别、剔除出模型面、运行期注入 ---------------------------------------


def test_context_first_param_is_hidden_from_the_model_schema():
    def with_ctx(ctx: ToolContext, word: str = "hi") -> str:
        """带上下文的工具。

        Args:
            word: 要说的词。
        """
        return word

    spec = registry._build_spec(with_ctx, "上下文工具")
    assert spec.takes_ctx is True
    assert [p.name for p in spec.params] == ["word"]
    assert set(spec.args_schema.model_fields) == {"word"}
    assert spec.args_schema().word == "hi"


# ---- 装配与执行 ---------------------------------------------------------------


def test_build_local_tools_dedups_preserves_order_and_runs_sync_tools():
    tools = build_local_tools(
        ["generate_uuid", "random_integer", "generate_uuid"], ToolContext(1, 2)
    )
    assert [tool.name for tool in tools] == ["generate_uuid", "random_integer"]

    one = wait(tools[0].ainvoke({}))
    assert len(one.split(", ")) == 1 and one
    three = wait(tools[0].ainvoke({"count": 3}))
    assert len(three.split(", ")) == 3
    # random_integer 返回 int，出工具边界必须已是字符串。
    assert wait(tools[1].ainvoke({"min_value": 5, "max_value": 5})) == "5"


def test_sync_tool_body_validation_propagates_as_exception():
    (tool,) = build_local_tools(["generate_uuid"], ToolContext(1, 2))
    with pytest.raises(ValueError, match="count 必须在 1 到 100"):
        wait(tool.ainvoke({"count": 0}))


def test_async_tool_with_ctx_gets_context_and_stringifies_output():
    captured: dict = {}

    @local_tool(title="上下文回显")
    async def ctx_echo(ctx: ToolContext, word: str = "hi") -> dict:
        """回显执行上下文。

        Args:
            word: 要说的词。
        """
        captured["ctx"] = ctx
        return {"owner": ctx.owner_id, "conversation": ctx.conversation_id, "word": word}

    try:
        (tool,) = build_local_tools(["ctx_echo"], ToolContext(7, 9))
        output = wait(tool.ainvoke({"word": "在吗"}))
    finally:
        registry._REGISTRY.pop("ctx_echo", None)

    assert captured["ctx"] == ToolContext(7, 9)
    assert output == '{"owner": 7, "conversation": 9, "word": "在吗"}'


def test_unknown_keys_are_rejected_by_validate_and_build():
    validate_keys(["generate_uuid"])  # 在册不抛
    with pytest.raises(ValueError, match="本地工具已不可用：ghost_tool"):
        validate_keys(["ghost_tool"])
    with pytest.raises(ValueError, match="ghost_tool.*移除后重试"):
        build_local_tools(["ghost_tool"], ToolContext(1, 2))


def test_stringify_output_serializes_and_truncates():
    assert registry.stringify_output(42) == "42"
    assert registry.stringify_output(["a", 1]) == '["a", 1]'
    assert registry.stringify_output(None) == ""

    out = registry.stringify_output("x" * (TOOL_OUTPUT_LIMIT + 10))
    assert out.endswith(registry._TRUNCATE_NOTICE)
    assert len(out) == TOOL_OUTPUT_LIMIT + len(registry._TRUNCATE_NOTICE)


def _named_tool(name: str) -> StructuredTool:
    async def _coro(**kwargs):  # noqa: ANN001
        return 5

    return StructuredTool.from_function(coroutine=_coro, name=name, description="测试")


def test_ensure_unique_tool_names_flags_clashes():
    ensure_unique_tool_names([_named_tool("a"), _named_tool("b")])  # 不抛
    with pytest.raises(ValueError, match="工具名冲突：a"):
        ensure_unique_tool_names([_named_tool("a"), _named_tool("a")])


def test_cap_tool_output_preserves_identity_and_stringifies():
    def add(a: int = 0, b: int = 0) -> int:
        """两数相加。"""
        return a + b

    raw = StructuredTool.from_function(add, name="calc_add", description="两数相加")
    capped = cap_tool_output(raw)
    assert capped.name == "calc_add"
    assert capped.args_schema is raw.args_schema
    assert wait(capped.ainvoke({"a": 2, "b": 3})) == "5"


# ---- 只读目录接口 -------------------------------------------------------------


@pytest.fixture()
def api_app(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'tools_api.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", session_factory)

    app = FastAPI()
    app.include_router(tools_router.router, prefix="/api")

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return app


def test_local_catalog_requires_auth(api_app):
    response = TestClient(api_app).get("/api/tools/local")
    assert response.status_code == 401


def test_local_catalog_returns_envelope_with_builtins(api_app):
    api_app.dependency_overrides[require_user] = lambda: CurrentUser(
        user_id=1, username="tester"
    )
    body = TestClient(api_app).get("/api/tools/local").json()

    assert body["code"] == 0
    items = body["data"]["items"]
    assert body["data"]["total"] == len(items)
    by_key = {item["key"]: item for item in items}
    uuid_item = by_key["generate_uuid"]
    assert uuid_item["title"] == "生成 UUID"
    assert uuid_item["source"].startswith("services/tools/builtins/random_tools.py")
    assert uuid_item["is_async"] is False
    param = uuid_item["params"][0]
    assert param == {
        "name": "count",
        "description": "生成数量，1 到 100，默认 1。",
        "type": "integer",
        "required": False,
        "default": 1,
    }
