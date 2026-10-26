# Redis 控制台（SCAN 浏览 + 结构化读写）

运维「数据库」页里 Redis 连接的键工作台：左边树展开逻辑库，右边页签做 SCAN 浏览和
key 详情。它不是一个裸命令输入框——界面上交给后端的永远是坐标（key / field /
member / 下标），实际命令由服务端用 redis-py 的类型化方法拼出，值作为独立参数进
协议层，不经过任何命令文本解析。默认全程只读，只有台账连接打开「允许写入」且账号
有 `ops_write` 时才出现写按钮。

## 用户可见行为

- 连接树第一层是逻辑库列表：来自 `INFO keyspace`，只列真有 key 的库，外加台账配置的
  默认库（即使是空的），每个库带 key 计数
- key 列表：`user:*` 式 pattern 过滤（最长 256），SCAN 游标翻页，底部「加载更多」；
  每行带类型彩色标签，SCAN 不保证不重复，前端追加时按 key 去重；双击行打开详情页签
- key 详情：类型、TTL（`TTL n s` / 永不过期 / 不存在）、按类型塑形的值；集合太大时
  只显示前 100 个元素并挂黄色截断提示；module 类型（RedisJSON / Bloom 等）无通用
  取值方式，值区如实留空
- 只读视图（连接没开写入或账号无 `ops_write`）：顶部提示条，没有勾选框、没有任何
  写按钮
- 开启写入后：
  - **新建 key**：string/list/set/zset/hash 五类（stream 条目的 id 只能由服务端生成，
    不在候选里），集合类带一个初始元素建出 key，可填过期秒数；key 已存在直接报错，
    绝不覆盖
  - **改 string 值**：文本框编辑，界面明说「保存只替换值，原有 TTL 保持不变」
  - **元素增删**：hash 加/覆盖 field、list 选头部 LPUSH 或尾部 RPUSH、set SADD、
    zset 带 score ZADD；删除时 hash 删 field、set/zset 删 member、stream 按条目 id
    XDEL、**list 按下标删**（同值的其它元素不受影响）
  - **TTL**：弹层填秒数设过期（常用 60/3600/86400 提示），或一键 PERSIST 取消过期
  - **批量删除**：列表勾选后一键 UNLINK（单次最多 200 个），确认框列出前三个 key；
    key 详情页的「删除 key」也走同一个接口
- 每次写成功的 toast 直接展示服务端生成的命令文本（如 `HSET user:1 name 张三`），
  超长截断；所有写操作落审计，失败也记

## 关键取舍

### 为什么浏览走 SCAN 而不是 KEYS

`KEYS pattern` 是一次性全库扫描，key 多的时候 Redis 主线程被长时间占住，同一实例上
的业务一起被拖——运维面板不该成为事故源。浏览一律走游标式 SCAN：一次取一页
（前端 100、后端上限 500），配合非事务 pipeline 批量补 `TYPE`，`cursor="0"` 表示
扫完。代价是 SCAN 只保证最终一致、可能返回重复 key，由前端追加去重兜底；翻页期间
别的客户端写入或删除造成的新旧交错，界面不做快照一致性承诺。取值同样限流：集合类
最多带回 100 个元素，string 截到 `OPS_OUTPUT_LIMIT`，超出只给 `truncated` 标记。

### 写开关为什么挂在连接级

闸门是两道叠加：台账行 `ops_database.writable` 且用户 `can_ops_write`，缺一不可，
后端 `_run_redis_write` 每个写端点都重新判定（前端置灰只是体验，不是安全边界），
被拒的动作也以 `forbidden` 落审计。开关属于连接而不是用户：同一账号管着生产和测试
两台 Redis 时，可以只给测试连接开写；复制一份只读台账也比维护一套「人 × 实例」
权限矩阵便宜。Redis 没有 MySQL 那种服务端只读会话，结构化写路径不做命令关键字网关
（值根本进不了命令文本），但手敲的 Redis 控制台仍是白名单 + 硬禁用（FLUSHALL、
EVAL、CONFIG SET 等），两套通道各自独立，元数据浏览永远只读。

