"""模型配置这一域的本域用例（``services/model_config.py``）。

钉的是四件事，全部围绕「一份配置什么时候还算能用」：

- 列表与默认查询：所有读都走 ``_scope``，跨账号读等于把别人的模型名与 base_url
  摆到自己界面上；默认位是按 owner + purpose 各一份，不是全局一份。
- 掩码：``to_item`` 是 API 唯一往外递配置的口子，明文出去就等于凭据泄漏；反过来，
  解密失败必须**说出来**（``api_key_error``），静默成空串跟「用户从来没填过」长得
  一模一样，正是这套设计最要避免的故障。
- 凭据轮换：前端表单里 api_key 位预填的是掩码。掩码、空串、没传，三种都只表示
  「别动它」；真换了 key 或 base_url，之前那次「测试通过」就不作数了，默认位也得
  一起撤——拿一份没验证过的配置顶着默认，坏掉的是所有人的对话。
- 删除语义：引用它的会话与个人工作流步骤要跟着清空（不清就留悬空 id，主链路撞
  ``LookupError`` 变成用户看不懂的 404）；``workflow_run_step`` 里那一列是「这一步
  当时实际用了哪份」的留痕，不许跟着清；删掉默认那份要顶上另一份验证过的。
- 思维链开关（``reasoning_mode`` / ``request_extra``）：三态各自选哪种客户端、额外
  参数只在「开启」时进请求体；这两列前端每次整份回传，所以「没传 / 传 null / 传空
  对象」三种必须分清，改动参数算作测试态失效，光挪开关不算。

路由层再加一小节，验的是掩码真的带到了线上：响应体里只有 ``sk-****`` 加尾四位。

跑文件型 SQLite；不连服务商，也不碰共享 MySQL。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.crypto import MASK_PREFIX, decrypt, encrypt
from app.database import Base, get_db
from app.main import app
from app.models.entities import (
    ChatConversation,
    ModelConfig,
    SysUser,
    Workflow,
    WorkflowRunStep,
    WorkflowStep,
)
from app.models.schemas import ModelConfigCreate, ModelConfigUpdate, ModelPurpose
from app.security import create_access_token, hash_password
from app.services.chat import ChatService
from app.services.model_config import ModelConfigService, default_model_config
from app.services.providers import ChatOpenAI, ReasoningChatOpenAI, build_chat_model

OWNER = 1
OTHER = 2
# 掩码只留尾四位，用例拿这一把当「明文」，断言它绝不出现在响应里。
KEY = "sk-prov-abcdef1234"
MASKED_KEY = f"{MASK_PREFIX}1234"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'model_config.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_config(
    db, *, owner_id: int = OWNER, title: str = "配置", is_default: bool = False,
    last_test_ok: bool = True, purpose: ModelPurpose = ModelPurpose.CHAT,
    api_key: str | None = None, vector_size: int | None = None,
) -> ModelConfig:
    row = ModelConfig(
        owner_id=owner_id,
        purpose=purpose.value,
        title=title,
        model_name="unit-test",
        base_url="http://localhost:1/v1",
        api_key_enc=encrypt(api_key) if api_key is not None else None,
        is_default=is_default,
        last_test_ok=last_test_ok,
        vector_size=vector_size,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_conversation(db, config: ModelConfig, *, owner_id: int = OWNER) -> ChatConversation:
    row = ChatConversation(owner_id=owner_id, title="引用了配置的会话", model_config_id=config.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_step(db, config: ModelConfig, *, key: str = "s1") -> WorkflowStep:
    workflow = Workflow(owner_id=OWNER, slug=f"wf-{key}", name="删除用例会跑的流程")
    db.add(workflow)
    db.commit()
    step = WorkflowStep(
        workflow_id=workflow.id,
        key=key,
        group_key=key,
        sort_order=0,
        agent_slug="general-chat",
        prompt_tpl="{{input}}",
        model_config_id=config.id,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def svc(db, owner_id: int = OWNER) -> ModelConfigService:
    return ModelConfigService(db, owner_id)


def create_payload(**over) -> ModelConfigCreate:
    fields = {
        "purpose": ModelPurpose.CHAT,
        "title": "一份配置",
        "model_name": "unit-model",
        "base_url": "http://localhost:1/v1",
        "api_key": KEY,
    }
    fields.update(over)
    return ModelConfigCreate(**fields)


# ---- 一、引用清理 -------------------------------------------------------------


def test_delete_clears_the_conversation_reference_and_the_chat_gate_survives(db):
    """删掉会话引用的配置后要退回默认配置，而不是留下悬空 id 让下一次回答 404。"""
    chosen = make_config(db, title="被删的那份")
    keep = make_config(db, title="默认那份", is_default=True)
    conversation = make_conversation(db, chosen)
    db.refresh(conversation)

    svc(db).delete(chosen.id)

    db.expire_all()
    assert conversation.model_config_id is None
    # 真正要坐实的是读路径：以前这一句抛 LookupError，收尾任务不接那个异常。
    assert svc(db).get_default(ModelPurpose.CHAT).id == keep.id
    assert ChatService(db, OWNER).require_chat_config(conversation.model_config_id).id == keep.id


def test_delete_clears_workflow_step_but_not_the_run_record(db):
    """步骤定义是「以后用哪份」，要清；运行留痕是「当时用了哪份」，留下。"""
    chosen = make_config(db, title="步骤指定那份")
    step = make_step(db, chosen)
    trace = WorkflowRunStep(
        owner_id=OWNER,
        run_id=1,
        key=step.key,
        group_key=step.group_key,
        agent_slug=step.agent_slug,
        model_config_id=chosen.id,
    )
    db.add(trace)
    db.commit()

    svc(db).delete(chosen.id)

    db.expire_all()
    assert step.model_config_id is None
    assert trace.model_config_id == chosen.id


def test_delete_rejects_another_owner_config_and_leaves_other_rows(db):
    """别人的配置删不掉（404），指向别的配置的会话也不该被顺手抹掉。"""
    other_owner = make_config(db, owner_id=OTHER, title="别人的配置")
    chosen = make_config(db, title="被删的那份")
    keep = make_config(db, title="留下那份")
    doomed = make_conversation(db, chosen)
    survivor = make_conversation(db, keep)
    theirs = make_conversation(db, other_owner, owner_id=OTHER)

    with pytest.raises(LookupError):
        svc(db).delete(other_owner.id)

    svc(db).delete(chosen.id)

    db.expire_all()
    assert doomed.model_config_id is None
    assert survivor.model_config_id == keep.id
    assert theirs.model_config_id == other_owner.id


# ---- 二、默认顶上 -------------------------------------------------------------


def test_delete_of_default_promotes_a_verified_replacement(db):
    keep = make_config(db, title="验证过的另一份")
    doomed = make_config(db, title="默认这份", is_default=True)

    svc(db).delete(doomed.id)

    db.expire_all()
    assert svc(db).get_default(ModelPurpose.CHAT).id == keep.id
    assert keep.is_default is True


def test_delete_of_default_without_a_verified_fallback_leaves_none(db):
    """没验证过的不能顶：宁可可读地「还没配模型」，也不要悄悄拿一份坏配置去调用。"""
    make_config(db, title="没测过", last_test_ok=False)
    doomed = make_config(db, title="默认这份", is_default=True)

    svc(db).delete(doomed.id)

    assert svc(db).get_default(ModelPurpose.CHAT) is None
    assert db.scalar(select(ModelConfig).where(ModelConfig.is_default.is_(True))) is None


# ---- 三、列表与默认查询：所有读都得带 owner + purpose -------------------------


def test_list_puts_the_default_first_then_the_newest(db):
    oldest = make_config(db, title="最早")
    middle = make_config(db, title="默认这份")
    newest = make_config(db, title="最新")

    svc(db).set_default(middle.id)

    # 设置页把默认那份顶在最前，其余按新旧；「默认优先 + id 倒序」是这个界面唯一的
    # 排序契约，改成按标题排会让用户以为默认位换了人。
    assert [config.id for config in svc(db).list()] == [middle.id, newest.id, oldest.id]


def test_list_filters_by_purpose_without_leaking_the_other_kind(db):
    chat = make_config(db, title="对话那份")
    embedding = make_config(db, title="向量那份", purpose=ModelPurpose.EMBEDDING)

    assert [config.id for config in svc(db).list(ModelPurpose.CHAT)] == [chat.id]
    assert [config.id for config in svc(db).list(ModelPurpose.EMBEDDING)] == [embedding.id]
    assert [config.id for config in svc(db).list()] == [embedding.id, chat.id]


def test_list_never_shows_another_owners_configs(db):
    mine = make_config(db, title="我的")
    theirs = make_config(db, owner_id=OTHER, title="别人的")

    assert [config.id for config in svc(db).list()] == [mine.id]
    assert [config.id for config in svc(db, OTHER).list()] == [theirs.id]
    # 别人的 id 在这个用户视角里根本不存在：报 404 而不是 403，否则这一层就是一台
    # 配置 id 探测机。
    with pytest.raises(LookupError) as caught:
        svc(db).get(theirs.id)
    assert "不存在" in str(caught.value)


def test_the_default_is_one_row_per_owner_and_per_purpose(db):
    mine = make_config(db, title="我的对话默认", is_default=True)
    theirs = make_config(db, owner_id=OTHER, title="别人的对话默认", is_default=True)
    mine_embedding = make_config(
        db, title="我的向量默认", purpose=ModelPurpose.EMBEDDING, is_default=True
    )

    assert svc(db).get_default(ModelPurpose.CHAT).id == mine.id
    assert svc(db, OTHER).get_default(ModelPurpose.CHAT).id == theirs.id
    assert svc(db).get_default(ModelPurpose.EMBEDDING).id == mine_embedding.id

    # 同一 owner + 同一用途只留一个默认；切到别处要把这一档清掉，且不跨档传染。
    challenger = make_config(db, title="后来居上")
    svc(db).set_default(challenger.id)
    assert svc(db).get_default(ModelPurpose.CHAT).id == challenger.id
    assert db.get(ModelConfig, mine.id).is_default is False
    assert db.get(ModelConfig, mine_embedding.id).is_default is True


def test_the_first_config_of_a_purpose_becomes_the_default_on_its_own(db):
    first = svc(db).create(create_payload(title="对话第一份"))
    assert first.is_default is True
    assert svc(db).create(create_payload(title="对话第二份")).is_default is False
    # embedding 是另一档，它的第一份同样自动顶上——新账号不用逐档多点一次。
    assert svc(db).create(
        create_payload(title="向量第一份", purpose=ModelPurpose.EMBEDDING)
    ).is_default is True


def test_default_model_config_only_answers_the_query(db):
    """「能不能拿来用」不在这道门上：默认那份没通过测试也照样被查出来。"""
    broken = make_config(db, title="默认但没测过", is_default=True, last_test_ok=False)

    assert default_model_config(db, OWNER, ModelPurpose.CHAT).id == broken.id
    assert svc(db).get_default(ModelPurpose.EMBEDDING) is None


# ---- 四、掩码：to_item 是往外递配置的唯一口子 --------------------------------


def test_create_strips_the_trailing_slash_off_the_base_url(db):
    assert svc(db).create(
        create_payload(base_url="http://localhost:1/v1/")
    ).base_url == "http://localhost:1/v1"


def test_the_vision_flag_survives_create_and_to_item(db):
    """对话页只从 to_item 看得到这一列；漏掉它等于所有模型都被判成不收图。"""
    item = svc(db).to_item(svc(db).create(create_payload(supports_vision=True)))

    assert item.supports_vision is True


def test_a_stored_key_comes_back_masked_and_never_in_plaintext(db):
    item = svc(db).to_item(svc(db).create(create_payload(api_key=KEY)))

    assert item.api_key == MASKED_KEY
    assert item.api_key_error is None
    # 尾四位是给用户认「这是哪一把」的，其余一律不出去。
    assert item.api_key.startswith(MASK_PREFIX)
    assert len(item.api_key) == len(MASK_PREFIX) + 4
    assert KEY not in str(item.model_dump())


def test_the_database_holds_ciphertext_not_the_plaintext(db):
    config = svc(db).create(create_payload(api_key=KEY))
    db.refresh(config)

    assert KEY.encode() not in config.api_key_enc
    assert decrypt(config.api_key_enc) == KEY


@pytest.mark.parametrize(
    "secret,expected",
    [
        ("", ""),  # 从来没填过：空串，不写错误
        ("ab", MASK_PREFIX),  # 短于四位：连尾四位都不给，免得把整把 key 端出去
        ("abcd", f"{MASK_PREFIX}abcd"),
        (KEY, MASKED_KEY),
    ],
)
def test_the_mask_only_ever_shows_the_last_four(db, secret, expected):
    assert (
        svc(db).to_item(svc(db).create(create_payload(api_key=secret))).api_key == expected
    )


def test_a_key_that_no_longer_decrypts_says_so_instead_of_vanishing(db):
    """等价于加密密钥换过一把（或那一列坏了）：界面必须报出来。

    静默成空串跟「用户从来没填过 key」长得一模一样，而这两件事的处置完全相反——
    前者要用户重新填凭据，后者什么也不用做。
    """
    config = svc(db).create(create_payload(api_key=KEY))
    config.api_key_enc = Fernet(Fernet.generate_key()).encrypt(KEY.encode())
    db.commit()

    item = svc(db).to_item(config)
    assert item.api_key == ""
    assert item.api_key_error is not None and "重新填写" in item.api_key_error


# ---- 五、凭据轮换：掩码回传等于「别动它」 ------------------------------------


def test_a_masked_or_absent_key_leaves_the_credential_alone(db):
    config = svc(db).create(create_payload(api_key=KEY))
    config.vector_size = 1024
    config.last_test_ok = True
    db.commit()
    stored = config.api_key_enc

    # 表单里 api_key 那格预填的就是掩码，用户只改个标题也会把它原样提交回来。
    for payload in (
        ModelConfigUpdate(api_key=MASKED_KEY),
        ModelConfigUpdate(api_key=""),
        ModelConfigUpdate(api_key=None),
        ModelConfigUpdate(title="只改标题"),
    ):
        updated = svc(db).update(config.id, payload)
        assert decrypt(updated.api_key_enc) == KEY, payload
        # 没动凭据就不该牵动测试态与维度。
        assert updated.last_test_ok is True, payload
        assert updated.vector_size == 1024, payload


def test_a_new_key_rotates_the_credential_and_clears_the_verified_state(db):
    config = svc(db).create(create_payload(api_key=KEY))
    config.vector_size = 1024
    config.last_test_ok = True
    config.last_test_error = None
    db.commit()

    updated = svc(db).update(config.id, ModelConfigUpdate(api_key="sk-prov-new9999"))

    assert decrypt(updated.api_key_enc) == "sk-prov-new9999"
    assert updated.last_test_ok is False
    assert updated.last_test_error is None
    # 维度是按旧模型测出来的，换了凭据这个数就没有出处了；Qdrant 的 collection
    # 按它建、建好改不了，所以宁可清空重测。
    assert updated.vector_size is None


def test_rotating_a_key_takes_the_default_flag_with_it(db):
    doomed = svc(db).create(create_payload(title="默认这份", api_key=KEY))
    assert doomed.is_default is True  # 该档第一份，自动顶上
    other = make_config(db, title="另一份")

    updated = svc(db).update(doomed.id, ModelConfigUpdate(api_key="sk-prov-new9999"))

    assert updated.is_default is False
    # 和删除那一路不一样：改凭据不顺手顶上一份。默认位空着至少能在界面上读出
    # 「还没配模型」，顶一份没验证过的等于把所有人的对话交给它。
    assert svc(db).get_default(ModelPurpose.CHAT) is None
    assert db.get(ModelConfig, other.id).is_default is False


def test_a_base_url_change_counts_as_a_credential_change(db):
    config = svc(db).create(create_payload(api_key=KEY))
    config.is_default = True
    config.last_test_ok = True
    db.commit()

    updated = svc(db).update(config.id, ModelConfigUpdate(base_url="http://other:2/v1"))

    assert updated.base_url == "http://other:2/v1"
    assert updated.last_test_ok is False
    assert updated.is_default is False
    # 换的是地址，key 还是那一把，不该被顺手抹掉。
    assert decrypt(updated.api_key_enc) == KEY


def test_the_same_url_written_back_does_not_wipe_the_verified_state(db):
    """比较是在剥掉尾斜杠之后做的：原样回传同一个地址（带不带斜杠）不算改动。"""
    config = svc(db).create(create_payload(api_key=KEY))
    config.is_default = True
    config.last_test_ok = True
    db.commit()

    updated = svc(db).update(config.id, ModelConfigUpdate(base_url="http://localhost:1/v1/"))

    assert updated.base_url == "http://localhost:1/v1"
    assert updated.last_test_ok is True
    assert updated.is_default is True


def test_update_writes_back_only_the_fields_you_sent(db):
    config = svc(db).create(create_payload(api_key=KEY, title="原", remark="原备注"))

    updated = svc(db).update(
        config.id, ModelConfigUpdate(title="改标题", model_name=None, remark=None)
    )
    assert (updated.title, updated.model_name, updated.remark) == (
        "改标题",
        "unit-model",
        "原备注",
    )
    # 显式传 null 也清不掉：实现只写非 None 的值。要「清空备注」得改语义，这里先
    # 钉住现状，免得日后顺手改成静默丢数据。
    assert decrypt(updated.api_key_enc) == KEY


def test_purpose_can_be_moved_without_touching_the_credential(db):
    config = svc(db).create(create_payload(api_key=KEY))
    config.last_test_ok = True
    db.commit()

    updated = svc(db).update(config.id, ModelConfigUpdate(purpose=ModelPurpose.EMBEDDING))

    assert updated.purpose == ModelPurpose.EMBEDDING.value
    assert decrypt(updated.api_key_enc) == KEY
    assert updated.last_test_ok is True
    # 默认是按用途各一份：挪了用途，原来那档的默认位就不再认它。
    assert svc(db).get_default(ModelPurpose.CHAT) is None
    assert svc(db).get_default(ModelPurpose.EMBEDDING).id == updated.id


def test_updating_another_owners_config_reads_as_404(db):
    theirs = make_config(db, owner_id=OTHER, title="别人的", api_key=KEY)

    with pytest.raises(LookupError):
        svc(db).update(theirs.id, ModelConfigUpdate(title="试图改名"))
    db.expire_all()
    assert theirs.title == "别人的"
    assert decrypt(theirs.api_key_enc) == KEY


# ---- 六、测试结论与默认切换 --------------------------------------------------


def test_record_test_result_keeps_the_error_within_the_column(db):
    config = svc(db).create(create_payload(api_key=KEY))

    svc(db).record_test_result(config, ok=False, message="服务商报错" * 300)
    assert config.last_test_ok is False
    assert len(config.last_test_error) == 512
    assert config.last_tested_at is not None

    svc(db).record_test_result(config, ok=True, message="")
    assert (config.last_test_ok, config.last_test_error) == (True, None)


def test_record_test_result_writes_the_dimension_only_when_measured(db):
    config = make_config(db, purpose=ModelPurpose.EMBEDDING, vector_size=1024)

    # 没测出维度时不能把已有的数抹成 None——那个数决定 Qdrant collection 的建法。
    svc(db).record_test_result(config, ok=True, message="")
    assert config.vector_size == 1024

    svc(db).record_test_result(config, ok=True, message="", vector_size=3072)
    assert config.vector_size == 3072


def test_set_default_requires_a_verified_config(db):
    untested = make_config(db, title="没测过", last_test_ok=False)

    with pytest.raises(ValueError) as caught:
        svc(db).set_default(untested.id)
    assert "连通性测试" in str(caught.value)
    assert untested.is_default is False


def test_set_default_is_a_404_for_another_owners_config(db):
    theirs = make_config(db, owner_id=OTHER, title="别人的")

    with pytest.raises(LookupError):
        svc(db).set_default(theirs.id)


# ---- 七、路由层：掩码要真的带到线上 -----------------------------------------


@pytest.fixture()
def api(tmp_path):
    """真 app 打这六条配置路由，库还是文件 SQLite。

    ``require_user`` 不覆盖，只覆盖 ``get_db``：这一层要钉的是响应体里到底出现了
    什么，鉴权本身在 ``test_auth_users_api.py`` 里钉过。刻意不进 ``with``——TestClient
    的上下文管理器会跑 lifespan，那会拿开发库做启动初始化。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'model_config_api.db'}",
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


