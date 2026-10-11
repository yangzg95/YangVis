"""Redis 台账连接的键浏览与结构化读写。

浏览：逻辑库来自 ``INFO keyspace``（只列真有 key 的，外加台账配的默认库），
key 列表走 SCAN 游标 + pipeline ``TYPE``，一页数量吃 ``MAX_PAGE_SIZE``；取值按
类型分别限流——集合类最多带回 ``_COLLECTION_LIMIT`` 个元素，string 截到
``OPS_OUTPUT_LIMIT``，多出来的只以 ``truncated`` 标记告知。

写：界面上交的是坐标（key / field / member / 下标），命令由 redis-py 的类型化
方法拼出，值作为独立参数进协议层、从不参与命令文本构造，所以这一路不需要
``classify_redis`` 那类关键字网关；闸门是路由层的「连接 writable × 用户
ops_write」，剩下的类型不符、下标越界交给 Redis 自己报错。命令文本只用于审计
展示，产出的文本从不回放执行。
"""
from __future__ import annotations

import shlex
import time
import uuid
from typing import (
    Any,
    Awaitable,
    Callable,
    List,
    Sequence,
    Tuple,
)

from app.config import get_settings
from app.models.entities import OpsDatabase
from app.models.schemas import (
    DbSchemaItem,
    RedisElementAddRequest,
    RedisElementDeleteRequest,
    RedisKeyCreateRequest,
    RedisKeyDeleteRequest,
    RedisKeyDetail,
    RedisKeyItem,
    RedisScanResult,
    RedisStringUpdateRequest,
    RedisTtlRequest,
    RedisWriteResult,
)
from app.services.ops_database import OpsDbError
from app.services.ops_db_exec import (
    MAX_PAGE_SIZE,
    _connect_redis,
    _default_redis_db,
    _password_of,
)

# Redis 集合类值最多带回多少个元素。
_COLLECTION_LIMIT = 100


async def _list_redis_dbs(item: OpsDatabase) -> List[DbSchemaItem]:
    """Redis 的「schema」是有 key 的逻辑库，外加台账里配置的默认库。"""
    from redis.exceptions import RedisError

    password = _password_of(item)
    client = _connect_redis(item, password)
    try:
        info = await client.info("keyspace")
    except RedisError as exc:
        raise OpsDbError(f"读取库列表失败：{exc}") from exc
    finally:
        await client.aclose()

    found = {
        int(name[2:]): int(stats.get("keys", 0))
        for name, stats in info.items()
        if name.startswith("db") and name[2:].isdigit()
    }
    found.setdefault(_default_redis_db(item), 0)
    return [
        DbSchemaItem(name=f"db{n}", kind="redisdb", object_count=found[n])
        for n in sorted(found)
    ]


async def scan_keys(
    item: OpsDatabase, db: int, pattern: str, cursor: str, count: int
) -> RedisScanResult:
    """一页 key。SCAN 是游标式的，``cursor`` 为 "0" 表示没有更多了。"""
    from redis.exceptions import RedisError

    count = min(max(1, int(count)), MAX_PAGE_SIZE)
    password = _password_of(item)
    client = _connect_redis(item, password, db)
    try:
        next_cursor, keys = await client.scan(
            cursor=int(cursor or 0), match=pattern or None, count=count
        )
        types: List[Any] = []
        if keys:
            pipe = client.pipeline(transaction=False)
            for key in keys:
                pipe.type(key)
            types = await pipe.execute()
    except (RedisError, ValueError) as exc:
        raise OpsDbError(f"读取 key 列表失败：{exc}") from exc
    finally:
        await client.aclose()

    return RedisScanResult(
        cursor=str(next_cursor),
        keys=[
            RedisKeyItem(key=str(k), key_type=str(t))
            for k, t in zip(keys, types or ["unknown"] * len(keys))
        ],
    )


