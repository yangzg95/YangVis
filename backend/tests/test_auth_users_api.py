"""认证与账号管理两条路由的契约：登录分支、统一信封、管理员闸门。

``login_guard`` 与图形验证码的机制本身在 ``tests/test_login_guard.py`` 里已经钉过；
这一层只管只有真请求才暴露得出来的东西：

- 「密码错」「用户不存在」「账号禁用」三种失败必须是同一个 code 同一句话——否则
  这个接口就是一台用户名探测机。
- 401/403 是 ``app.main`` 里的异常处理器套的信封。SPA 的 axios 拦截器只读响应体
  的 ``code``，FastAPI 原生的 ``{"detail": ...}`` 会让 401 绕过跳转逻辑。
- 鉴权链全程是真的：``require_user`` 每次回库读 ``status``，「禁用下次请求即生效」
  这条契约只有拿真 token 打真路由才验得出来，覆盖掉依赖等于把它跳过。

SQLite 用文件库；密码哈希把迭代次数调低（迭代数写在哈希串里，verify 跟着它走），
免得一个用例为 26 万轮 PBKDF2 付费。
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.entities import SysUser
from app.security import create_access_token, create_ws_ticket, hash_password
from app.services import captcha, login_guard
from app.services.captcha import issue_captcha

settings = get_settings()

ADMIN_PW = "admin-password"
WORKER_PW = "worker-password"
NEW_PW = "another-password"
MAX_FAILURES = int(settings.AUTH_LOGIN_MAX_FAILURES)
# TestClient 的对端地址恒定，防爆破计数的 IP 维就用它。
IP = "testclient"


# ---- 环境 ------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    """真 app + 文件库里的两个账号（一个管理员、一个普通用户）。

    这里刻意不覆盖 ``require_user``：token 怎么签、怎么验、禁用怎么落到每一次请求
    上，正是这一层要验的东西。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()

    db.add_all(
        [
            SysUser(
                username="admin",
                password_hash=hash_password(ADMIN_PW, iterations=2),
                nickname="管理员",
                email="admin@example.com",
                status=True,
                is_admin=True,
            ),
            SysUser(
                username="worker",
                password_hash=hash_password(WORKER_PW, iterations=2),
                nickname="张三",
                ops_write=True,
                status=True,
                is_admin=False,
            ),
        ]
    )
    db.commit()

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        yield SimpleNamespace(db=db, client=client)
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean_counters():
    """防爆破计数是进程内存，用例之间必须互不干扰。"""
    login_guard._counters.clear()
    yield
    login_guard._counters.clear()


def login_request(client, username: str, password: str, *, code: str | None = None):
    """带一张真验证码提交登录，返回原始响应。``code`` 传错值用来走验证码失败分支。"""
    captcha_id, answer = issue_captcha()
    if code is None:
        code = captcha._cipher().decrypt(captcha_id.encode("ascii")).decode("utf-8")
    return client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": password,
            "captcha_id": captcha_id,
            "captcha_code": code,
        },
    )


def login(client, username: str, password: str, *, code: str | None = None) -> dict:
    return login_request(client, username, password, code=code).json()


def token_of(env, username: str = "admin", password: str = ADMIN_PW) -> str:
    body = login(env.client, username, password)
    assert body["code"] == 0, body
    return body["data"]["access_token"]


def auth(env, **kwargs) -> dict:
    return {"Authorization": f"Bearer {token_of(env, **kwargs)}"}


def row_of(db, username: str) -> SysUser:
    return db.scalar(select(SysUser).where(SysUser.username == username))


