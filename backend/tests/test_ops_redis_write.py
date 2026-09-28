"""Redis 结构化写（值 / 元素 / TTL / 批量删除）的服务层测试。

共享实例上不该为测试改数据，所以把连接那一层换成会记调用的假客户端：这里验的
是「界面坐标 → 发的是哪条命令、值怎么进去的、审计文本长什么样」。
"""
import asyncio
from types import SimpleNamespace

import pytest
from redis.exceptions import RedisError

from app.models.schemas import (
    RedisElementAddRequest,
    RedisElementDeleteRequest,
    RedisKeyCreateRequest,
    RedisKeyDeleteRequest,
    RedisStringUpdateRequest,
    RedisTtlRequest,
)
from app.services import ops_database as db_ops

ITEM = SimpleNamespace(id=1, db_type="redis", writable=True)


class FakeRedis:
    """记调用、按命令名回预设回复的假客户端。

    服务层只用 ``await client.命令(*args, **kwargs)`` 这一种形态，``__getattr__``
    兜住全部命令，连 ``aclose`` 也一样记下来。
    """

    def __init__(self, **replies) -> None:
        self.calls: list = []
        self.closed = 0
        self.replies = {"exists": 0, "type": "string", "pttl": -1}
        self.replies.update(replies)

    async def aclose(self) -> None:
        # 单独计数：每个写动作都该收掉自己的连接，但它不是业务命令。
        self.closed += 1

    @property
    def names(self) -> list:
        return [name for name, _, _ in self.calls]

    def sent(self, name: str):
        """某条命令实际收到的位置参数（只有一条时用它，省去翻 calls）。"""
        picked = [args for cmd, args, _ in self.calls if cmd == name]
        assert len(picked) == 1, f"{name} 被调用 {len(picked)} 次"
        return picked[0]

    def __getattr__(self, name):
        async def run(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            reply = self.replies.get(name)
            if callable(reply):
                return reply(*args, **kwargs)
            return True if reply is None else reply

        return run


@pytest.fixture
def fake(monkeypatch):
    def install(**replies):
        client = FakeRedis(**replies)
        monkeypatch.setattr(db_ops, "_password_of", lambda item: "pw")
        monkeypatch.setattr(db_ops, "_connect_redis", lambda *a, **k: client)
        return client

    return install


def call(func, *args):
    return asyncio.run(func(*args))


# ---- 新建 key -------------------------------------------------------------------


def test_create_string_sends_set_with_value_as_argument(fake):
    client = fake()
    result = call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="user:1", key_type="string", value="ok"),
    )
    assert client.names == ["exists", "set"]
    assert client.sent("set") == ("user:1", "ok")
    assert result.command == "SET user:1 ok"


def test_create_string_with_ttl_expires_on_the_same_connection(fake):
    client = fake()
    result = call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="cache:a", key_type="string", value="v", ttl=60),
    )
    assert client.names == ["exists", "set", "expire"]
    assert client.sent("expire") == ("cache:a", 60)
    assert result.command == "SET cache:a v; EXPIRE cache:a 60"


def test_create_refuses_an_existing_key(fake):
    client = fake(exists=1)
    with pytest.raises(db_ops.OpsDbError) as exc:
        call(db_ops.create_key, ITEM, RedisKeyCreateRequest(key="k", key_type="string", value="v"))
    assert "已存在" in str(exc.value)
    # 拒绝发生在写之前：一个 SET 都不该发出去。
    assert client.names == ["exists"]


def test_create_list_set_zset_hash_use_their_own_commands(fake):
    client = fake()
    call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="l", key_type="list", value=["a", "b"]),
    )
    assert client.sent("rpush") == ("l", "a", "b")
    call(db_ops.create_key, ITEM, RedisKeyCreateRequest(key="s", key_type="set", value=["m"]))
    assert client.sent("sadd") == ("s", "m")
    call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="z", key_type="zset", value=[["m1", "1"], ["m2", "2.5"]]),
    )
    assert client.sent("zadd") == ("z", {"m1": 1.0, "m2": 2.5})
    call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="h", key_type="hash", value=[["f", "v"]]),
    )
    assert client.calls[-1][2] == {"mapping": {"f": "v"}}