async def key_detail(item: OpsDatabase, db: int, key: str) -> RedisKeyDetail:
    """一个 key 的只读详情。集合类只带回前 ``_COLLECTION_LIMIT`` 个元素。"""
    from redis.exceptions import RedisError

    password = _password_of(item)
    client = _connect_redis(item, password, db)
    try:
        key_type = str(await client.type(key))
        if key_type == "none":
            return RedisKeyDetail(key=key, key_type="none", ttl=-2)
        ttl = int(await client.ttl(key))
        value, truncated = await _read_redis_value(client, key, key_type)
    except RedisError as exc:
        raise OpsDbError(f"读取 key 失败：{exc}") from exc
    finally:
        await client.aclose()

    return RedisKeyDetail(key=key, key_type=key_type, ttl=ttl, value=value, truncated=truncated)


async def _read_redis_value(client: Any, key: str, key_type: str) -> Tuple[Any, bool]:
    limit = _COLLECTION_LIMIT

    if key_type == "string":
        value = await client.get(key)
        text = "" if value is None else str(value)
        cap = get_settings().OPS_OUTPUT_LIMIT
        return (text[:cap], True) if len(text) > cap else (text, False)

    if key_type == "list":
        length = int(await client.llen(key))
        values = await client.lrange(key, 0, limit - 1)
        return [str(v) for v in values], length > limit

    if key_type == "set":
        cardinality = int(await client.scard(key))
        if cardinality <= limit:
            members = await client.smembers(key)
            return sorted(str(m) for m in members), False
        members = await client.srandmember(key, limit)
        if isinstance(members, str):
            members = [members]
        return sorted(str(m) for m in members), True

    if key_type == "zset":
        cardinality = int(await client.zcard(key))
        pairs = await client.zrange(key, 0, limit - 1, withscores=True)
        return [[str(m), str(s)] for m, s in pairs], cardinality > limit

    if key_type == "hash":
        length = int(await client.hlen(key))
        if length <= limit:
            data = await client.hgetall(key)
            return [[str(f), str(v)] for f, v in data.items()], False
        pairs = await client.hscan(key, count=limit)
        fields = list(pairs[1].items())[:limit]
        return [[str(f), str(v)] for f, v in fields], True

    if key_type == "stream":
        length = int(await client.xlen(key))
        entries = await client.xrange(key, count=limit)
        return [[str(entry_id), str(fields)] for entry_id, fields in entries], length > limit

    # module 类型（JSON / Bloom 等）没有通用的只读取值方式，如实告诉用户。
    return None, False


# ---- Redis 结构化写（值 / 元素 / TTL / 批量删除） ---------------------------------
#
# 和 MySQL 的行内改值同一个思路：界面上交上来的是坐标（key、field、member、
# 下标），命令由服务端用 redis-py 的类型化方法拼出来，值作为独立参数进协议层，
# 从不参与命令文本的构造——所以这一路不需要 classify_redis 的关键字网关。
# 闸门是路由层的「连接 writable × 用户 ops_write」，加上 Redis 自己会报的
# WRONGTYPE、下标越界。


# 审计文本里单个参数的长度上限：值可以是几百 KB，历史列表要看得清是哪条。
_REDIS_ARG_LEN = 2000

# 多参数命令（RPUSH / UNLINK 等）在文本里列出的参数个数，其余用总数代替。
_REDIS_ARG_DISPLAY = 10


def _redis_arg(value: str) -> str:
    """命令文本里的一个参数。只做展示（审计、提示），产出的文本从不回放执行。"""
    text = value
    if len(text) > _REDIS_ARG_LEN:
        text = text[:_REDIS_ARG_LEN] + f"…（共 {len(value)} 字符）"
    # 换行压成可见字符：审计列表是一行一条记录。
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    return shlex.quote(text)


def _redis_command(name: str, key: str, args: Sequence[str] = ()) -> str:
    """``NAME key arg…`` 形态的命令文本，超长参数列表按个数截。"""
    parts = [name, _redis_arg(key)]
    shown = [_redis_arg(a) for a in args[:_REDIS_ARG_DISPLAY]]
    rest = len(args) - len(shown)
    if rest > 0:
        shown.append(f"…（另有 {rest} 个）")
    return " ".join(parts + shown)


async def _redis_write(
    item: OpsDatabase, db: int, action: Callable[[Any], Awaitable[Any]]
) -> Tuple[Any, int]:
    """开一条连接跑一个写动作，返回 (回复, 耗时 ms)。

    只把 RedisError 转成 OpsDbError：动作里主动抛的语义错误（类型不对、下标
    越界）原样往上走，别在里面套两层「失败」。
    """
    from redis.exceptions import RedisError

    password = _password_of(item)
    started = time.perf_counter()
    client = _connect_redis(item, password, db)
    try:
        raw = await action(client)
    except RedisError as exc:
        raise OpsDbError(f"写入失败：{exc}") from exc
    finally:
        await client.aclose()
    return raw, int((time.perf_counter() - started) * 1000)