def create(db, username: str, *, password: str = WORKER_PW, **fields) -> SysUser:
    row = SysUser(
        username=username,
        password_hash=hash_password(password, iterations=2),
        status=True,
        is_admin=False,
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- 一、验证码与登录成功路径 ------------------------------------------------


def test_captcha_endpoint_returns_a_png_and_an_opaque_token(env):
    body = env.client.get("/api/auth/captcha").json()
    assert body["code"] == 0
    payload = body["data"]
    assert payload["image"].startswith("data:image/png;base64,")
    assert base64.b64decode(payload["image"].split(",", 1)[1]).startswith(b"\x89PNG")
    # 答案只以密文存在：Fernet 令牌（版本字节 0x80 → "gAAAAA" 前缀）而不是明文短码，
    # 否则答案空间那么小，抓一次响应就能离线穷举。
    assert payload["captcha_id"].startswith("gAAAAA")


def test_login_returns_a_token_that_the_rest_endpoints_accept(env):
    body = login(env.client, "admin", ADMIN_PW)
    assert body["code"] == 0
    info = body["data"]["user_info"]
    assert info["user_id"] == row_of(env.db, "admin").id
    assert info["roles"] == ["admin"]
    # 管理员天然是运维写超集，权限位从这两个布尔推导，不是前端自己填的。
    assert info["permissions"] == ["ops:write"]

    me = env.client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['data']['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "admin"
    # 登录成功要留下最近登录时间，管理页那一列读的就是它。
    assert row_of(env.db, "admin").last_login_at is not None


@pytest.mark.parametrize(
    ("username", "ops_write", "expected_roles", "expected_permissions"),
    [
        ("admin", False, ["admin"], ["ops:write"]),
        ("worker", True, ["user"], ["ops:write"]),
        ("worker", False, ["user"], []),
    ],
)
def test_permissions_are_derived_from_the_two_bits(
    env, username, ops_write, expected_roles, expected_permissions
):
    row_of(env.db, username).ops_write = ops_write
    env.db.commit()

    info = login(env.client, username, ADMIN_PW if username == "admin" else WORKER_PW)["data"][
        "user_info"
    ]
    assert info["roles"] == expected_roles
    assert info["permissions"] == expected_permissions
    assert info["ops_write"] is ops_write


def test_logout_is_a_success_envelope(env):
    body = env.client.post("/api/auth/logout", headers=auth(env)).json()
    assert body["code"] == 0
    assert body["data"] is None


# ---- 二、失败分支：不能变成用户名探测机 --------------------------------------


def test_three_failures_are_the_same_answer(env):
    wrong_password = login_request(env.client, "admin", "不对的密码")
    no_such_user = login_request(env.client, "charlie", ADMIN_PW)
    row_of(env.db, "worker").status = False
    env.db.commit()
    disabled = login_request(env.client, "worker", WORKER_PW)

    expected = {"code": 401, "message": "用户名或密码错误", "data": None}
    for response in (wrong_password, no_such_user, disabled):
        # 连状态码都统一：业务失败恒 HTTP 200，失败只由响应体的 code 表达，
        # 前端按 code 分流。三种原因同形，多一个字的差别都够别人枚举有效用户名。
        assert response.status_code == 200
        assert response.json() == expected


def test_captcha_is_gatekept_before_the_password_and_does_not_count(env):
    body = login(env.client, "admin", ADMIN_PW, code="0000")
    assert body["code"] == -1
    assert "验证码" in body["message"]
    # 验证码错误不计入密码失败次数：否则任何人拿别人的用户名乱填验证码，
    # 就能替那个用户触发锁定（DoS）。
    assert login_guard._counters == {}


def test_lockout_trips_after_the_configured_failures(env):
    for _ in range(MAX_FAILURES - 1):
        assert login(env.client, "admin", "不对的密码")["code"] == 401
    assert login_guard.locked_seconds("admin", IP) == 0

    assert login(env.client, "admin", "不对的密码")["code"] == 401

    # 锁定期间连密码对都不给过，而且锁校验排在验证码之前——不然攻击者还能
    # 拿这段窗口继续白嫖验证码校验。
    body = env.client.post(
        "/api/auth/login",
        json={
            "username": "admin",
            "password": ADMIN_PW,
            "captcha_id": "not-a-fernet-token",
            "captcha_code": "ABCD",
        },
    ).json()
    assert body["code"] == 429
    assert "分钟后再试" in body["message"]


def test_the_lock_key_collapses_case_and_surrounding_spaces(env):
    # 计数键统一小写、去掉首尾空格。线上 MySQL 默认排序规则下 'Worker' 命中的就是
    # 'worker' 这一行，不收敛大小写的话每换一种写法就多一轮尝试额度。
    for name in ("Worker", "WORKER", " worker ", "wOrKeR", "WORKER "):
        assert login(env.client, name, "不对的密码")["code"] == 401

    assert set(login_guard._counters) == {("worker", IP), ("", IP)}
    assert login_guard.locked_seconds("worker", IP) > 0
    # 真用户此刻被自己的大小写变体烧光了额度，密码对也进不来。
    assert login(env.client, "worker", WORKER_PW)["code"] == 429


def test_a_successful_login_clears_that_users_failure_window(env):
    for _ in range(MAX_FAILURES - 1):
        login(env.client, "admin", "不对的密码")
    assert login(env.client, "admin", ADMIN_PW)["code"] == 0
    assert login_guard.locked_seconds("admin", IP) == 0

    # 清零是真的清零：再失败 上限-1 次仍不该锁。
    for _ in range(MAX_FAILURES - 1):
        login(env.client, "admin", "不对的密码")
    assert login_guard.locked_seconds("admin", IP) == 0


# ---- 三、require_user 在路由层的表现 -----------------------------------------


def test_missing_token_is_a_401_inside_the_envelope(env):
    response = env.client.get("/api/auth/me")
    assert response.status_code == 401
    # 关键在这条：SPA 拦截器读的是 body.code，detail 形态的原生错误会绕过跳转登录页。
    assert response.json() == {"code": 401, "message": "未登录或登录已过期", "data": None}


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_prefix_is_case_insensitive(env, scheme):
    response = env.client.get(
        "/api/auth/me", headers={"Authorization": f"{scheme} {token_of(env)}"}
    )
    assert response.status_code == 200


def test_a_forged_or_expired_token_is_rejected(env):
    forged = jwt.encode(
        {"sub": "1", "username": "admin", "is_admin": True},
        "somebody-elses-secret",
        algorithm=settings.AUTH_JWT_ALGORITHM,
    )
    expired = create_access_token(user_id=1, username="admin", is_admin=True, ttl_seconds=-1)
    for token in (forged, expired):
        response = env.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.json()["message"] == "未登录或登录已过期"


def test_a_websocket_ticket_does_not_work_against_a_rest_endpoint(env):
    # 票走 query string、会进访问日志，拿它调 REST 等于把认证降到日志可见的强度。
    ticket = create_ws_ticket(
        user_id=row_of(env.db, "admin").id, username="admin", is_admin=True
    )
    response = env.client.get("/api/auth/me", headers={"Authorization": f"Bearer {ticket}"})
    assert response.status_code == 401


def test_a_token_outlives_its_user_but_not_the_disable(env):
    row = create(env.db, "temp", nickname="外包")
    headers = auth(env, username="temp", password=WORKER_PW)
    assert env.client.get("/api/auth/me", headers=headers).status_code == 200

    # 无状态 JWT 撤不掉，「禁用」靠的是每次请求回库读 status。
    row.status = False
    env.db.commit()
    assert env.client.get("/api/auth/me", headers=headers).status_code == 401
    row.status = True
    env.db.commit()
    assert env.client.get("/api/auth/me", headers=headers).status_code == 200


# ---- 四、自助改密 -------------------------------------------------------------


def test_change_password_checks_the_old_one_and_refuses_a_no_op(env):
    headers = auth(env)

    wrong_old = env.client.post(
        "/api/auth/password",
        headers=headers,
        json={"old_password": "不对的原密码", "new_password": NEW_PW},
    ).json()
    assert wrong_old == {"code": -1, "message": "原密码不正确", "data": None}

    unchanged = env.client.post(
        "/api/auth/password",
        headers=headers,
        json={"old_password": ADMIN_PW, "new_password": ADMIN_PW},
    ).json()
    assert unchanged["code"] == -1
    assert "不能与原密码相同" in unchanged["message"]

    ok = env.client.post(
        "/api/auth/password",
        headers=headers,
        json={"old_password": ADMIN_PW, "new_password": NEW_PW},
    ).json()
    assert ok["code"] == 0
    assert login(env.client, "admin", NEW_PW)["code"] == 0
    assert login(env.client, "admin", ADMIN_PW)["code"] == 401
    # 改密不撤销已有 token（logout 同理）：这是写在注释里的取舍，钉住防止日后走偏。
    assert env.client.get("/api/auth/me", headers=headers).status_code == 200


def test_a_too_short_new_password_is_rejected_by_validation(env):
    response = env.client.post(
        "/api/auth/password",
        headers=auth(env),
        json={"old_password": ADMIN_PW, "new_password": "abc"},
    )
    assert response.status_code == 422
    assert response.json() == {"code": 422, "message": "请求参数不合法", "data": None}


# ---- 五、管理员账号管理 -------------------------------------------------------


def test_users_routes_are_closed_to_non_admins(env):
    headers = auth(env, username="worker", password=WORKER_PW)
    response = env.client.get("/api/users", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"code": 403, "message": "需要管理员权限", "data": None}

    # 闸门挂在 router 的 dependencies 上，所以写操作同样进不来，不靠前端藏按钮。
    write = env.client.post(
        "/api/users", headers=headers, json={"username": "someone", "password": WORKER_PW}
    )
    assert write.status_code == 403


def test_admin_creates_lists_and_never_leaks_the_hash(env):
    headers = auth(env)
    created = env.client.post(
        "/api/users",
        headers=headers,
        json={
            "username": "li4",
            "password": WORKER_PW,
            "nickname": "李四",
            "email": "l@example.com",
        },
    ).json()
    assert created["code"] == 0
    assert created["data"]["status"] is True
    assert created["data"]["is_admin"] is False

    # 没给昵称就用登录名兜底，管理列表那一列不会空着。
    env.client.post(
        "/api/users", headers=headers, json={"username": "li6", "password": WORKER_PW}
    )

    listed = env.client.get("/api/users", headers=headers).json()["data"]
    assert listed["total"] == 4
    assert [item["username"] for item in listed["items"]] == ["li6", "li4", "worker", "admin"]
    assert listed["items"][1]["nickname"] == "李四"
    assert listed["items"][0]["nickname"] == "li6"
    assert all("password_hash" not in item for item in listed["items"])


def test_username_is_a_login_credential_and_stays_unique(env):
    headers = auth(env)
    env.client.post("/api/users", headers=headers, json={"username": "li4", "password": WORKER_PW})
    duplicated = env.client.post(
        "/api/users", headers=headers, json={"username": "li4", "password": WORKER_PW}
    ).json()
    assert duplicated == {"code": -1, "message": "用户名已存在", "data": None}

    # 登录名太短（schema min_length=2）在校验层就挡下，不落库。
    short = env.client.post(
        "/api/users", headers=headers, json={"username": "x", "password": WORKER_PW}
    )
    assert short.status_code == 422
    assert short.json()["message"] == "请求参数不合法"


def test_update_only_writes_back_the_fields_you_sent(env):
    headers = auth(env)
    row = create(env.db, "wang5", nickname="王五", email="w@example.com")

    body = env.client.put(
        f"/api/users/{row.id}", headers=headers, json={"nickname": "王五改名"}
    ).json()
    assert body["code"] == 0
    assert body["data"]["nickname"] == "王五改名"
    # 改昵称不该顺手把邮箱清空——exclude_unset 就是为这个。
    assert body["data"]["email"] == "w@example.com"

    missing = env.client.put("/api/users/999999", headers=headers, json={"nickname": "x"}).json()
    assert missing == {"code": -1, "message": "用户不存在", "data": None}


def test_admin_promotes_others_but_not_themselves(env):
    headers = auth(env)
    row = create(env.db, "wang5")

    promoted = env.client.put(
        f"/api/users/{row.id}", headers=headers, json={"is_admin": True}
    ).json()
    assert promoted["data"]["is_admin"] is True
    # 提权是真的提权：下一次请求 require_user 回库读到的就是新身份。
    promoted_headers = auth(env, username="wang5", password=WORKER_PW)
    assert env.client.get("/api/users", headers=promoted_headers).status_code == 200

    admin_row = row_of(env.db, "admin")
    demoted = env.client.put(
        f"/api/users/{admin_row.id}", headers=headers, json={"is_admin": False}
    ).json()
    assert demoted["code"] == -1
    assert "不能取消当前登录账号的管理员身份" in demoted["message"]
    # 把自己降级会留下一个可能没有管理员的实例。
    assert admin_row.is_admin is True


def test_admin_cannot_disable_the_account_they_are_logged_into(env):
    headers = auth(env)
    admin_row = row_of(env.db, "admin")

    self_disable = env.client.put(
        f"/api/users/{admin_row.id}/status", headers=headers, json={"status": False}
    ).json()
    assert self_disable["code"] == -1
    assert "不能禁用当前登录的账号" in self_disable["message"]

    row = create(env.db, "wang5")
    disabled = env.client.put(
        f"/api/users/{row.id}/status", headers=headers, json={"status": False}
    ).json()
    assert disabled["data"]["status"] is False

    missing = env.client.put("/api/users/999999/status", headers=headers, json={"status": False})
    assert missing.json()["message"] == "用户不存在"


def test_password_reset_replaces_the_old_one(env):
    headers = auth(env)
    row = create(env.db, "wang5")

    reset = env.client.post(
        f"/api/users/{row.id}/password", headers=headers, json={"new_password": NEW_PW}
    ).json()
    assert reset["code"] == 0
    assert login(env.client, "wang5", WORKER_PW)["code"] == 401
    assert login(env.client, "wang5", NEW_PW)["code"] == 0

    missing = env.client.post(
        "/api/users/999999/password", headers=headers, json={"new_password": NEW_PW}
    ).json()
    assert missing == {"code": -1, "message": "用户不存在", "data": None}