def http_body(api, method: str, path: str, token: str, *, json_body=None):
    """带 token 发一个请求，只回（状态码, 响应, 响应文本）。"""
    kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
    if json_body is not None:
        kwargs["json"] = json_body
    response = getattr(api.client, method)(path, **kwargs)
    return response.status_code, response.json(), response.text


def created_over_http(api, **over) -> dict:
    body = {
        "purpose": "chat",
        "title": "线上这份",
        "model_name": "unit-model",
        "base_url": "http://localhost:1/v1",
        "api_key": KEY,
    }
    body.update(over)
    status_code, envelope, _ = http_body(
        api, "post", "/api/settings/models", api.tokens["admin"], json_body=body
    )
    assert (status_code, envelope["code"]) == (200, 0)
    return envelope["data"]


def test_the_http_response_carries_only_the_mask(api):
    config = created_over_http(api)
    assert config["api_key"] == MASKED_KEY
    assert config["api_key_error"] is None

    status_code, envelope, raw = http_body(
        api, "get", "/api/settings/models", api.tokens["admin"]
    )
    assert (status_code, envelope["code"]) == (200, 0)
    assert envelope["data"]["total"] == 1
    assert MASK_PREFIX in raw
    # 明文和密文列都不许出现在响应里。
    assert KEY not in raw
    assert "api_key_enc" not in raw

    api.db.expire_all()
    assert decrypt(api.db.get(ModelConfig, config["id"]).api_key_enc) == KEY


