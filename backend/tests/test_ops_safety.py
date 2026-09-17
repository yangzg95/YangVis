"""SQL 拆分器与只读判定的测试。

判定器是「让 AI / 用户碰数据库」这件事的第一道门，各种绕过手法和误报
回归都钉在这里。
"""
from app.services.ops_safety import (
    classify_redis,
    classify_redis_writable,
    classify_sql,
    classify_sql_writable,
    split_sql_script,
    strip_sql_comments,
)


# ---- split_sql_script ---------------------------------------------------------


def test_split_multiple_statements():
    assert split_sql_script("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_split_keeps_semicolon_inside_string():
    assert split_sql_script("SELECT 'a;b'; SELECT 2") == ["SELECT 'a;b'", "SELECT 2"]


def test_split_keeps_semicolon_inside_backticks():
    assert split_sql_script("SELECT `a;b` FROM t") == ["SELECT `a;b` FROM t"]


def test_split_ignores_semicolon_inside_comments():
    assert split_sql_script("SELECT 1 /* ; */; SELECT 2") == [
        "SELECT 1 /* ; */",
        "SELECT 2",
    ]
    assert split_sql_script("-- ;\nSELECT 1") == ["-- ;\nSELECT 1"]
    assert split_sql_script("# ;\nSELECT 1") == ["# ;\nSELECT 1"]


def test_split_handles_escaped_and_doubled_quotes():
    assert split_sql_script(r"SELECT 'it\'s;ok'") == [r"SELECT 'it\'s;ok'"]
    assert split_sql_script("SELECT 'it''s;x'") == ["SELECT 'it''s;x'"]


def test_split_drops_comment_only_and_empty_pieces():
    assert split_sql_script("/* 只有注释 */") == []
    assert split_sql_script("SELECT 1;; ;") == ["SELECT 1"]
    assert split_sql_script("") == []


def test_split_tolerates_unclosed_string():
    # 未闭合的字符串保守地吞到文末，里面的分号不是切分点。
    assert split_sql_script("SELECT 'abc; SELECT 2") == ["SELECT 'abc; SELECT 2"]


# ---- strip_sql_comments -------------------------------------------------------


def test_strip_comments_keeps_string_content():
    # 字符串里的 -- / /* 不是注释，老正则实现会把字符串截断。
    assert strip_sql_comments("SELECT 'a--b'").strip() == "SELECT 'a--b'"
    assert strip_sql_comments("SELECT 'a/*b'").strip() == "SELECT 'a/*b'"


def test_strip_comments_removes_real_comments():
    assert strip_sql_comments("SELECT 1 -- x").strip() == "SELECT 1"
    assert strip_sql_comments("SELECT 1 /* x */ + 2").strip() == "SELECT 1         + 2"


# ---- classify_sql：误报回归 ----------------------------------------------------


def test_classify_allows_semicolon_inside_string():
    assert classify_sql("SELECT 'a;b'").allowed


def test_classify_allows_keywords_inside_string():
    assert classify_sql("SELECT 'for update'").allowed
    assert classify_sql("SELECT 'sleep(1)'").allowed


# ---- classify_sql：拦截不放宽 --------------------------------------------------


def test_classify_still_blocks_multi_statement():
    verdict = classify_sql("SELECT 1; DROP TABLE t")
    assert verdict.kind == "forbidden"


def test_classify_still_blocks_write_and_dangerous_clauses():
    assert classify_sql("DELETE FROM t").kind == "forbidden"
    assert classify_sql("SELECT * FROM t FOR UPDATE").kind == "forbidden"
    assert classify_sql("SELECT SLEEP(1)").kind == "forbidden"
    assert classify_sql("/*x*/ DELETE FROM t").kind == "forbidden"
    assert classify_sql("SELECT * FROM t -- ;\n").allowed


# ---- classify_sql_writable：连接开了写开关的控制台 ---------------------------------


def test_writable_allows_dml_and_ddl():
    assert classify_sql_writable("INSERT INTO t (a) VALUES (1)").allowed
    assert classify_sql_writable("UPDATE t SET a = 1 WHERE id = 2").allowed
    assert classify_sql_writable("DELETE FROM t WHERE id = 2").allowed
    assert classify_sql_writable("REPLACE INTO t (a) VALUES (1)").allowed
    assert classify_sql_writable("CREATE TABLE t (id INT)").allowed
    assert classify_sql_writable("ALTER TABLE t ADD COLUMN b INT").allowed
    assert classify_sql_writable("DROP TABLE t").allowed
    assert classify_sql_writable("TRUNCATE TABLE t").allowed
    # 查询与锁子句在可写会话里是正当用法。
    assert classify_sql_writable("SELECT * FROM t FOR UPDATE").allowed
    assert classify_sql_writable("START TRANSACTION").allowed


def test_writable_still_blocks_multi_statement_and_comment_bypass():
    assert classify_sql_writable("UPDATE t SET a = 1; DROP TABLE t").kind == "forbidden"
    assert classify_sql_writable("/*x*/ UPDATE t SET a = 1").allowed
    assert classify_sql_writable("UPDATE t SET a = 'x;y' WHERE id = 1").allowed


def test_writable_still_blocks_dangerous_functions():
    assert classify_sql_writable("SELECT * FROM t INTO OUTFILE '/tmp/x'").kind == "forbidden"
    assert classify_sql_writable("SELECT LOAD_FILE('/etc/passwd')").kind == "forbidden"
    assert classify_sql_writable("SELECT SLEEP(1)").kind == "forbidden"
    assert classify_sql_writable("LOAD DATA INFILE '/tmp/x' INTO TABLE t").kind == "forbidden"


def test_writable_still_blocks_account_and_instance_ops():
    assert classify_sql_writable("GRANT ALL ON *.* TO 'x'@'%'").kind == "forbidden"
    assert classify_sql_writable("REVOKE ALL ON *.* FROM 'x'@'%'").kind == "forbidden"
    assert classify_sql_writable("CREATE USER 'x'@'%' IDENTIFIED BY 'p'").kind == "forbidden"
    assert classify_sql_writable("DROP USER 'x'@'%'").kind == "forbidden"
    assert classify_sql_writable("SET GLOBAL max_connections = 100").kind == "forbidden"
    assert classify_sql_writable("SHUTDOWN").kind == "forbidden"
    # 字符串里的 grant / shutdown 不是操作，不能误伤。
    assert classify_sql_writable("INSERT INTO t VALUES ('grant shutdown')").allowed


# ---- classify_redis_writable -------------------------------------------------------


def test_redis_writable_allows_writes():
    assert classify_redis_writable("SET k v").allowed
    assert classify_redis_writable("DEL k").allowed
    assert classify_redis_writable("HSET h f v").allowed
    assert classify_redis_writable("GET k").allowed


def test_redis_writable_keeps_hard_deny():
    assert classify_redis_writable("FLUSHALL").kind == "forbidden"
    assert classify_redis_writable("FLUSHDB").kind == "forbidden"
    assert classify_redis_writable("EVAL \"return 1\" 0").kind == "forbidden"
    assert classify_redis_writable("DEBUG SLEEP 0").kind == "forbidden"
    assert classify_redis_writable("SHUTDOWN").kind == "forbidden"
    # CONFIG SET / REWRITE 能借 RDB 落盘写任意文件，可写模式下同样拦。
    assert classify_redis_writable("CONFIG SET dir /tmp").kind == "forbidden"
    assert classify_redis_writable("CONFIG GET maxmemory").allowed
    assert classify_redis("CONFIG SET dir /tmp").kind == "forbidden"