def test_create_rejects_bad_collection_payloads(fake):
    fake()
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.create_key, ITEM, RedisKeyCreateRequest(key="l", key_type="list", value=[]))
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.create_key, ITEM, RedisKeyCreateRequest(key="l", key_type="list", value="不是数组"))
    with pytest.raises(db_ops.OpsDbError):
        call(
            db_ops.create_key,
            ITEM,
            RedisKeyCreateRequest(key="z", key_type="zset", value=[["m", "abc"]]),
        )
    with pytest.raises(db_ops.OpsDbError):
        call(
            db_ops.create_key,
            ITEM,
            RedisKeyCreateRequest(key="h", key_type="hash", value=[["只有一项"]]),
        )


def test_create_command_text_caps_the_element_list(fake):
    client = fake()
    result = call(
        db_ops.create_key,
        ITEM,
        RedisKeyCreateRequest(key="l", key_type="list", value=[f"v{i}" for i in range(15)]),
    )
    assert client.sent("rpush")[1:] == tuple(f"v{i}" for i in range(15))
    assert result.command.endswith("…（另有 5 个）")


# ---- 改 string 的值 --------------------------------------------------------------


def test_string_update_restores_the_previous_ttl(fake):
    client = fake(pttl=90_000)
    result = call(
        db_ops.set_string, ITEM, RedisStringUpdateRequest(key="cache:a", value="new")
    )
    assert client.names == ["type", "pttl", "set", "pexpire"]
    assert client.sent("pexpire") == ("cache:a", 90_000)
    assert result.command == "SET cache:a new"


def test_string_update_without_ttl_leaves_expiry_alone(fake):
    client = fake(pttl=-1)
    call(db_ops.set_string, ITEM, RedisStringUpdateRequest(key="k", value="v"))
    assert "pexpire" not in client.names


def test_string_update_refuses_a_non_string_key(fake):
    client = fake(type="hash")
    with pytest.raises(db_ops.OpsDbError) as exc:
        call(db_ops.set_string, ITEM, RedisStringUpdateRequest(key="h", value="v"))
    assert "hash" in str(exc.value)
    assert "set" not in client.names


# ---- 元素增删 -------------------------------------------------------------------


def test_add_element_maps_each_type_to_its_command(fake):
    client = fake()
    call(
        db_ops.add_element,
        ITEM,
        RedisElementAddRequest(key="h", key_type="hash", field="f", value="v"),
    )
    assert client.sent("hset") == ("h", "f", "v")
    call(db_ops.add_element, ITEM, RedisElementAddRequest(key="l", key_type="list", value="v"))
    assert client.sent("rpush") == ("l", "v")
    call(
        db_ops.add_element,
        ITEM,
        RedisElementAddRequest(key="l", key_type="list", value="v", position="head"),
    )
    assert client.sent("lpush") == ("l", "v")
    call(db_ops.add_element, ITEM, RedisElementAddRequest(key="s", key_type="set", value="m"))
    assert client.sent("sadd") == ("s", "m")
    call(
        db_ops.add_element,
        ITEM,
        RedisElementAddRequest(key="z", key_type="zset", value="m", score=1.5),
    )
    assert client.sent("zadd") == ("z", {"m": 1.5})


def test_add_element_requires_the_per_type_arguments(fake):
    client = fake()
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.add_element, ITEM, RedisElementAddRequest(key="h", key_type="hash", value="v"))
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.add_element, ITEM, RedisElementAddRequest(key="z", key_type="zset", value="m"))
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.add_element, ITEM, RedisElementAddRequest(key="s", key_type="set"))
    assert client.names == []


def test_remove_element_maps_each_type_to_its_command(fake):
    client = fake()
    call(
        db_ops.remove_element,
        ITEM,
        RedisElementDeleteRequest(key="h", key_type="hash", target="f"),
    )
    assert client.sent("hdel") == ("h", "f")
    call(
        db_ops.remove_element,
        ITEM,
        RedisElementDeleteRequest(key="s", key_type="set", target="m"),
    )
    assert client.sent("srem") == ("s", "m")
    call(
        db_ops.remove_element,
        ITEM,
        RedisElementDeleteRequest(key="z", key_type="zset", target="m"),
    )
    assert client.sent("zrem") == ("z", "m")
    call(
        db_ops.remove_element,
        ITEM,
        RedisElementDeleteRequest(key="x", key_type="stream", target="1-0"),
    )
    assert client.sent("xdel") == ("x", "1-0")