def test_a_round_trip_of_the_mask_through_http_keeps_the_key(api):
    """SPA 会把表单里预填的掩码原样 PUT 回来，这一把 key 必须活下来。"""
    config = created_over_http(api)
    api.db.expire_all()
    stored = api.db.get(ModelConfig, config["id"]).api_key_enc

    status_code, envelope, _ = http_body(
        api,
        "put",
        f"/api/settings/models/{config['id']}",
        api.tokens["admin"],
        json_body={"title": "只改标题", "api_key": MASKED_KEY},
    )
    assert (status_code, envelope["code"]) == (200, 0)
    assert envelope["data"]["title"] == "只改标题"

    api.db.expire_all()
    assert api.db.get(ModelConfig, config["id"]).api_key_enc == stored


def test_a_bogus_purpose_query_is_a_422_envelope(api):
    status_code, envelope, raw = http_body(
        api, "get", "/api/settings/models?purpose=whatever", api.tokens["admin"]
    )
    assert (status_code, envelope["code"], envelope["message"]) == (
        422,
        422,
        "请求参数不合法",
    )
    # 统一信封：SPA 的拦截器只读响应体的 code，FastAPI 原生的 detail 数组会绕过它。
    assert "detail" not in raw


def test_setting_a_default_over_http_refuses_an_unverified_config(api):
    config = created_over_http(api)

    status_code, envelope, _ = http_body(
        api, "post", f"/api/settings/models/{config['id']}/default", api.tokens["admin"]
    )
    # 业务拒绝走 HTTP 200 + code=-1，和 404 不是一类。
    assert (status_code, envelope["code"]) == (200, -1)
    assert "连通性测试" in envelope["message"]


