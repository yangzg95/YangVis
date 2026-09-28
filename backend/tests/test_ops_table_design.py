"""设计表（表定义编辑）的 diff 与执行闸门测试。

改表是这套运维界面里唯一会重建整张表的动作，而语句全部由服务端按「期望定义 vs
information_schema 现状」生成，所以生成逻辑的每条分支、以及「回传的语句只可能改
这一张表」这条兜底都得钉住——它们在界面上点不全。
"""
import asyncio
import re

import pytest

from app.models.entities import OpsDatabase
from app.models.schemas import (
    DbColumnDef,
    DbColumnItem,
    DbIndexDef,
    DbIndexItem,
    DbTableDef,
    DbTableDefUpdate,
)
from app.services import ops_database as db_ops


def col(name, column_type="varchar(64)", *, nullable=True, default=None, extra="", comment=""):
    return DbColumnItem(
        name=name,
        column_type=column_type,
        nullable=nullable,
        default=default,
        extra=extra,
        comment=comment,
    )


def want(name, column_type="varchar(64)", *, origin=None, nullable=True, auto=False,
         has_default=False, default=None, on_update=False, comment=""):
    return DbColumnDef(
        name=name,
        origin_name=origin,
        column_type=column_type,
        nullable=nullable,
        auto_increment=auto,
        on_update_current_timestamp=on_update,
        has_default=has_default,
        default=default,
        comment=comment,
    )


def show(payload, *, columns, primary_key=(), indexes=(), comment=""):
    """跑一次纯 diff。表固定是 ``shop.orders``，断言里直接写全限定名。"""
    definition = DbTableDef(
        schema_name="shop",
        table="orders",
        columns=list(columns),
        primary_key=list(primary_key),
        indexes=list(indexes),
        comment=comment,
    )
    return db_ops.build_alter_preview("shop", "orders", definition, payload)


def sqls(result):
    return [a.sql for a in result.actions]


HEAD = "ALTER TABLE `shop`.`orders` "


# ---- 列 ---------------------------------------------------------------------


def test_no_change_produces_no_statement():
    payload = DbTableDefUpdate(
        columns=[want("id", "int", nullable=False, comment="主键")], primary_key=["id"]
    )
    current = [col("id", "int", nullable=False, comment="主键")]
    assert show(payload, columns=current, primary_key=["id"]).actions == []


def test_added_column_lands_where_the_form_puts_it():
    payload = DbTableDefUpdate(
        columns=[want("id", "int", nullable=False), want("user_name", "varchar(32)", comment="用户名")]
    )
    actions = show(payload, columns=[col("id", "int", nullable=False)]).actions
    assert len(actions) == 1
    assert actions[0].kind == "column-add"
    assert actions[0].sql == HEAD + (
        "ADD COLUMN `user_name` varchar(32) NULL COMMENT '用户名' AFTER `id`"
    )


def test_column_inserted_at_head_uses_first():
    payload = DbTableDefUpdate(columns=[want("code", "varchar(8)"), want("id", "int", nullable=False)])
    assert sqls(show(payload, columns=[col("id", "int", nullable=False)])) == [
        HEAD + "ADD COLUMN `code` varchar(8) NULL FIRST"
    ]


def test_dropped_column_is_destructive_and_warned():
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False)])
    result = show(payload, columns=[col("id", "int", nullable=False), col("tmp", "varchar(8)")])
    drop = next(a for a in result.actions if a.kind == "column-drop")
    assert drop.destructive and drop.sql == HEAD + "DROP COLUMN `tmp`"
    assert any("tmp" in w for w in result.warnings)


def test_rename_uses_change_column_so_data_survives():
    payload = DbTableDefUpdate(columns=[want("user_name", "varchar(64)", origin="name")])
    actions = show(payload, columns=[col("name", "varchar(64)")]).actions
    assert len(actions) == 1
    assert actions[0].kind == "column-rename"
    assert actions[0].sql == HEAD + "CHANGE COLUMN `name` `user_name` varchar(64) NULL"


def test_rename_from_unknown_column_is_refused():
    payload = DbTableDefUpdate(columns=[want("b", "int", origin="a")])
    with pytest.raises(db_ops.OpsDbError, match="不存在"):
        show(payload, columns=[col("x", "int")])


def test_narrowing_type_warns_about_truncation():
    payload = DbTableDefUpdate(columns=[want("amount", "decimal(6,2)", nullable=False)])
    result = show(payload, columns=[col("amount", "decimal(10,2)", nullable=False)])
    assert sqls(result) == [HEAD + "MODIFY COLUMN `amount` decimal(6,2) NOT NULL"]
    assert any("decimal(10,2)" in w for w in result.warnings)


