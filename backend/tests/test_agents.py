"""内置智能体这张表与 :class:`AgentService` 的本域用例。

``agents.py`` 七百行里五百行是人设数据，逻辑只有 seed 与增删改查那一段。风险恰恰
在数据那侧：

- 内置项是启动时 seed 的，构造的是 ORM 对象，**绕过了** ``AgentCreate`` 的字段校验。
  slug 不合规则、名字超了 128、prompt 超了 8000、sort_order 撞车，都不会在提交时报
  错，只会在部署后某个界面或某次复制里崩。
- 工作流的步骤是按 ``agent_slug`` 找人（``services/workflows/builtin.py``）。名字改
  了没有任何静态引用会红，跑到那一步才炸，所以这张交叉表必须有用例守着。
- seed 每次启动都跑：它刷人设但不刷 ``enabled``，也不能碰用户自己的同名私有项。
- 可见性与修改权全部落在 ``owner_id in (0, 自己)`` 这一句 SQL 上，隔离破了就是
  跨账号读别人的提示词。
- 路由把服务的三种失败折成两种信封：业务拒绝是 HTTP 200 + ``code=-1``，「不存在 /
  不是你的」是 HTTP 404 + ``code=404``。前端读的是哪一层得由用例钉住，否则复制内置
  项这条路（duplicate 是唯一能改的路径）一崩就分不清是权限还是数据问题。

SQLite 跑文件库；这层不调模型。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.main import app
from app.models.entities import Agent, SysUser
from app.models.schemas import AgentCreate, AgentUpdate
from app.security import create_access_token, hash_password
from app.services.agents import (
    BUILTIN_AGENTS,
    BUILTIN_OWNER_ID,
    _GROUNDED_RULES,
    AgentService,
    seed_builtin_agents,
)

OWNER = 1
OTHER = 2
# 与 AgentCreate.slug 的 pattern 一致：内置项没走那个模型，只能自己核一遍。
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agents.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def service(session, owner_id: int = OWNER) -> AgentService:
    return AgentService(session, owner_id)


def seeded(session) -> None:
    seed_builtin_agents(session)


def builtin(session, slug: str) -> Agent:
    return session.scalar(
        select(Agent).where(Agent.owner_id == BUILTIN_OWNER_ID, Agent.slug == slug)
    )


def private(session, owner_id: int, slug: str, **fields) -> Agent:
    fields.setdefault("name", slug)
    fields.setdefault("system_prompt", "人设")
    row = Agent(owner_id=owner_id, slug=slug, **fields)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def payload(**over) -> AgentCreate:
    fields = {"slug": "my-agent", "name": "我的助手", "system_prompt": "帮我把事情做完。"}
    fields.update(over)
    return AgentCreate(**fields)


# ---- 一、内置表自身的形状：seed 绕过 schema，所以自己钉一遍 --------------------


def test_slugs_and_display_orders_are_unique():
    slugs = [agent.slug for agent in BUILTIN_AGENTS]
    assert len(slugs) == len(set(slugs))
    orders = [agent.sort_order for agent in BUILTIN_AGENTS]
    # 撞车的 sort_order 不会报错，只会让列表顺序变成「看插入顺序」。
    assert len(orders) == len(set(orders)), f"sort_order 有重复：{orders}"


def test_every_builtin_row_fits_the_limits_a_user_must_respect():
    for agent in BUILTIN_AGENTS:
        assert SLUG_PATTERN.match(agent.slug), agent.slug
        assert 0 < len(agent.name) <= 128, agent.slug
        assert len(agent.description or "") <= 512, agent.slug
        assert 0 < len(agent.system_prompt) <= 8000, agent.slug
        # None 是合法值：表示「跟随模型配置」，运维那类人设就该走这个，别替模型规定温度。
        assert agent.temperature is None or 0 <= agent.temperature <= 100, agent.slug
        assert 0 <= agent.sort_order <= 9999, agent.slug


def test_knowledge_personas_are_exactly_the_grounded_ones():
    # 「只能依据知识库内容回答，不要凭常识补充」这段规则与 use_knowledge 必须同进
    # 同出：标志开着而 prompt 没带规则，模型就会拿常识编造出处；反过来 prompt 带
    # 规则却不检索，等于让它「依据」一份根本没有的材料。
    for agent in BUILTIN_AGENTS:
        assert (_GROUNDED_RULES in agent.system_prompt) is agent.use_knowledge, agent.slug


def test_builtin_slugs_are_the_ones_the_module_documents():
    # 这份名单是硬约束的输入：简历 / 运维 / 幻灯片那几条流程按 slug 认人设，改名
    # 属于一次有意的迁移，应该在这里被要求同步。
    assert {agent.slug for agent in BUILTIN_AGENTS} == {
        "customer-service",
        "tech-support",
        "doc-summary",
        "diagram-assistant",
        "general-chat",
        "code-assistant",
        "career-advisor",
        "self-review-feedback",
        "server-ops",
        "db-ops",
        "ops-assistant",
        "resume-analyzer",
        "resume-compare",
        "resume-optimizer",
        "career-matcher",
        "jd-match-auditor",
        "interview-coach",
        "portfolio-planner",
        "salary-negotiator",
        "interview-answerer",
        "slide-architect",
    }


def test_feature_owned_personas_stay_out_of_the_picker():
    # chat_visible=False 的那批是功能自己的人设（运维、简历、面试、幻灯片），由后台
    # 调用；出现在对话选择列表里等于给用户一批他改不动也选不明白的选项。
    assert {agent.slug for agent in BUILTIN_AGENTS if not agent.chat_visible} == {
        "server-ops",
        "db-ops",
        "resume-analyzer",
        "resume-compare",
        "resume-optimizer",
        "career-matcher",
        "jd-match-auditor",
        "interview-coach",
        "portfolio-planner",
        "salary-negotiator",
        "interview-answerer",
        "slide-architect",
    }
    # 运维工具面的开关只有对话里那个运维助手需要：server-ops / db-ops 是功能人设，
    # 能不能执行由工具白名单决定，不由这个标志决定。
    assert {agent.slug for agent in BUILTIN_AGENTS if agent.use_ops} == {"ops-assistant"}


def test_every_workflow_step_points_at_an_agent_that_exists():
    # 步骤里是一个裸字符串 agent_slug，没有任何静态引用会跟着改名一起红。
    from app.services.workflows.builtin import BUILTIN_WORKFLOWS

    slugs = {agent.slug for agent in BUILTIN_AGENTS}
    referenced = {step.agent_slug for workflow in BUILTIN_WORKFLOWS for step in workflow.steps}
    assert referenced, "内置工作流应当引用人设，这条交叉检查才有意义"
    assert referenced - slugs == set()


# ---- 二、seed：刷人设，不刷用户的决定 ----------------------------------------


def test_seed_creates_the_shared_rows_once_and_survives_being_run_again(db):
    seeded(db)
    rows = db.scalars(select(Agent).where(Agent.owner_id == BUILTIN_OWNER_ID)).all()
    assert len(rows) == len(BUILTIN_AGENTS)
    assert all(row.is_builtin and row.enabled for row in rows)

    # 每次启动都跑，撞 (owner_id, slug) 唯一键就是起不来。
    seeded(db)
    assert db.scalar(select(func.count()).select_from(Agent)) == len(BUILTIN_AGENTS)


def test_seed_pulls_back_an_edited_prompt_but_leaves_the_hide_alone(db):
    seeded(db)
    row = service(db).get_by_slug("customer-service")
    row.system_prompt = "被改过的人设"
    row.enabled = False
    db.commit()

    seeded(db)
    db.refresh(row)
    expected = next(agent for agent in BUILTIN_AGENTS if agent.slug == "customer-service")
    assert row.system_prompt == expected.system_prompt
    assert row.name == expected.name
    # 把某个智能体藏起来是用户自己的决定，应该扛得过一次部署。
    assert row.enabled is False


def test_seed_only_looks_at_the_shared_space(db):
    mine = private(db, OWNER, "customer-service", name="我的客服")
    seeded(db)
    db.refresh(mine)
    assert mine.name == "我的客服"
    assert mine.is_builtin is False
    # 内置项照常建出来，两者靠 owner_id 各自成立。
    assert builtin(db, "customer-service").is_builtin is True


# ---- 三、可见性与隔离 ---------------------------------------------------------


def test_list_shows_built_ins_plus_only_your_own(db):
    seeded(db)
    private(db, OWNER, "mine", sort_order=1)
    private(db, OTHER, "theirs", sort_order=2)

    visible = service(db, OWNER).list()
    assert "mine" in [agent.slug for agent in visible]
    assert "theirs" not in [agent.slug for agent in visible]
    assert len(visible) == len(BUILTIN_AGENTS) + 1
    # 列表按 sort_order 排，用户自己的项要能排到内置项前面。
    assert [agent.sort_order for agent in visible] == sorted(
        agent.sort_order for agent in visible
    )


def test_enabled_only_drops_what_the_user_hid(db):
    seeded(db)
    service(db).get_by_slug("general-chat").enabled = False
    db.commit()

    assert len(service(db).list()) == len(BUILTIN_AGENTS)
    assert len(service(db).list(enabled_only=True)) == len(BUILTIN_AGENTS) - 1


def test_a_foreign_agent_reads_as_not_found(db):
    seeded(db)
    theirs = private(db, OTHER, "theirs")

    with pytest.raises(LookupError) as caught:
        service(db, OWNER).get(theirs.id)
    assert "不存在" in str(caught.value)
    assert service(db, OWNER).get_by_slug(theirs.slug) is None
    # 内置项对所有人开放。
    assert service(db, OWNER).get_by_slug("customer-service") is not None


def test_require_by_slug_keeps_using_a_disabled_feature_persona(db):
    seeded(db)
    row = service(db).get_by_slug("resume-analyzer")
    row.enabled = False
    db.commit()

    # 功能自己的人设不是可选项：用户在界面上关掉它，后台那条流程还得照常用，
    # 否则简历分析会凭空停摆。
    assert service(db).require_by_slug("resume-analyzer").id == row.id


def test_require_by_slug_reports_a_broken_initialization(db):
    with pytest.raises(BusinessError) as caught:
        service(db).require_by_slug("server-ops")
    assert caught.value.code == CODE_CHAT_NOT_READY
    assert "重启服务" in caught.value.msg


# ---- 四、写路径：内置项只读，改动只回写显式字段 --------------------------------


def test_editing_a_builtin_is_read_only_through_the_service(db):
    seeded(db)
    target = service(db).get_by_slug("customer-service")

    with pytest.raises(PermissionError) as caught:
        service(db).update(target.id, AgentUpdate(name="改了名"))
    assert "复制后再编辑" in str(caught.value)
    with pytest.raises(PermissionError):
        service(db).delete(target.id)
    db.rollback()
    assert target.name == "智能客服"


def test_a_new_agent_cannot_take_a_slug_that_is_already_taken(db):
    seeded(db)
    # 撞内置 slug 等于把自己的项压在共享项上：列表里会出现两个同名人设，而内置那个
    # 还改不掉。唯一键是按 owner 分组的，数据库不会替我们挡。
    with pytest.raises(ValueError) as caught:
        service(db).create(payload(slug="customer-service"))
    assert "标识已存在" in str(caught.value)

    private(db, OTHER, "theirs")
    with pytest.raises(ValueError) as caught:
        service(db, OTHER).create(payload(slug="theirs"))
    assert "标识已存在" in str(caught.value)

    # 同一个 slug 归另一个人是允许的（唯一键按 owner 分组）。
    assert service(db, OWNER).create(payload(slug="theirs")).owner_id == OWNER


def test_create_defaults_a_row_into_the_users_space(db):
    agent = service(db).create(payload())
    assert (agent.owner_id, agent.slug, agent.is_builtin, agent.enabled) == (
        OWNER,
        "my-agent",
        False,
        True,
    )
    assert agent.temperature == 30
    assert service(db, OTHER).get_by_slug("my-agent") is None


def test_update_writes_back_only_the_fields_you_sent(db):
    seeded(db)
    agent = service(db).create(
        payload(name="王五的助手", description="一句话简介", temperature=40)
    )

    updated = service(db).update(agent.id, AgentUpdate(name="改了名"))
    assert updated.name == "改了名"
    assert updated.description == "一句话简介"
    assert updated.temperature == 40

    # 显式传 null 也清不掉字段：实现只写非 None 的值。要「清空」得改语义，这里先
    # 钉住现状，免得日后顺手改成静默丢数据。
    assert service(db).update(agent.id, AgentUpdate(description=None)).description == (
        "一句话简介"
    )

    # False 不是 None：隐藏开关走得进库里（seed 也据此不再动它）。
    assert service(db).update(agent.id, AgentUpdate(enabled=False)).enabled is False

    with pytest.raises(LookupError):
        service(db).update(999999, AgentUpdate(name="不存在"))


def test_delete_removes_only_the_callers_own_row(db):
    agent = service(db).create(payload())
    theirs = private(db, OTHER, "theirs")

    # 别人的 id 在这个用户视角里根本不存在，删除也只能报「不存在」。
    with pytest.raises(LookupError):
        service(db).delete(theirs.id)
    service(db).delete(agent.id)
    assert service(db).get_by_slug("my-agent") is None
    assert service(db, OTHER).get_by_slug("theirs") is not None


# ---- 五、复制：定制内置人设的唯一路径 -----------------------------------------


def test_duplicate_hands_back_an_editable_copy(db):
    seeded(db)
    source = service(db).get_by_slug("customer-service")

    clone = service(db).duplicate(source.id)
    assert clone.slug == "customer-service-copy"
    assert clone.owner_id == OWNER
    assert clone.is_builtin is False
    assert clone.name == f"{source.name} 副本"
    assert clone.system_prompt == source.system_prompt
    # 能力开关跟人设一起复制，副本的行为要和被复制的那一份对得上。
    assert clone.use_knowledge == source.use_knowledge
    assert clone.use_ops == source.use_ops
    assert clone.sort_order == source.sort_order + 1

    # 复制存在的全部意义就是这一句：副本可以改。
    assert service(db).update(clone.id, AgentUpdate(name="我调过的人设")).id == clone.id

    # 第二次复制不能再撞 slug。
    assert service(db).duplicate(source.id).slug == "customer-service-copy2"
    # slug 的空间是按 owner 分的：另一个人第一次复制同样拿到 -copy。
    assert service(db, OTHER).duplicate(source.id).slug == "customer-service-copy"


def test_duplicating_something_that_is_not_there_reports_it(db):
    with pytest.raises(LookupError):
        service(db).duplicate(999999)


def test_a_copied_user_agent_is_duplicable_too(db):
    agent = service(db).create(payload())
    assert service(db).duplicate(agent.id).slug == "my-agent-copy"


# ---- 六、路由层：404 与 code=-1 的分界 ---------------------------------------


@pytest.fixture()
def api(tmp_path):
    """真 app 打这五条路由，库还是文件 SQLite。

    ``require_user`` 不覆盖：这一层要钉的是「路由把服务的三种失败翻译成两种信封」，
    鉴权本身在 ``test_auth_users_api.py`` 里已经钉过。刻意不进 ``with``——TestClient
    的上下文管理器会跑 lifespan，那会拿开发库做启动初始化。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agents_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    users = {
        username: SysUser(
            username=username,
            password_hash=hash_password("口令", iterations=2),
            status=True,
            is_admin=username == "admin",
        )
        for username in ("admin", "worker")
    }
    session.add_all(list(users.values()))
    session.commit()
    seed_builtin_agents(session)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        yield SimpleNamespace(
            db=session,
            client=client,
            tokens={
                username: create_access_token(
                    user_id=row.id, username=username, is_admin=row.is_admin
                )
                for username, row in users.items()
            },
        )
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def agent_body(client, method: str, path: str, token: str, *, json_body=None):
    """发一个带 token 的请求并只回（状态码, 响应体），信封断言全都围着它写。

    ``json`` 只在有请求体时才传：httpx 的 ``get``/``delete`` 不收这个关键字。
    """
    kwargs = {"headers": bearer(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = getattr(client, method)(path, **kwargs)
    return response.status_code, response.json()


def test_the_list_endpoint_sits_behind_a_token(api):
    response = api.client.get("/api/agents")
    assert response.status_code == 401
    # 401 也必须套信封：SPA 的拦截器只读响应体的 code，原生 detail 格式会漏掉跳转。
    envelope = response.json()
    assert (envelope["code"], envelope["data"]) == (401, None)


def test_list_hands_back_shared_rows_plus_only_the_callers_own(api):
    status_code, envelope = agent_body(api.client, "get", "/api/agents", api.tokens["worker"])
    assert (status_code, envelope["code"]) == (200, 0)
    assert envelope["data"]["total"] == len(BUILTIN_AGENTS)
    assert all(item["is_builtin"] for item in envelope["data"]["items"])

    created = agent_body(
        api.client,
        "post",
        "/api/agents",
        api.tokens["worker"],
        json_body={"slug": "my-worker-agent", "name": "王五的助手", "system_prompt": "把事做完。"},
    )[1]
    assert created["data"]["is_builtin"] is False

    slugs = [item["slug"] for item in agent_body(
        api.client, "get", "/api/agents", api.tokens["worker"]
    )[1]["data"]["items"]]
    assert "my-worker-agent" in slugs
    # 管理员的列表里没有别人的私有项：两个用户看同一张表，靠 owner_id 分家。
    admin_slugs = [item["slug"] for item in agent_body(
        api.client, "get", "/api/agents", api.tokens["admin"]
    )[1]["data"]["items"]]
    assert "my-worker-agent" not in admin_slugs


def test_enabled_only_is_a_query_parameter_not_a_client_side_filter(api):
    hidden = api.db.scalar(
        select(Agent).where(Agent.owner_id == BUILTIN_OWNER_ID, Agent.slug == "general-chat")
    )
    hidden.enabled = False
    api.db.commit()

    full = agent_body(api.client, "get", "/api/agents", api.tokens["worker"])[1]["data"]
    shown = agent_body(
        api.client, "get", "/api/agents?enabled_only=true", api.tokens["worker"]
    )[1]["data"]
    assert full["total"] == len(BUILTIN_AGENTS)
    assert shown["total"] == len(BUILTIN_AGENTS) - 1
    assert "general-chat" not in [item["slug"] for item in shown["items"]]


def test_a_taken_slug_is_a_business_refusal_not_an_http_error(api):
    # 撞内置 slug：服务 raise ValueError，路由把它折成 HTTP 200 + code=-1，前端据此
    # 弹提示而不是当成服务器错误。
    status_code, envelope = agent_body(
        api.client,
        "post",
        "/api/agents",
        api.tokens["worker"],
        json_body={"slug": "customer-service", "name": "压在共享项上", "system_prompt": "人设"},
    )
    assert (status_code, envelope["code"], envelope["message"]) == (
        200,
        -1,
        "标识已存在",
    )


def test_an_illegal_body_never_reaches_the_service(api):
    # 这条正是内置表最需要外部对照的地方：seed 构造 ORM 对象时绕过 AgentCreate，
    # 而用户这条路径必须被 pattern 拦住，否则界面上就会出现一批「能建不能选」的行。
    status_code, envelope = agent_body(
        api.client,
        "post",
        "/api/agents",
        api.tokens["worker"],
        json_body={"slug": "Bad Slug", "name": "不合规则", "system_prompt": "人设"},
    )
    assert (status_code, envelope["code"], envelope["message"]) == (
        422,
        422,
        "请求参数不合法",
    )
    assert api.db.scalar(
        select(Agent).where(Agent.owner_id == BUILTIN_OWNER_ID, Agent.slug == "Bad Slug")
    ) is None


def test_editing_a_builtin_is_refused_in_the_body_not_the_status(api):
    target = api.db.scalar(
        select(Agent).where(
            Agent.owner_id == BUILTIN_OWNER_ID, Agent.slug == "customer-service"
        )
    )
    put = agent_body(
        api.client, "put", f"/api/agents/{target.id}", api.tokens["admin"], json_body={"name": "改名"}
    )
    assert (put[0], put[1]["code"]) == (200, -1)
    assert "复制后再编辑" in put[1]["message"]

    deleted = agent_body(api.client, "delete", f"/api/agents/{target.id}", api.tokens["admin"])
    assert (deleted[0], deleted[1]["code"]) == (200, -1)
    api.db.refresh(target)
    assert target.name == "智能客服"


def test_ids_that_are_not_yours_read_as_404(api):
    # 「不存在」和「是别人的」必须是同一个答案，否则这五条路由就是一台 id 探测机；
    # 而 404 走 HTTPException，信封里的 code 跟着状态码走，和业务拒绝的 -1 不是一类。
    theirs = agent_body(
        api.client,
        "post",
        "/api/agents",
        api.tokens["admin"],
        json_body={"slug": "admin-only", "name": "管理员的", "system_prompt": "人设"},
    )[1]["data"]

    for method, kwargs in (
        ("put", {"json_body": {"name": "试图改名"}}),
        ("delete", {}),
    ):
        status_code, envelope = agent_body(
            api.client, method, f"/api/agents/{theirs['id']}", api.tokens["worker"], **kwargs
        )
        assert (status_code, envelope["code"], envelope["message"]) == (
            404,
            404,
            "智能体不存在",
        )
    duplicate = agent_body(
        api.client, "post", f"/api/agents/{theirs['id']}/duplicate", api.tokens["worker"]
    )
    assert (duplicate[0], duplicate[1]["code"]) == (404, 404)


def test_duplicate_is_the_way_to_customise_a_builtin_over_http(api):
    source = api.db.scalar(
        select(Agent).where(
            Agent.owner_id == BUILTIN_OWNER_ID, Agent.slug == "customer-service"
        )
    )
    status_code, envelope = agent_body(
        api.client, "post", f"/api/agents/{source.id}/duplicate", api.tokens["worker"]
    )
    assert (status_code, envelope["code"]) == (200, 0)
    clone = envelope["data"]
    assert (clone["slug"], clone["is_builtin"], clone["name"]) == (
        "customer-service-copy",
        False,
        "智能客服 副本",
    )
    # 复制存在的意义就是这一句：副本改得动。
    edited = agent_body(
        api.client,
        "put",
        f"/api/agents/{clone['id']}",
        api.tokens["worker"],
        json_body={"name": "我调过的人设"},
    )
    assert (edited[0], edited[1]["code"], edited[1]["data"]["name"]) == (200, 0, "我调过的人设")


# ---- temperature nullable --------------------------------------------------


def test_agent_temperature_can_be_null_on_create(db):
    """新建智能体时 temperature 可以为 null（跟随模型配置）。"""
    svc = AgentService(db, OWNER)
    agent = svc.create(
        AgentCreate(
            slug="null-temp",
            name="Null Temp",
            system_prompt="test",
            temperature=None,
        )
    )
    assert agent.temperature is None


def test_agent_update_can_set_temperature_back_to_null(db):
    """显式传 temperature=null 必须能落库，不被 update 的 None 过滤掉。"""
    svc = AgentService(db, OWNER)
    agent = svc.create(
        AgentCreate(
            slug="explicit-temp",
            name="Explicit Temp",
            system_prompt="test",
            temperature=50,
        )
    )
    assert agent.temperature == 50

    updated = svc.update(agent.id, AgentUpdate(temperature=None))
    assert updated.temperature is None