def test_the_model_routes_answer_404_for_another_owners_config(api):
    config = created_over_http(api)

    for method, kwargs in (
        ("put", {"json_body": {"title": "试图改名"}}),
        ("delete", {}),
        ("post", {}),
    ):
        path = f"/api/settings/models/{config['id']}"
        if method == "post":
            path = f"{path}/default"
        status_code, envelope, _ = http_body(api, method, path, api.tokens["worker"], **kwargs)
        assert (status_code, envelope["code"], envelope["message"]) == (
            404,
            404,
            "模型配置不存在",
        )

    api.db.expire_all()
    assert api.db.get(ModelConfig, config["id"]).title == "线上这份"


def test_the_reasoning_switch_survives_a_round_trip_over_http(api):
    """SPA 存的是整份表单：把已保存的参数原样 PUT 回来，不能把它读成「清空」。"""
    config = created_over_http(
        api, reasoning_mode="on", request_extra={"thinking": {"type": "enabled"}}
    )
    assert (config["reasoning_mode"], config["request_extra"]) == (
        "on",
        {"thinking": {"type": "enabled"}},
    )

    status_code, envelope, _ = http_body(
        api,
        "put",
        f"/api/settings/models/{config['id']}",
        api.tokens["admin"],
        json_body={"title": "只改标题", "reasoning_mode": "on", "request_extra": config["request_extra"]},
    )
    assert (status_code, envelope["code"]) == (200, 0)
    assert envelope["data"]["request_extra"] == {"thinking": {"type": "enabled"}}

    status_code, envelope, _ = http_body(
        api,
        "put",
        f"/api/settings/models/{config['id']}",
        api.tokens["admin"],
        json_body={"reasoning_mode": "auto", "request_extra": None},
    )
    assert (status_code, envelope["code"]) == (200, 0)
    assert (envelope["data"]["reasoning_mode"], envelope["data"]["request_extra"]) == (
        "auto",
        None,
    )