def test_list_element_removal_is_positional_not_by_value(fake):
    """LREM 按值删会把同值的元素一起带走，所以走 LSET 哨兵 + LREM 一条。"""
    client = fake()
    result = call(
        db_ops.remove_element,
        ITEM,
        RedisElementDeleteRequest(key="l", key_type="list", target="2"),
    )
    lset = client.sent("lset")
    lrem = client.sent("lrem")
    assert lset[0] == "l" and lset[1] == 2
    # 哨兵是随机的：值本身不可能出现在列表里，LREM 只会删掉刚放上去的那一个。
    assert lrem[0] == "l" and lrem[1] == 1
    assert lset[2] == lrem[2]
    assert result.command == "LREM l 1 <第 2 个元素>"


def test_list_element_removal_rejects_a_bad_index(fake):
    client = fake()
    for target in ("abc", "-1"):
        with pytest.raises(db_ops.OpsDbError):
            call(
                db_ops.remove_element,
                ITEM,
                RedisElementDeleteRequest(key="l", key_type="list", target=target),
            )
    assert client.names == []


def test_string_key_has_no_elements_to_remove(fake):
    fake()
    with pytest.raises(db_ops.OpsDbError):
        call(
            db_ops.remove_element,
            ITEM,
            RedisElementDeleteRequest(key="k", key_type="string", target="x"),
        )


# ---- TTL 与批量删除 ---------------------------------------------------------------


def test_expire_reports_a_missing_key_instead_of_zero(fake):
    client = fake(expire=False)
    with pytest.raises(db_ops.OpsDbError) as exc:
        call(
            db_ops.set_ttl,
            ITEM,
            RedisTtlRequest(key="gone", action="expire", seconds=60),
        )
    assert "不存在" in str(exc.value)
    assert client.sent("expire") == ("gone", 60)


def test_expire_and_persist_command_text(fake):
    client = fake()
    result = call(db_ops.set_ttl, ITEM, RedisTtlRequest(key="k", action="expire", seconds=60))
    assert result.command == "EXPIRE k 60"
    assert result.deleted == 1
    result = call(db_ops.set_ttl, ITEM, RedisTtlRequest(key="k", action="persist"))
    assert result.command == "PERSIST k"
    assert client.names == ["expire", "persist"]


def test_expire_without_seconds_is_rejected(fake):
    fake()
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.set_ttl, ITEM, RedisTtlRequest(key="k", action="expire"))


def test_batch_delete_uses_unlink_and_reports_the_count(fake):
    client = fake(unlink=2)
    result = call(db_ops.delete_keys, ITEM, RedisKeyDeleteRequest(keys=["a", "b", "c"]))
    assert client.sent("unlink") == ("a", "b", "c")
    assert result.deleted == 2
    assert result.command == "UNLINK a b c"


def test_batch_delete_caps_the_audit_text(fake):
    fake()
    keys = [f"k{i}" for i in range(30)]
    result = call(db_ops.delete_keys, ITEM, RedisKeyDeleteRequest(keys=keys))
    assert result.command.endswith("…（另有 19 个）")
    assert result.command.startswith("UNLINK k0 k1")


def test_batch_delete_needs_at_least_one_key(fake):
    client = fake()
    with pytest.raises(db_ops.OpsDbError):
        call(db_ops.delete_keys, ITEM, RedisKeyDeleteRequest(keys=[""]))
    assert client.names == []


# ---- 命令文本与失败收尾 ------------------------------------------------------------


def test_display_text_keeps_one_line_and_caps_long_values(fake):
    fake()
    long_value = "x" * (db_ops._REDIS_ARG_LEN + 10)
    result = call(db_ops.set_string, ITEM, RedisStringUpdateRequest(key="k", value=long_value))
    assert f"…（共 {len(long_value)} 字符）" in result.command

    multiline = call(
        db_ops.add_element,
        ITEM,
        RedisElementAddRequest(key="l", key_type="list", value="a\nb"),
    )
    assert "\\n" in multiline.command
    assert "\n" not in multiline.command


def test_redis_error_becomes_ops_error_and_still_closes(fake):
    client = fake(set=lambda *a, **k: (_ for _ in ()).throw(RedisError("boom")))
    with pytest.raises(db_ops.OpsDbError) as exc:
        call(db_ops.set_string, ITEM, RedisStringUpdateRequest(key="k", value="v"))
    assert "boom" in str(exc.value)
    assert client.closed == 1