def test_pure_reorder_moves_the_minimum_number_of_columns():
    payload = DbTableDefUpdate(
        columns=[want("b", "int"), want("a", "int")], primary_key=[]
    )
    actions = show(payload, columns=[col("a", "int"), col("b", "int")]).actions
    # MySQL 改顺序要整表拷贝，所以换两列的位置只该生成一句。
    assert len(actions) == 1
    assert actions[0].note == "调整列顺序"
    assert re.search(r"\bFIRST\b|\bAFTER\b", actions[0].sql)


def test_reordered_columns_do_not_touch_untouched_neighbours():
    payload = DbTableDefUpdate(columns=[want("a", "int"), want("c", "int"), want("b", "int")])
    actions = show(
        payload, columns=[col("a", "int"), col("b", "int"), col("c", "int")]
    ).actions
    assert len(actions) == 1
    assert "MODIFY COLUMN `b` int NULL AFTER `c`" in actions[0].sql


def test_default_value_takes_three_shapes():
    payload = DbTableDefUpdate(
        columns=[
            want("id", "int", nullable=False),
            want("status", "tinyint", has_default=True, default="0"),
            want("note", "varchar(8)", has_default=True, default=None),
            want("created_at", "datetime", has_default=True, default="current_timestamp"),
            want("flag", "tinyint"),
        ]
    )
    added = sqls(show(payload, columns=[col("id", "int", nullable=False)]))
    assert any("`status` tinyint NULL DEFAULT '0'" in s for s in added)
    assert any("`note` varchar(8) NULL DEFAULT NULL" in s for s in added)
    # CURRENT_TIMESTAMP 是表达式，加引号 MySQL 不认。
    assert any("`created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP" in s for s in added)
    # 没勾默认值的列不该冒出 DEFAULT。
    assert [s for s in added if "`flag`" in s and "DEFAULT" in s] == []


def test_auto_increment_and_on_update_come_from_the_two_flags():
    payload = DbTableDefUpdate(
        columns=[want("id", "bigint", nullable=False, auto=True),
                 want("updated_at", "datetime", on_update=True)]
    )
    added = sqls(show(payload, columns=[col("id", "bigint", nullable=False)]))
    assert any("`id` bigint NOT NULL AUTO_INCREMENT" in s for s in added)
    assert any("`updated_at` datetime NULL ON UPDATE CURRENT_TIMESTAMP" in s for s in added)


def test_generated_column_is_refused():
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False)])
    with pytest.raises(db_ops.OpsDbError, match="生成列"):
        show(payload, columns=[col("id", "int", nullable=False), col("gen", "int", extra="GENERATED")])


def test_duplicate_column_name_is_refused():
    payload = DbTableDefUpdate(columns=[want("a", "int"), want("a", "int")])
    with pytest.raises(db_ops.OpsDbError, match="重复"):
        show(payload, columns=[col("a", "int")])


# ---- 注入面：类型串与名字是唯二直接进 DDL 的用户文本 ----------------------------


@pytest.mark.parametrize(
    "value",
    ["int; DROP TABLE users", "int /*x*/", "varchar(8)--", "int `b`", "1int",
     "int unsigned unsigned", "int) OR 1=1"],
)
def test_hostile_column_type_is_refused(value):
    payload = DbTableDefUpdate(columns=[want("a", "int"), want("b", value)])
    with pytest.raises(db_ops.OpsDbError):
        show(payload, columns=[col("a", "int")])


def test_hostile_identifier_and_comment_are_refused():
    payload = DbTableDefUpdate(
        columns=[want("a", "int"), want("b`x", "int", comment="'; DROP TABLE y; --")]
    )
    with pytest.raises(db_ops.OpsDbError):
        show(payload, columns=[col("a", "int")])


def test_quotable_names_are_escaped_and_still_pass_the_gate():
    payload = DbTableDefUpdate(columns=[want("a", "int"), want("b`c", "int")])
    sql = sqls(show(payload, columns=[col("a", "int")]))[0]
    assert "`b``c`" in sql
    assert db_ops.classify_sql_writable(sql).allowed


# ---- 主键与索引 ---------------------------------------------------------------


def test_primary_key_change_is_one_statement():
    payload = DbTableDefUpdate(columns=[want("a", "int")], primary_key=["a"])
    result = show(payload, columns=[col("a", "int", nullable=False)])
    primary = next(a for a in result.actions if a.kind == "primary")
    # DROP 与 ADD 必须同一条 ALTER：中间状态没有主键，自增列会被 MySQL 直接拒绝。
    assert primary.sql == HEAD + "ADD PRIMARY KEY (`a`)"
    # 主键列必须是 NOT NULL，收紧走的是列 diff 那句 MODIFY。
    payload_null = DbTableDefUpdate(columns=[want("a", "int")], primary_key=["a"])
    result_null = show(payload_null, columns=[col("a", "int")])
    assert any("MODIFY COLUMN `a` int NOT NULL" in s for s in sqls(result_null))