# ---- 八、思维链开关：三态各走各的客户端，参数只在「开启」时进请求体 ------------


def test_a_new_config_defaults_to_auto_and_carries_no_extra_params(db):
    config = svc(db).create(create_payload(api_key=KEY))

    assert (config.reasoning_mode, config.request_extra) == ("auto", None)
    item = svc(db).to_item(config)
    assert (item.reasoning_mode.value, item.request_extra) == ("auto", None)


def test_create_keeps_the_switch_and_the_passthrough_object(db):
    config = svc(db).create(
        create_payload(
            api_key=KEY,
            reasoning_mode="on",
            request_extra={"thinking": {"type": "enabled"}},
        )
    )
    db.refresh(config)

    assert config.reasoning_mode == "on"
    assert config.request_extra == {"thinking": {"type": "enabled"}}
    item = svc(db).to_item(config)
    assert (item.reasoning_mode.value, item.request_extra) == (
        "on",
        {"thinking": {"type": "enabled"}},
    )


def test_an_absent_request_extra_leaves_it_alone_but_null_clears_it(db):
    """表单每次都整份提交，所以「没传」和「传了个 null」必须是两件事。"""
    config = svc(db).create(
        create_payload(api_key=KEY, reasoning_mode="on", request_extra={"thinking": {"type": "enabled"}})
    )
    config.last_test_ok = True
    db.commit()

    untouched = svc(db).update(config.id, ModelConfigUpdate(title="只改标题"))
    assert untouched.request_extra == {"thinking": {"type": "enabled"}}
    assert untouched.last_test_ok is True

    cleared = svc(db).update(config.id, ModelConfigUpdate(request_extra=None))
    assert cleared.request_extra is None
    # 空对象等同于清空，不该在库里留下一份 {} 让人以为参数还在。
    emptied = svc(db).update(
        config.id, ModelConfigUpdate(request_extra={}, reasoning_mode="on")
    )
    assert emptied.request_extra is None