### 改 string 为什么能保留 TTL

`SET key value` 会无条件清掉过期时间，而 `SET KEEPTTL` 只有 Redis 6.0 以上才有，
不能假设实例版本。服务层的顺序是同一连接上先 `PTTL` 记下毫秒 TTL，再 `SET`，
`pttl > 0` 时补一条 `PEXPIRE` 原样还回；本来就永不过期的 key 不补任何东西。
「改个值把缓存 TTL 弄没了」是这个界面最不该发生的副作用，所以宁可多两条命令也不
依赖新版本特性。新建 key 的 EXISTS 预检、写入、补 EXPIRE 同样收在同一条连接里连着
做（非事务，极端并发下 EXISTS 与 SET 之间仍有竞态窗口，靠报错而非静默覆盖兜底）。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/ops/db_redis.py` | 逻辑库列表、SCAN、key 详情取值限流、六类结构化写、命令/审计文本生成 |
| `backend/app/services/ops/db_exec.py` | 连接原语 `_connect_redis`、`_password_of`、默认库解析、`MAX_PAGE_SIZE` |
| `backend/app/services/ops/database.py` | `OpsDatabaseService`：连接台账（与 MySQL 共用 `ops_database` 表） |
| `backend/app/routers/ops.py` | Redis 路由、类型校验（调错 MySQL/Redis 给 400）、写闸门与审计骨架 |
| `backend/app/models/schemas.py` | `RedisScanResult` / `RedisKeyDetail` 及六个写请求模型（含长度/范围约束） |
| `frontend/src/views/OpsDatabase.vue` | 页签宿主，`redis-keys` / `redis-key` 两种页签的挂载与互跳 |
| `frontend/src/components/ops/RedisKeysTab.vue` | key 列表：pattern、游标翻页、勾选批量删、新建弹窗 |
| `frontend/src/components/ops/RedisKeyTab.vue` | key 详情：值渲染、改 string、元素增删、TTL 弹层、删 key |
| `frontend/src/api/ops.ts` | `opsApi.redis*` 八个方法，统一 60s 超时、`skipErrorToast` |

连接台账复用：Redis 不是单独一张表。`OpsDatabaseService(db, user_id).get(id)` 取出
`ops_database` 行（`db_type="redis"`），`db_name` 字段对 Redis 是默认库序号
（`"0"`，非数字回退 0）；密码是行内加密字段，用时解密。每次浏览/写操作都新建一条
`redis.asyncio` 客户端（`decode_responses=True`），`finally` 里 `aclose()`，不做连接
池；浏览成功还会顺手把台账的「未测试」连通状态翻成正常（仅状态翻转时落库）。

### 接口一览（前缀 `/api/ops`）

| 方法 + 路径 | 服务层 | 实际 Redis 命令 |
| --- | --- | --- |
| `GET /databases/{id}/schemas` | `_list_redis_dbs` | `INFO keyspace` |
| `GET /databases/{id}/keys` | `scan_keys` | `SCAN cursor MATCH .. COUNT n` + pipeline `TYPE` |
| `GET /databases/{id}/key?key=&db=` | `key_detail` | `TYPE` `TTL` 加各类型只读命令 |
| `POST /databases/{id}/redis/keys` | `create_key` | `SET` / `RPUSH` / `SADD` / `ZADD` / `HSET`，先 `EXISTS`，可选 `EXPIRE` |
| `PUT /databases/{id}/redis/key` | `set_string` | `TYPE` → `PTTL` → `SET` →（需要时）`PEXPIRE` |
| `POST /databases/{id}/redis/key/elements` | `add_element` | `HSET` / `LPUSH` / `RPUSH` / `SADD` / `ZADD` |
| `DELETE /databases/{id}/redis/key/elements` | `remove_element` | `HDEL` / `SREM` / `ZREM` / `XDEL`；list 为 `LSET 下标 哨兵` + `LREM key 1 哨兵` |
| `POST /databases/{id}/redis/key/ttl` | `set_ttl` | `EXPIRE`（对不存在的 key 返回 0 时转成报错）/ `PERSIST` |
| `POST /databases/{id}/redis/keys/delete` | `delete_keys` | `UNLINK`（最多 200 个，后台回收不卡主线程） |

查询/请求体约束：`db` 0–15；key 最长 4096；写值（string/元素）上限 1 MiB；
TTL 秒数 1–3,153,600,000（约 100 年，前后端同一上限）；`keys` 列表 1–200 个。

list 按下标删除没有单条原生命令：`LREM` 按值删会把同值元素一起带走。做法是先
`LSET key index <随机哨兵>` 再 `LREM key 1 <同一哨兵>`，哨兵含一次 uuid hex，业务值
不可能撞上；LSET 越界自带报错，顺带完成下标校验。审计文本显示为
`LREM key 1 <第 N 个元素>`，不泄露哨兵。

审计文本只用于展示与留痕，产出后从不回放执行：单参数超 2000 字符截成
`…（共 N 字符）`、多参数只列前 10 个、换行压成字面 `\n`、`shlex.quote` 转义。
命令还没生成就失败（参数语义错误、闸门拒绝）时审计记意图文本 `[动作] key`。

## 配置

没有 Redis 控制台专属开关，复用运维通用设置：

| 变量 | 默认 | 在这里的用途 |
| --- | --- | --- |
| `OPS_SSH_TIMEOUT` | 15 | Redis 建连超时 `socket_connect_timeout`（秒） |
| `OPS_CMD_TIMEOUT` | 20 | 单条命令读写超时 `socket_timeout`（秒） |
| `OPS_OUTPUT_LIMIT` | 8192 | string 详情值的截断字符数 |

代码常量（非环境变量）：`db_exec.MAX_PAGE_SIZE = 500`（SCAN count 后端上限）、
`db_redis._COLLECTION_LIMIT = 100`（集合类详情元素上限）、前端 `PAGE_SIZE = 100`
（实际每页请求数）。

## 测试

`backend/tests/test_ops_redis_write.py` 覆盖服务层结构化写，约 20 个用例，连接层
整体换成 `FakeRedis`（monkeypatch `_connect_redis`，按命令名记调用/回预设回复），
不在共享实例上动真数据。实际覆盖点：

- 新建：五类 key 各自发出的命令与参数形态（含 zset score 转 float、hash mapping）、
  EXISTS 拒绝已存在 key 且拒绝前不发写命令、非法集合载荷、TTL 同连接补 EXPIRE、
  审计文本参数个数截断
- 改 string：PTTL→SET→PEXPIRE 的调用顺序与毫秒值还原、无 TTL 不发 PEXPIRE、
  非 string 类型拒绝且不发 SET
- 元素增删：四类添加命令映射及各自必填参数；HDEL/SREM/ZREM/XDEL 映射；list 的
  LSET+LREM 哨兵方案（两处哨兵一致、下标转 int）；非数字/负下标拒绝；string 无元素可删
- TTL：EXPIRE 返回 0 转「key 不存在」错误、PERSIST 文本、缺秒数拒绝
- 批量删除：UNLINK 参数透传与删除数回执、30 个 key 时审计文本截到前 10 个、空选择拒绝
- 文本与收尾：超长值截 2000 字符并附总长度、多行压成一行；RedisError 转 OpsDbError
  且连接仍被 `aclose()`

未覆盖：没有路由级（HTTP/鉴权/闸门/审计落库）测试，也没有接真实 Redis 的集成测试；
浏览路径（SCAN、`_read_redis_value` 各类型分支、INFO keyspace）无自动化测试。
`test_ops_safety.py` 里的 `classify_redis*` 用例属于手敲控制台的命令网关，不是本页
的结构化写路径。