def test_primary_key_column_must_be_in_the_column_list():
    payload = DbTableDefUpdate(columns=[want("a", "int")], primary_key=["ghost"])
    with pytest.raises(db_ops.OpsDbError, match="主键列"):
        show(payload, columns=[col("a", "int")])


def test_changed_index_drops_before_rebuilding():
    payload = DbTableDefUpdate(
        columns=[want("a", "int")], indexes=[DbIndexDef(name="idx_a", unique=True, columns=["a"])]
    )
    actions = show(
        payload, columns=[col("a", "int")], indexes=[DbIndexItem(name="idx_a", columns=["a"])]
    ).actions
    assert [a.sql for a in actions] == [
        HEAD + "DROP INDEX `idx_a`",
        HEAD + "ADD UNIQUE KEY `idx_a` (`a`)",
    ]
    assert actions[0].destructive and not actions[1].destructive


def test_index_on_unknown_column_is_refused():
    payload = DbTableDefUpdate(
        columns=[want("a", "int")], indexes=[DbIndexDef(name="idx", columns=["ghost"])]
    )
    with pytest.raises(db_ops.OpsDbError, match="不存在"):
        show(payload, columns=[col("a", "int")])


def test_index_reordering_is_a_rebuild_not_a_no_op():
    payload = DbTableDefUpdate(
        columns=[want("a", "int"), want("b", "int")],
        indexes=[DbIndexDef(name="idx_ab", columns=["b", "a"])],
    )
    actions = show(
        payload,
        columns=[col("a", "int"), col("b", "int")],
        indexes=[DbIndexItem(name="idx_ab", columns=["a", "b"])],
    ).actions
    assert len(actions) == 2


def test_table_comment_only_touches_comment():
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False)], comment="订单表")
    result = show(payload, columns=[col("id", "int", nullable=False)], comment="")
    assert sqls(result) == [HEAD + "COMMENT = '订单表'"]
    assert result.actions[0].kind == "comment"


def test_empty_primary_key_means_drop_it_and_warns():
    # 期望定义是「整张表」：payload 不带主键就是「没有主键」，不是「保持原样」。
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False)])
    result = show(payload, columns=[col("id", "int", nullable=False)], primary_key=["id"])
    assert sqls(result) == [HEAD + "DROP PRIMARY KEY"]
    assert any("没有主键" in w for w in result.warnings)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"comment": "it's the key"},
        {"comment": "a\\b"},
        {"has_default": True, "default": "x' OR 1=1 --"},
        {"has_default": True, "default": "a\\'b"},
    ],
)
def test_quoted_text_in_comment_and_default_is_refused(kwargs):
    """内联字面量只有反斜杠转义一层保护，NO_BACKSLASH_ESCAPES 下会失效，所以直接拒。"""
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False), want("b", "int", **kwargs)])
    with pytest.raises(db_ops.OpsDbError):
        show(payload, columns=[col("id", "int", nullable=False)])


def test_table_comment_with_quote_is_refused():
    payload = DbTableDefUpdate(columns=[want("id", "int", nullable=False)], comment="订单' ; DROP")
    with pytest.raises(db_ops.OpsDbError):
        show(payload, columns=[col("id", "int", nullable=False)])


# ---- 执行闸门：回传的语句只可能改这一张表 --------------------------------------


def apply(statements):
    item = OpsDatabase(
        id=1, owner_id=1, name="台账", db_type="mysql", host="h", username="u", writable=True
    )
    # 全部被拒时一句都不会进数据库连接，所以这些用例不需要真实 MySQL。
    return asyncio.run(db_ops.apply_alter(item, "shop", "orders", statements))


def test_apply_refuses_statements_aimed_at_other_tables():
    result, any_forbidden = apply(
        ["ALTER TABLE `shop`.`other` DROP COLUMN `x`", "ALTER TABLE `x`.`orders` DROP COLUMN `y`"]
    )
    assert (result.total, result.succeeded, result.failed) == (2, 0, 2)
    assert any_forbidden
    assert all("只能修改 shop.orders" in s.message for s in result.statements)
    assert [s.index for s in result.statements] == [1, 2]


def test_apply_refuses_lowercased_target_so_it_cannot_slip_past_the_prefix_check():
    # Linux 上表名大小写敏感，小写前缀不是「同一条语句」，而是另一张表。
    result, any_forbidden = apply(["alter table `shop`.`orders` drop column `x`"])
    assert result.failed == 1 and any_forbidden


def test_apply_refuses_a_second_statement_hidden_in_one_line():
    result, any_forbidden = apply(
        ["ALTER TABLE `shop`.`orders` DROP COLUMN `x`; DROP TABLE `shop`.`users`"]
    )
    assert result.failed == 1 and any_forbidden
    assert "多条语句" in result.statements[0].message


def test_apply_refuses_empty_statement():
    result, _ = apply(["   "])
    assert result.statements[0].message == "语句为空"