@pytest.mark.parametrize(
    "extra,expect_test_ok",
    [({"thinking": {"type": "disabled"}}, False), ({"thinking": {"type": "enabled"}}, True)],
)
def test_only_a_real_change_to_the_params_invalidates_the_last_test(
    db, extra, expect_test_ok
):
    """透传参数会真的进请求体，改对了就等于换了一份没验证过的配置；原样回传不算改。"""
    config = svc(db).create(
        create_payload(api_key=KEY, reasoning_mode="on", request_extra={"thinking": {"type": "enabled"}})
    )
    config.is_default = True
    config.last_test_ok = True
    db.commit()

    updated = svc(db).update(config.id, ModelConfigUpdate(request_extra=extra))

    assert updated.last_test_ok is expect_test_ok
    assert updated.is_default is (True if expect_test_ok else False)


def test_moving_the_switch_by_itself_keeps_the_verified_state(db):
    """关不关思考跟这份配置通不通没关系，不该逼人重测一次。"""
    config = svc(db).create(create_payload(api_key=KEY))
    config.is_default = True
    config.last_test_ok = True
    db.commit()

    for mode in ("on", "off", "auto"):
        updated = svc(db).update(config.id, ModelConfigUpdate(reasoning_mode=mode))
        assert updated.reasoning_mode == mode
        assert updated.last_test_ok is True, mode
        assert updated.is_default is True, mode


