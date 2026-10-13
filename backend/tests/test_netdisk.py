"""百度网盘服务层（``services/netdisk.py:NetdiskService``）的本域用例。

这个模块的价值全在「与真网盘之间那层契约」上，而它此前一条用例都没有。钉住的是
四类会静默坏掉的东西：

- **令牌的去向**。两个 token 以 Fernet 密文落库；access_token 走 query 参数，而
  httpx 的 INFO 日志会打完整 URL，所以有一个 redacting filter；删文件是 best-effort；
  带 token 的下载地址只能留在服务端做代理，绝不下发浏览器。
- **路径逃逸**。文件只允许落在 ``/apps/<APP_NAME>/`` 里，``..`` 必须被吃掉。
- **临期自动续**。30 天的 access_token 临期（<5 分钟）要自己拿 refresh_token 换新，
  否则用户看到的是「传到一半突然失败」。
- **错误契约**。OAuth 端点是 ``error``/``error_description`` 风格，xpan 业务接口是
  ``errno`` 风格，两套都得落成 :class:`BusinessError` 而不是裸异常或 httpx 异常。

出站 HTTP 全部走 ``httpx.MockTransport``，数据库是文件型 SQLite：不连百度，也不碰
共享 MySQL。异步入口统一 ``asyncio.run``（本仓库没装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.database import Base
from app.errors import (
    CODE_NETDISK_API_ERROR,
    CODE_NETDISK_NOT_BOUND,
    CODE_NETDISK_NOT_CONFIGURED,
    BusinessError,
)
from app.models.entities import NetdiskAccount
from app.services.netdisk import NetdiskService, _TokenRedactingFilter, _now

wait = asyncio.run

OWNER = 1
APP_NAME = "yangvis"


# ---- 环境 ------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'netdisk.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def settings(monkeypatch):
    """一份「网盘功能已启用」的配置。

    ``get_settings()`` 是 lru_cache 的单例，monkeypatch 在用例结束后逐项还原。
    未配置那条用例自己把三样改回空串。
    """
    conf = get_settings()
    for name, value in {
        "BAIDU_NETDISK_APP_KEY": "app-key",
        "BAIDU_NETDISK_SECRET_KEY": "app-secret",
        "BAIDU_NETDISK_APP_NAME": APP_NAME,
        "BAIDU_NETDISK_REDIRECT_URI": "https://yangvis.test/office/netdisk/callback",
    }.items():
        monkeypatch.setattr(conf, name, value)
    return conf


def svc(db, owner_id: int = OWNER) -> NetdiskService:
    return NetdiskService(db, owner_id)


def bind_account(
    db,
    *,
    owner_id: int = OWNER,
    token: str = "access-current",
    refresh: str = "refresh-current",
    expires_in: int = 3600,
) -> NetdiskAccount:
    """直接落一份已绑定的账号，绕开 OAuth 那一步。"""
    account = (
        db.scalar(select(NetdiskAccount).where(NetdiskAccount.owner_id == owner_id))
        or NetdiskAccount(owner_id=owner_id)
    )
    account.access_token_enc = encrypt(token)
    account.refresh_token_enc = encrypt(refresh)
    account.expires_at = _now() + timedelta(seconds=expires_in)
    account.baidu_uid = "42"
    account.baidu_name = "测试网盘"
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def key_of(request: httpx.Request) -> str:
    """把一次出站请求归成一个路由名，用例只关心「打到哪个接口」。"""
    path = request.url.path
    if path.endswith("/oauth/2.0/token"):
        return f"token:{request.url.params.get('grant_type')}"
    if path.endswith("/xpan/nas"):
        return "uinfo"
    if path.endswith("/xpan/file"):
        return f"file:{request.url.params.get('method') or request.url.params.get('opera')}"
    if path.endswith("/superfile2"):
        return "slice"
    if path.endswith("/xpan/multimedia"):
        return "filemetas"
    return f"unrouted:{path}"


class Pan:
    """伪造百度那一侧，同时把收到的请求原样留着给用例断言。

    同一个路由预备多份响应时按顺序取用，只剩最后一份就固定复用——分片上传那种
    「同一路由打 N 次」的用例不必为每片都写一份响应。没预备就立刻 AssertionError：
    漏配必须显形，不能悄悄走到 200 空体去撞后面的断言。
    """

    TOKEN_OK = {
        "access_token": "access-fresh",
        "refresh_token": "refresh-fresh",
        "expires_in": 7200,
    }
    UINFO_OK = {"uk": "42", "baidu_name": "测试网盘"}
    PRECREATE_OK = {"uploadid": "upload-1"}
    SLICE_OK = {"md5": "0" * 32}
    CREATE_OK = {"fs_id": 1234}

    def __init__(self, monkeypatch) -> None:
        self._responses: dict[str, list[dict]] = {}
        self._error: Exception | None = None
        self.requests: list[httpx.Request] = []
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self._error is not None:
                raise self._error
            return httpx.Response(200, json=self._next(key_of(request)))

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    def route(self, key: str, *payloads: dict) -> "Pan":
        self._responses.setdefault(key, []).extend(payloads)
        return self

    def fail_with(self, exc: Exception) -> "Pan":
        self._error = exc
        return self

    def calls(self, key: str) -> list[httpx.Request]:
        return [r for r in self.requests if key_of(r) == key]

    def _next(self, key: str) -> dict:
        queue = self._responses.get(key)
        if not queue:
            raise AssertionError(f"用例没有为 {key} 预备响应")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def form(request: httpx.Request) -> dict[str, str]:
    """application/x-www-form-urlencoded 的请求体。"""
    return {k: v for k, v in parse_qsl(request.content.decode("utf-8"))}


# ---- 一、配置与绑定状态 ------------------------------------------------------


def test_unconfigured_shuts_the_door_on_every_entry(db, monkeypatch):
    conf = get_settings()
    for name in (
        "BAIDU_NETDISK_APP_KEY",
        "BAIDU_NETDISK_SECRET_KEY",
        "BAIDU_NETDISK_APP_NAME",
    ):
        monkeypatch.setattr(conf, name, "")

    service = svc(db)
    assert service.is_bound() is False
    assert service.status_view().configured is False
    with pytest.raises(BusinessError) as auth_url:
        service.auth_url()
    with pytest.raises(BusinessError) as upload:
        wait(service.upload_file("reports/a.txt", b"hi"))
    assert auth_url.value.code == CODE_NETDISK_NOT_CONFIGURED
    assert upload.value.code == CODE_NETDISK_NOT_CONFIGURED


def test_status_view_separates_configured_from_bound(db, settings):
    service = svc(db)
    bare = service.status_view()
    assert (bare.configured, bare.bound, bare.baidu_name) == (True, False, None)

    account = bind_account(db)
    bound = service.status_view()
    assert (bound.configured, bound.bound, bound.baidu_name) == (True, True, "测试网盘")
    assert bound.expires_at == account.expires_at


def test_auth_url_is_the_oauth_page_with_the_app_credentials(db, settings):
    url = svc(db).auth_url()
    assert url.startswith("https://openapi.baidu.com/oauth/2.0/authorize?")
    query = dict(parse_qsl(url.split("?", 1)[1]))
    assert query == {
        "response_type": "code",
        "client_id": "app-key",
        "redirect_uri": "https://yangvis.test/office/netdisk/callback",
        "scope": "basic,netdisk",
        "display": "page",
    }
    # 授权页是公开的：secret 只在换 token 那一步用，绝不能出现在这里。
    assert "app-secret" not in url


def test_unbind_without_a_binding_says_so(db, settings):
    with pytest.raises(BusinessError) as caught:
        svc(db).unbind()
    assert caught.value.code == CODE_NETDISK_NOT_BOUND


def test_unbind_drops_the_local_credentials(db, settings):
    bind_account(db)
    svc(db).unbind()
    assert svc(db).get_account() is None
    # 令牌密文跟着行一起消失，不留一把还能用的钥匙。
    assert db.scalar(select(func.count()).select_from(NetdiskAccount)) == 0


# ---- 二、绑定：换令牌 + 拉用户信息 -------------------------------------------


def test_bind_stores_ciphertext_tokens_and_the_pan_name(db, settings, monkeypatch):
    pan = Pan(monkeypatch)
    pan.route("token:authorization_code", Pan.TOKEN_OK).route("uinfo", Pan.UINFO_OK)

    account = wait(svc(db).bind("  code-with-spaces  "))

    token_request = pan.calls("token:authorization_code")[0].url.params
    assert token_request["code"] == "code-with-spaces"  # 从授权页复制回来常带空白
    assert token_request["client_secret"] == "app-secret"
    assert decrypt(account.access_token_enc) == "access-fresh"
    assert decrypt(account.refresh_token_enc) == "refresh-fresh"
    assert account.baidu_uid == "42" and account.baidu_name == "测试网盘"
    # 到期时刻按 expires_in 从「现在」起算（naive UTC），不是服务商返回的字符串。
    assert timedelta(seconds=7100) < account.expires_at - _now() <= timedelta(seconds=7200)
    # uinfo 用的是刚换来的那份 token：既验证确实可用，也顺手拿到展示用的账号名。
    assert pan.calls("uinfo")[0].url.params["access_token"] == "access-fresh"


def test_rebind_updates_the_same_row_because_binding_is_one_per_owner(
    db, settings, monkeypatch
):
    pan = Pan(monkeypatch)
    pan.route("token:authorization_code", Pan.TOKEN_OK).route("uinfo", Pan.UINFO_OK)
    service = svc(db)

    wait(service.bind("code-1"))
    second = wait(service.bind("code-2"))

    assert db.scalar(select(func.count()).select_from(NetdiskAccount)) == 1
    assert decrypt(second.access_token_enc) == "access-fresh"


def test_bind_surfaces_the_oauth_error_shape(db, settings, monkeypatch):
    """OAuth 端点是 error/error_description 风格，不是 errno。"""
    Pan(monkeypatch).route(
        "token:authorization_code",
        {"error": "invalid_code", "error_description": "授权码已过期"},
    )

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).bind("stale-code"))

    assert caught.value.code == CODE_NETDISK_API_ERROR
    assert "授权码换取令牌失败" in caught.value.msg
    assert "授权码已过期" in caught.value.msg
    assert svc(db).get_account() is None


def test_bind_keeps_no_row_when_the_new_token_turns_out_unusable(db, settings, monkeypatch):
    """uinfo 报错说明换来的 token 什么都干不了：这次绑定整体作废，不留半条记录。"""
    Pan(monkeypatch).route("token:authorization_code", Pan.TOKEN_OK).route(
        "uinfo", {"errno": -6}
    )

    with pytest.raises(BusinessError):
        wait(svc(db).bind("code-1"))

    db.rollback()  # _store_tokens 已经进了 session，没 commit 就该一起消失
    assert svc(db).get_account() is None


# ---- 三、临期自动续 ----------------------------------------------------------


def test_a_nearly_expired_token_is_renewed_before_the_first_call(db, settings, monkeypatch):
    account = bind_account(db, expires_in=60)  # 剩 1 分钟，低于 5 分钟的提前量
    pan = Pan(monkeypatch)
    pan.route("token:refresh_token", Pan.TOKEN_OK).route(
        "filemetas", {"list": [{"dlink": "https://d.pcs.baidu.com/f/x?sign=1"}]}
    )

    url, _ = wait(svc(db).open_download(1234))

    assert pan.calls("token:refresh_token")[0].url.params["refresh_token"] == "refresh-current"
    assert pan.calls("filemetas")[0].url.params["access_token"] == "access-fresh"
    assert "access_token=access-fresh" in url  # 用的是新那份，不是临期那份
    db.refresh(account)
    assert decrypt(account.access_token_enc) == "access-fresh"


def test_a_fresh_token_is_left_alone(db, settings, monkeypatch):
    bind_account(db, expires_in=3600)
    pan = Pan(monkeypatch)
    pan.route("filemetas", {"list": [{"dlink": "https://d.pcs.baidu.com/f/x?sign=1"}]})

    wait(svc(db).open_download(1234))

    assert pan.calls("token:refresh_token") == []


def test_a_missing_expiry_is_not_treated_as_expired(db, settings, monkeypatch):
    """expires_at 为空是历史行的形态：不能因为「不知道几点过期」就每调一次刷一次。"""
    account = bind_account(db)
    account.expires_at = None
    db.commit()
    pan = Pan(monkeypatch)
    pan.route("filemetas", {"list": [{"dlink": "https://d.pcs.baidu.com/f/x?sign=1"}]})

    wait(svc(db).open_download(1234))

    assert pan.calls("token:refresh_token") == []


def test_a_failed_refresh_tells_the_user_to_rebind(db, settings, monkeypatch):
    bind_account(db, expires_in=60)
    Pan(monkeypatch).route(
        "token:refresh_token",
        {"error": "not_active", "error_description": "refresh token 已失效"},
    )

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).open_download(1234))

    assert "网盘授权刷新失败，请重新绑定" in caught.value.msg


def test_file_calls_need_a_binding(db, settings, monkeypatch):
    Pan(monkeypatch)
    with pytest.raises(BusinessError) as upload:
        wait(svc(db).upload_file("reports/a.txt", b"hi"))
    with pytest.raises(BusinessError) as download:
        wait(svc(db).open_download(1))
    assert upload.value.code == CODE_NETDISK_NOT_BOUND
    assert download.value.code == CODE_NETDISK_NOT_BOUND
    assert "请先在页面上完成绑定" in upload.value.msg


# ---- 四、路径：只能落在应用目录里 --------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("a.txt", f"/apps/{APP_NAME}/a.txt"),
        ("resumes/12_zhangsan.pdf", f"/apps/{APP_NAME}/resumes/12_zhangsan.pdf"),
        # 越界的段一律换成 `_`：结果永远还在 /apps/<APP_NAME>/ 里。每个 `..` 各自
        # 占一段，不做「抵消」——抵消等于承认相对路径能往上走。
        ("../../etc/passwd", f"/apps/{APP_NAME}/_/_/etc/passwd"),
        ("a/../../b.txt", f"/apps/{APP_NAME}/a/_/_/b.txt"),
        ("a/../b.txt", f"/apps/{APP_NAME}/a/_/b.txt"),
        ("/leading/slash/", f"/apps/{APP_NAME}/leading/slash"),
        ("back\\slash.txt", f"/apps/{APP_NAME}/back_slash.txt"),
        ("带\x00控制字符.txt", f"/apps/{APP_NAME}/带_控制字符.txt"),
    ],
)
def test_remote_path_never_escapes_the_app_directory(db, settings, rel_path, expected):
    assert svc(db)._remote_path(rel_path) == expected


# 空段（含纯空白）在清洗之前就被跳过，全跳完就没有段了——这时报错而不是默默落到
# 应用目录根上。
@pytest.mark.parametrize("rel_path", ["", "/", "///", "   ", "  /  "])
def test_remote_path_rejects_a_blank_target(db, settings, rel_path):
    with pytest.raises(BusinessError) as caught:
        svc(db)._remote_path(rel_path)
    assert caught.value.code == CODE_NETDISK_API_ERROR
    assert "目标路径为空" in caught.value.msg


# ---- 五、三步上传 ------------------------------------------------------------


def test_upload_runs_precreate_then_slice_then_create(db, settings, monkeypatch):
    bind_account(db)
    pan = Pan(monkeypatch)
    pan.route("file:precreate", Pan.PRECREATE_OK).route("slice", Pan.SLICE_OK).route(
        "file:create", Pan.CREATE_OK
    )

    fs_id, path = wait(svc(db).upload_file("reports/周报.txt", "内容".encode()))

    assert (fs_id, path) == (1234, f"/apps/{APP_NAME}/reports/周报.txt")
    assert [key_of(r) for r in pan.requests] == ["file:precreate", "slice", "file:create"]
    pre = form(pan.calls("file:precreate")[0])
    assert pre["path"] == path and pre["size"] == "6" and pre["rtype"] == "3"
    assert json.loads(pre["block_list"]) == [hashlib.md5("内容".encode()).hexdigest()]
    created = form(pan.calls("file:create")[0])
    assert created["uploadid"] == "upload-1" and created["path"] == path
    assert pan.calls("slice")[0].url.params["partseq"] == "0"


def test_upload_slices_a_large_file_at_four_mb_and_hashes_each_piece(db, settings, monkeypatch):
    bind_account(db)
    pan = Pan(monkeypatch)
    pan.route("file:precreate", Pan.PRECREATE_OK).route("slice", Pan.SLICE_OK).route(
        "file:create", Pan.CREATE_OK
    )
    raw = b"a" * (4 * 1024 * 1024) + b"b" * 5

    wait(svc(db).upload_file("big.bin", raw))

    slices = pan.calls("slice")
    assert [r.url.params["partseq"] for r in slices] == ["0", "1"]
    expected = [
        hashlib.md5(raw[: 4 * 1024 * 1024]).hexdigest(),
        hashlib.md5(b"b" * 5).hexdigest(),
    ]
    assert json.loads(form(pan.calls("file:precreate")[0])["block_list"]) == expected
    assert form(pan.calls("file:create")[0])["size"] == str(len(raw))
    assert b"big.bin" in slices[0].content  # 每片是 multipart，文件名取自路径末段


def test_an_empty_upload_still_sends_one_slice(db, settings, monkeypatch):
    """0 字节也得走完三步：slices 为空列表时兜一片空片，否则 create 拿不到块。"""
    bind_account(db)
    pan = Pan(monkeypatch)
    pan.route("file:precreate", Pan.PRECREATE_OK).route("slice", Pan.SLICE_OK).route(
        "file:create", Pan.CREATE_OK
    )

    wait(svc(db).upload_file("empty.txt", b""))

    assert len(pan.calls("slice")) == 1
    assert json.loads(form(pan.calls("file:precreate")[0])["block_list"]) == [
        hashlib.md5(b"").hexdigest()
    ]


def test_upload_stops_at_the_first_rejected_errno_without_uploading(db, settings, monkeypatch):
    bind_account(db)
    pan = Pan(monkeypatch)
    pan.route("file:precreate", {"errno": 31034})

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).upload_file("a.txt", b"hi"))

    assert "访问过于频繁" in caught.value.msg
    assert pan.calls("slice") == []


def test_upload_fails_loudly_when_precreate_carries_no_uploadid(db, settings, monkeypatch):
    bind_account(db)
    Pan(monkeypatch).route("file:precreate", {"errno": 0})

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).upload_file("a.txt", b"hi"))

    assert "响应里没有 uploadid" in caught.value.msg


def test_upload_fails_loudly_when_create_carries_no_fs_id(db, settings, monkeypatch):
    bind_account(db)
    (
        Pan(monkeypatch)
        .route("file:precreate", Pan.PRECREATE_OK)
        .route("slice", Pan.SLICE_OK)
        .route("file:create", {"errno": 0})
    )

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).upload_file("a.txt", b"hi"))

    assert "响应里没有 fs_id" in caught.value.msg


# ---- 六、下载：带 token 的 URL 只进服务端 ------------------------------------


def test_download_appends_the_token_and_pins_the_pan_user_agent(db, settings, monkeypatch):
    """dlink 必须带 ``User-Agent: pan.baidu.com``，缺了就 403——没有报错可看，
    只会「下载下来是个 HTML」，所以钉在返回的 header 里。"""
    bind_account(db, token="access-current")
    pan = Pan(monkeypatch)
    pan.route("filemetas", {"list": [{"dlink": "https://d.pcs.baidu.com/f/x?sign=abc"}]})

    url, headers = wait(svc(db).open_download(1234))

    assert url == "https://d.pcs.baidu.com/f/x?sign=abc&access_token=access-current"
    assert headers == {"User-Agent": "pan.baidu.com"}
    params = pan.calls("filemetas")[0].url.params
    assert params["fsids"] == "[1234]" and params["dlink"] == "1"


def test_download_uses_a_question_mark_when_the_link_has_no_query(db, settings, monkeypatch):
    bind_account(db, token="access-current")
    Pan(monkeypatch).route("filemetas", {"list": [{"dlink": "https://d.pcs.baidu.com/f/x"}]})

    url, _ = wait(svc(db).open_download(7))

    assert url == "https://d.pcs.baidu.com/f/x?access_token=access-current"


@pytest.mark.parametrize("payload", [{"list": []}, {"list": [{}]}])
def test_download_without_a_link_says_the_file_may_be_gone(db, settings, monkeypatch, payload):
    bind_account(db)
    Pan(monkeypatch).route("filemetas", payload)

    with pytest.raises(BusinessError) as caught:
        wait(svc(db).open_download(1234))

    assert caught.value.code == CODE_NETDISK_API_ERROR
    assert "文件可能已被删除" in caught.value.msg


# ---- 七、错误契约与兜底 ------------------------------------------------------


@pytest.mark.parametrize(
    ("errno", "phrase"),
    [
        (-6, "重新绑定"),
        (42001, "重新绑定"),
        (-7, "不存在或已被移动"),
        (2, "参数错误"),
        (31034, "过于频繁"),
    ],
)
def test_common_errno_codes_read_in_chinese(errno, phrase):
    with pytest.raises(BusinessError) as caught:
        NetdiskService._check_errno({"errno": errno}, "删除网盘文件失败")
    assert caught.value.code == CODE_NETDISK_API_ERROR
    assert phrase in caught.value.msg
    assert str(errno) not in caught.value.msg  # 已知码不该把数字甩给用户


def test_an_unknown_errno_keeps_the_number_and_the_detail():
    with pytest.raises(BusinessError) as caught:
        NetdiskService._check_errno({"errno": 8607, "errmsg": "user is rejected"}, "前缀")
    assert "errno=8607" in caught.value.msg
    assert "user is rejected" in caught.value.msg


@pytest.mark.parametrize("payload", [{}, {"errno": None}, {"errno": 0}])
def test_a_zero_or_absent_errno_means_success(payload):
    NetdiskService._check_errno(payload, "前缀")  # 不抛即为通过


def test_an_oauth_response_without_an_error_key_is_success():
    NetdiskService._check_oauth_error({"access_token": "x"}, "授权码换取令牌失败")


def test_unparseable_and_unexpected_responses_become_readable():
    with pytest.raises(BusinessError) as unparseable:
        NetdiskService._parse(httpx.Response(500, text="<html>error</html>"))
    assert "无法解析的响应（HTTP 500）" in unparseable.value.msg

    with pytest.raises(BusinessError) as unexpected:
        NetdiskService._parse(httpx.Response(200, json=[1, 2]))
    assert "意料之外的响应（HTTP 200）" in unexpected.value.msg


def test_a_transport_failure_is_logged_without_the_token(db, settings, monkeypatch, caplog):
    bind_account(db, token="access-current")
    Pan(monkeypatch).fail_with(
        httpx.ConnectError("connect failed on ?access_token=access-current")
    )

    with caplog.at_level(logging.WARNING, logger="yangvis.netdisk"), pytest.raises(
        BusinessError
    ) as caught:
        wait(svc(db).open_download(1234))

    assert caught.value.code == CODE_NETDISK_API_ERROR
    # 日志那一行只记异常类型；完整 URL（query 里就是 token）不进日志。
    assert "ConnectError" in caplog.text
    assert "access_token=access-current" not in caplog.text


@pytest.mark.parametrize("errno", [0, -7])
def test_delete_is_best_effort_and_never_blocks_the_caller(
    db, settings, monkeypatch, errno, caplog
):
    """删网盘文件失败不能把本地删除一起拖掉：用户要的是「记录没了」。"""
    bind_account(db)
    Pan(monkeypatch).route("file:filemanager", {"errno": errno})

    wait(svc(db).delete_file(f"/apps/{APP_NAME}/reports/a.txt"))

    if errno:
        assert "netdisk delete" in caplog.text and "failed" in caplog.text


def test_delete_swallows_a_missing_binding(db, settings, monkeypatch, caplog):
    Pan(monkeypatch)
    wait(svc(db).delete_file(f"/apps/{APP_NAME}/a.txt"))  # 未绑定：只记日志
    assert "failed" in caplog.text


def test_the_httpx_log_filter_redacts_access_token():
    """xpan 的设计要求 token 走 query，而 httpx 的 INFO 日志会打完整 URL。"""
    flt = _TokenRedactingFilter()
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "test",
        1,
        "HTTP Request: %s",
        ("https://pan.baidu.com/rest?access_token=leaky&dlink=1",),
        None,
    )

    assert flt.filter(record) is True
    assert record.getMessage() == (
        "HTTP Request: https://pan.baidu.com/rest?access_token=***&dlink=1"
    )

    quiet = logging.LogRecord("httpx", logging.INFO, "test", 1, "no token here", None, None)
    assert flt.filter(quiet) is True
    assert quiet.msg == "no token here"
