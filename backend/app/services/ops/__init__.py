"""智能运维的业务包：让 AI 操作服务器与数据库所需的那十件事。

这一族原本是十个带 ``ops_`` 前缀的平铺模块，前缀就是它们唯一的共同语言。收进
包里之后前缀没了：``ops_safety.py`` 是 ``ops/safety.py``，``ops_db_exec.py`` 是
``ops/db_exec.py``。

这里没有唯一的入口类——十件事各有一个模块，所以 ``__init__`` 只当包文档、**不做
转发门面**，调用方按子模块取名字（``from app.services.ops import safety`` 这类）。
子模块分两批。

服务器侧：

* :mod:`app.services.ops.safety` —— 命令与 SQL 的安全判定，刻意写成不碰网络不碰
  数据库的纯函数，是整件事的边界所在，绕过手法一条条钉在测试里；
* :mod:`app.services.ops.server` —— 服务器台账（凭据加密、按 owner 隔离、主机公钥
  TOFU）加 SSH 执行通道：交互式 PTY 给终端，一次性执行给 AI 工具；
* :mod:`app.services.ops.sftp` —— 远程文件浏览器的文件操作层，收一条已建好的连接
  自己开 SFTP，不做路径沙箱；
* :mod:`app.services.ops.actions` —— 写命令的两阶段待确认项：落库状态机，confirm
  请求自己 CAS 认领 pending 行、在所在 worker 上执行并写回，所以多 worker 也搬得动。

数据库侧：

* :mod:`app.services.ops.database` —— 数据库台账与 SQL 收藏，密码 Fernet 落库、
  出参一律打码，本模块不碰任何网络连接；
* :mod:`app.services.ops.db_exec` —— 连接与执行通道，「默认只读」不靠解析兜底，
  MySQL 会话开在 ``READ ONLY`` 事务里，Redis 走白名单加硬禁用清单；
* :mod:`app.services.ops.db_schema` —— 元数据浏览：库 / 表 / 列 / 索引 / 外键 /
  DDL / ER 图 / 补全，标识符转义、值参数化、会话只读三层各管各的；
* :mod:`app.services.ops.db_redis` —— Redis 键浏览与按类型限流的结构化读写；
* :mod:`app.services.ops.db_data` —— 数据浏览、全量导出与行级写；
* :mod:`app.services.ops.db_design` —— 设计表：把期望定义与 information_schema 的
  现状 diff 成 ALTER 语句，预览与执行是两次请求。

依赖单向：safety、actions、database、server、sftp 谁都不接（sftp 只收 server 建好
的连接对象，不 import 它）；db_exec 接 database 与 safety；db_schema 与 db_redis 在
db_exec 之上；db_data 与 db_design 最高，接其余 db_* 加 safety。台账那两个模块只碰
自己的表，碰网络的活全在执行通道里。
"""