def test_off_uses_the_plain_client_and_on_is_the_only_mode_that_sends_params(db):
    """三态各自的行为：off 连解析层都不接 reasoning_content，on 才把参数并进请求体。"""
    extra = {"thinking": {"type": "enabled"}}
    on = svc(db).create(
        create_payload(api_key=KEY, reasoning_mode="on", request_extra=extra)
    )
    auto = svc(db).create(create_payload(api_key=KEY, title="自动那份", request_extra=extra))
    off = svc(db).create(
        create_payload(
            api_key=KEY, title="关闭那份", reasoning_mode="off", request_extra=extra
        )
    )

    on_client = build_chat_model(on)
    assert isinstance(on_client, ReasoningChatOpenAI)
    assert on_client.extra_body == extra

    auto_client = build_chat_model(auto)
    assert isinstance(auto_client, ReasoningChatOpenAI)
    # 没选「开启」就不该往请求体里塞私有参数，否则等于替用户猜厂商。
    assert auto_client.extra_body is None

    off_client = build_chat_model(off)
    assert isinstance(off_client, ChatOpenAI)
    assert not isinstance(off_client, ReasoningChatOpenAI)
    assert off_client.extra_body is None


def test_only_the_on_mode_puts_the_params_on_the_wire(db):
    """``extra_body`` 是 openai SDK 平展进 JSON body 的那一个键，不是客户端字段。

    钉的是 ``_get_request_payload`` 这个接缝：LangChain 若改了实现，这条会红，
    而它红了就等于「用户填的参数不再上线」——那正是这个开关唯一的存在理由。
    """
    from langchain_core.messages import HumanMessage

    extra = {"thinking": {"type": "enabled"}}
    on = svc(db).create(create_payload(api_key=KEY, reasoning_mode="on", request_extra=extra))
    auto = svc(db).create(create_payload(api_key=KEY, title="自动那份", reasoning_mode="auto",
                                         request_extra=extra))

    on_payload = build_chat_model(on)._get_request_payload([HumanMessage(content="hi")])
    auto_payload = build_chat_model(auto)._get_request_payload([HumanMessage(content="hi")])

    assert on_payload.get("extra_body") == extra
    # 没选「开启」就不能把私有参数发出去：发给不认识它的服务商是一句 400。
    assert auto_payload.get("extra_body") is None