def _as_members(value: Any, label: str) -> List[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise OpsDbError(f"{label}需要是一个非空数组")
    return [str(v) for v in value]


def _as_pairs(value: Any, label: str) -> List[Tuple[str, str]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise OpsDbError(f"{label}需要是非空的 [值, 值] 数组")
    pairs: List[Tuple[str, str]] = []
    for raw in value:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise OpsDbError(f"{label}的每一项都必须是两元素数组")
        pairs.append((str(raw[0]), str(raw[1])))
    return pairs


async def create_key(item: OpsDatabase, req: RedisKeyCreateRequest) -> RedisWriteResult:
    """新建一个 key。已存在直接拒绝——「新建」不该悄悄覆盖别人正在用的数据。"""
    key, key_type = req.key, req.key_type

    if key_type == "string":
        text = "" if req.value is None else str(req.value)
        display = _redis_command("SET", key, [text])

        async def action(client: Any) -> Any:
            return await client.set(key, text)

    elif key_type == "list":
        members = _as_members(req.value, "列表元素")
        display = _redis_command("RPUSH", key, members)

        async def action(client: Any) -> Any:
            return await client.rpush(key, *members)

    elif key_type == "set":
        members = _as_members(req.value, "集合成员")
        display = _redis_command("SADD", key, members)

        async def action(client: Any) -> Any:
            return await client.sadd(key, *members)

    elif key_type == "zset":
        pairs = _as_pairs(req.value, "有序集合元素")
        scored: List[Tuple[str, float]] = []
        for member, raw_score in pairs:
            try:
                scored.append((member, float(raw_score)))
            except ValueError:
                raise OpsDbError("有序集合的 score 需要是数字") from None
        display = _redis_command("ZADD", key, [f"{m} {s}" for m, s in scored])

        async def action(client: Any) -> Any:
            return await client.zadd(key, {m: s for m, s in scored})

    else:  # hash
        pairs = _as_pairs(req.value, "哈希字段")
        display = _redis_command("HSET", key, [f"{f} {v}" for f, v in pairs])

        async def action(client: Any) -> Any:
            return await client.hset(key, mapping=dict(pairs))

    async def run(client: Any) -> Any:
        # 存在性检查与写入在同一条连接里连着做：界面上的「新建」不该覆盖已有数据。
        if await client.exists(key):
            raise OpsDbError("key 已存在，请直接编辑它的值")
        await action(client)
        # TTL 也在这一条连接上补完，新建和设过期中间不断线。
        if req.ttl:
            await client.expire(key, req.ttl)

    _, elapsed = await _redis_write(item, req.db, run)
    if req.ttl:
        display = f"{display}; EXPIRE {_redis_arg(key)} {req.ttl}"
    return RedisWriteResult(command=display, elapsed_ms=elapsed)


async def set_string(item: OpsDatabase, req: RedisStringUpdateRequest) -> RedisWriteResult:
    """改 string 的值。"""
    key = req.key

    async def action(client: Any) -> Any:
        current = str(await client.type(key))
        if current not in ("none", "string"):
            raise OpsDbError(f"该 key 是 {current} 类型，不能按 string 改值")
        # 先记 pttl 再补回：SET 会清掉过期时间，而 KEEPTTL 只有 6.0 以上才有，
        # 「改个值把缓存的 TTL 弄没了」是这里最不该发生的副作用。
        pttl = int(await client.pttl(key))
        await client.set(key, req.value)
        if pttl > 0:
            await client.pexpire(key, pttl)

    _, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(
        command=_redis_command("SET", key, [req.value]), elapsed_ms=elapsed
    )


async def add_element(item: OpsDatabase, req: RedisElementAddRequest) -> RedisWriteResult:
    """给集合加一个元素（hash 的 field 已存在则按 HSET 语义覆盖值）。"""
    key = req.key
    if req.key_type == "hash":
        if not req.field:
            raise OpsDbError("哈希字段需要同时给出 field 和 value")
        value = req.value or ""
        display = _redis_command("HSET", key, [req.field, value])
        action = lambda client: client.hset(key, req.field, value)
    elif req.key_type == "list":
        if req.value is None:
            raise OpsDbError("列表元素需要给出 value")
        head = req.position == "head"
        display = _redis_command(
            "LPUSH" if head else "RPUSH", key, [req.value]
        )
        action = lambda client: (client.lpush if head else client.rpush)(key, req.value)
    elif req.key_type == "set":
        if req.value is None:
            raise OpsDbError("集合成员需要给出 value")
        display = _redis_command("SADD", key, [req.value])
        action = lambda client: client.sadd(key, req.value)
    else:  # zset
        if req.value is None or req.score is None:
            raise OpsDbError("有序集合元素需要同时给出 member 和 score")
        display = _redis_command("ZADD", key, [f"{req.score} {req.value}"])
        action = lambda client: client.zadd(key, {req.value: req.score})

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(raw or 0))


async def remove_element(item: OpsDatabase, req: RedisElementDeleteRequest) -> RedisWriteResult:
    """删掉集合里的一个元素。``target`` 的含义随类型变，见请求模型注释。"""
    key, key_type, target = req.key, req.key_type, req.target

    if key_type == "hash":
        display = _redis_command("HDEL", key, [target])
        action = lambda client: client.hdel(key, target)
    elif key_type == "set":
        display = _redis_command("SREM", key, [target])
        action = lambda client: client.srem(key, target)
    elif key_type == "zset":
        display = _redis_command("ZREM", key, [target])
        action = lambda client: client.zrem(key, target)
    elif key_type == "stream":
        display = _redis_command("XDEL", key, [target])
        action = lambda client: client.xdel(key, target)
    elif key_type == "list":
        try:
            index = int(target)
        except ValueError:
            raise OpsDbError("列表元素按下标删除，下标需要是整数") from None
        if index < 0:
            raise OpsDbError("列表下标不能为负")
        # 按位置精确删一个元素没有单条命令：LREM 按值删，同值会一起没掉。
        # 先把这一格换成随机哨兵，再按哨兵删一条——LSET 越界本身会报错，
        # 等于顺带替我们校验了下标。
        sentinel = f"__yangvis_del_{uuid.uuid4().hex}__"

        async def action(client: Any) -> Any:
            await client.lset(key, index, sentinel)
            return await client.lrem(key, 1, sentinel)

        display = f"LREM {_redis_arg(key)} 1 <第 {index} 个元素>"
    else:
        raise OpsDbError("string 没有元素可删，请改值或删除整个 key")

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(raw or 0))