def test_the_off_mode_drops_the_providers_thinking_stream_at_parse_time(db):
    """同一个上游增量：装 reasoning 的客户端抬进 additional_kwargs，原生客户端直接丢。

    ``off`` 靠的就是这一步——它不给「思考过程」留任何下游可读的入口，
    所以落库的 timeline 与 SSE 都不会出现 reason 事件。
    """
    from langchain_core.messages import AIMessageChunk

    chunk = {
        "id": "1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "unit-test",
        "choices": [
            {
                "index": 0,
                "delta": {"reasoning_content": "先想一想", "content": ""},
                "finish_reason": None,
            }
        ],
    }
    on = svc(db).create(create_payload(api_key=KEY, reasoning_mode="on"))
    off = svc(db).create(create_payload(api_key=KEY, title="关闭那份", reasoning_mode="off"))

    reasoning = build_chat_model(on)._convert_chunk_to_generation_chunk(
        chunk, AIMessageChunk, None
    )
    plain = build_chat_model(off)._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)

    assert reasoning.message.additional_kwargs["reasoning_content"] == "先想一想"
    assert "reasoning_content" not in plain.message.additional_kwargs


# ---- temperature -----------------------------------------------------------


def test_temperature_round_trip_over_http(api):
    """temperature 能存能取，0-100 整数原样回来。"""
    config = created_over_http(api, temperature=70)
    assert config["temperature"] == 70


def test_temperature_null_means_unspecified(api):
    """temperature 留空（null）表示「不指定」，API 返回 null。"""
    config = created_over_http(api, temperature=None)
    assert config["temperature"] is None


def test_explicit_null_clears_temperature(db):
    """表单每次都整份提交，显式传 null 必须能清空已保存的温度。"""
    config = svc(db).create(create_payload(api_key=KEY, temperature=50))
    assert config.temperature == 50

    updated = svc(db).update(config.id, ModelConfigUpdate(temperature=None))
    assert updated.temperature is None


def test_resolve_temperature_priority(db):
    """模型配置（硬约束）> 智能体（偏好）> 服务商默认（None）。"""
    from app.services.providers import resolve_temperature

    config_with_temp = svc(db).create(create_payload(api_key=KEY, temperature=60))
    config_no_temp = svc(db).create(create_payload(api_key=KEY, title="另一份", temperature=None))

    # 模型配置有值：盖过智能体——「Kimi 只接受 1」这类限制只需在模型这一处设一次
    assert resolve_temperature(config_with_temp, 30) == 0.6
    assert resolve_temperature(config_with_temp, 80) == 0.6

    # 模型配置 null：落到智能体的偏好
    assert resolve_temperature(config_no_temp, 30) == 0.3

    # 都 null：返回 None（让 langchain 剔除）
    assert resolve_temperature(config_no_temp, None) is None


def test_build_chat_model_omits_temperature_when_none(db):
    """temperature=None 时 langchain 应该把该字段从请求体里剔除。"""
    config = svc(db).create(create_payload(api_key=KEY))
    client = build_chat_model(config, temperature=None)
    # _get_request_payload 是 langchain 内部方法，返回发给上游的参数字典。
    payload = client._get_request_payload([{"role": "user", "content": "hi"}])
    assert "temperature" not in payload