async def set_ttl(item: OpsDatabase, req: RedisTtlRequest) -> RedisWriteResult:
    """EXPIRE / PERSIST。"""
    key = req.key
    if req.action == "expire":
        if not req.seconds:
            raise OpsDbError("设置过期需要给出秒数")

        async def action(client: Any) -> Any:
            expired = await client.expire(key, req.seconds)
            # EXPIRE 对不存在的 key 返回 0：此时用户以为设上了，其实什么都没发生。
            if not expired:
                raise OpsDbError("key 不存在或已过期")
            return expired

        display = f"EXPIRE {_redis_arg(key)} {req.seconds}"
    else:
        action = lambda client: client.persist(key)
        display = f"PERSIST {_redis_arg(key)}"

    raw, elapsed = await _redis_write(item, req.db, action)
    return RedisWriteResult(command=display, elapsed_ms=elapsed, deleted=int(bool(raw)))


async def delete_keys(item: OpsDatabase, req: RedisKeyDeleteRequest) -> RedisWriteResult:
    """批量删除 key。用 UNLINK 而不是 DEL：大集合的回收交给后台线程，不卡主线程。"""
    keys = [k for k in req.keys if k]
    if not keys:
        raise OpsDbError("没有选中任何 key")
    display = _redis_command("UNLINK", keys[0], keys[1:])

    raw, elapsed = await _redis_write(item, req.db, lambda client: client.unlink(*keys))
    return RedisWriteResult(
        command=display, elapsed_ms=elapsed, deleted=int(raw or 0)
    )
