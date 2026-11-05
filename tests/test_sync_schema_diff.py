"""结构比对引擎：内存 MetaData 构造的漂移矩阵（M2 验收）。"""

from sqlalchemy import (
    Column, ForeignKey, ForeignKeyConstraint, Index, Integer, MetaData, String,
    Table, Text, VARCHAR,
)
from sqlalchemy.dialects.mysql import BIGINT as MBIGINT, TINYINT as MTINYINT

from callfans.core.sync.config import TableRule
from callfans.core.sync.schema_diff import diff_schemas


def _rule(name, **kw):
    return TableRule(name=name, **kw)


def _md(tables):
    md = MetaData()
    for builder in tables:
        builder(md)
    return md


# ---------- 期望构造器 ----------

def _config_table(md, with_remark=True, label_len=64, extra_col=None, with_idx=True):
    cols = [
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("k", VARCHAR(64), nullable=False),
        Column("label", VARCHAR(0) if False else String(label_len), nullable=False),
    ]
    if with_remark:
        cols.append(Column("remark", Text))
    if extra_col is not None:
        cols.append(extra_col)
    t = Table("sys_config", md, *cols)
    if with_idx:
        Index("idx_label", t.c.label)
    return t


def _dict_table(md):
    t = Table("sys_dict", md,
              Column("code", VARCHAR(32), primary_key=True),
              Column("value", Text))
    Index("idx_value", t.c.value, unique=False)
    return t


class TestTableLevel:
    def test_no_diff_when_identical(self):
        manifest = [_rule("sys_config"), _rule("sys_dict")]
        cloud = _md([_config_table, _dict_table])
        local = _md([_config_table, _dict_table])
        result = diff_schemas(cloud, local, manifest)
        assert result.changes == []
        assert result.summary() == "结构无差异"

    def test_int_display_width_no_false_positive(self):
        """归一化消噪：INTEGER(11) 与 INT 不应产生差异（反射来源差异）。"""
        from sqlalchemy.dialects.mysql import INTEGER as MINT

        manifest = [_rule("sys_config")]

        def plain(md, int_type):
            return Table("sys_config", md,
                         Column("id", int_type, primary_key=True),
                         Column("k", VARCHAR(64)))

        cloud = _md([lambda md: plain(md, MINT(display_width=11))])
        local = _md([lambda md: plain(md, Integer)])
        assert diff_schemas(cloud, local, manifest).changes == []

    def test_add_table(self):
        manifest = [_rule("sys_config"), _rule("sys_dict")]
        cloud = _md([_config_table, _dict_table])
        local = _md([_config_table])  # 缺 sys_dict
        result = diff_schemas(cloud, local, manifest)
        kinds = [c.kind for c in result.changes]
        assert "add_table" in kinds
        add = next(c for c in result.changes if c.kind == "add_table")
        assert "CREATE TABLE `sys_dict`" in add.ddl
        assert "add_index" in kinds  # 二级索引随后建

    def test_drop_table_destructive(self):
        manifest = [_rule("sys_config"), _rule("sys_dict")]
        cloud = _md([_config_table])                     # 云端没有 sys_dict
        local = _md([_config_table, _dict_table])        # 本地有
        result = diff_schemas(cloud, local, manifest)
        drops = [c for c in result.changes if c.kind == "drop_table"]
        assert len(drops) == 1 and drops[0].destructive
        assert "DROP TABLE `sys_dict`" in drops[0].ddl

    def test_table_not_in_manifest_untouched(self):
        """不在清单里的表（业务表）绝不碰。"""
        manifest = [_rule("sys_config")]
        cloud = _md([_config_table])
        local = _md([_config_table, _dict_table])
        result = diff_schemas(cloud, local, manifest)
        assert all(c.table == "sys_config" for c in result.changes)


class TestColumnLevel:
    def _pair(self, local_builder):
        manifest = [_rule("sys_config")]
        return diff_schemas(
            _md([_config_table]), _md([local_builder]), manifest
        )

    def test_add_column(self):
        def local(md):
            return _config_table(md, with_remark=False)

        result = self._pair(local)
        adds = [c for c in result.changes if c.kind == "add_column"]
        assert len(adds) == 1
        assert "ADD COLUMN `remark`" in adds[0].ddl

    def test_drop_column_destructive(self):
        def local(md):
            return _config_table(md, extra_col=Column("legacy", Integer))

        result = self._pair(local)
        drops = [c for c in result.changes if c.kind == "drop_column"]
        assert len(drops) == 1 and drops[0].destructive
        assert "DROP COLUMN `legacy`" in drops[0].ddl

    def test_modify_column_type(self):
        def local(md):
            return _config_table(md, label_len=128)  # varchar(64) → varchar(128)

        result = self._pair(local)
        mods = [c for c in result.changes if c.kind == "modify_column"]
        assert len(mods) == 1
        assert "MODIFY COLUMN `label`" in mods[0].ddl
        assert "varchar(128)" in mods[0].detail and "varchar(64)" in mods[0].detail
        assert not mods[0].destructive

    def test_modify_column_unsigned_detected(self):
        """unsigned 差异必须检出（方言编译回归）。"""
        cloud = _md([lambda md: Table(
            "sys_config", md,
            Column("id", MBIGINT(unsigned=True), primary_key=True),
            Column("k", VARCHAR(64), nullable=False),
        )])
        local = _md([lambda md: Table(
            "sys_config", md,
            Column("id", MBIGINT(), primary_key=True),
            Column("k", VARCHAR(64), nullable=False),
        )])
        result = diff_schemas(cloud, local, [_rule("sys_config")])
        mods = [c for c in result.changes if c.kind == "modify_column"]
        assert len(mods) == 1
        assert "unsigned" in mods[0].ddl.lower()
        assert "unsigned" in mods[0].detail  # 指纹层面已检出

    def test_column_comment_only_change_detected(self):
        """列注释差异必须检出（2026-09-20 实测盲区：仅改 COMMENT 曾被判无差异）。"""
        manifest = [_rule("sys_config")]

        def table_with(comment):
            return lambda md: Table(
                "sys_config", md,
                Column("id", Integer, primary_key=True),
                Column("k", VARCHAR(64), nullable=False, comment=comment),
            )

        cloud = _md([table_with("机器名称")])
        local = _md([table_with(None)])
        result = diff_schemas(cloud, local, manifest)
        mods = [c for c in result.changes if c.kind == "modify_column"]
        assert len(mods) == 1
        assert "COMMENT '机器名称'" in mods[0].ddl
        assert "注释" in mods[0].detail and not mods[0].destructive
        # Down 回写到旧注释（本地无注释 → MODIFY 不带 COMMENT）
        assert "COMMENT" not in mods[0].down_ddl

    def test_same_comment_no_diff(self):
        manifest = [_rule("sys_config")]

        def table_with(comment):
            return lambda md: Table(
                "sys_config", md,
                Column("id", Integer, primary_key=True),
                Column("k", VARCHAR(64), nullable=False, comment=comment),
            )

        assert diff_schemas(_md([table_with("a")]), _md([table_with("a")]),
                            manifest).changes == []
        # 空串与 None 等价
        assert diff_schemas(_md([table_with("")]), _md([table_with(None)]),
                            manifest).changes == []

    def test_ignore_columns(self):
        rule = _rule("sys_config", ignore_columns=["remark", "legacy"])
        cloud = _md([_config_table])
        local = _md([lambda md: _config_table(md, with_remark=False,
                                              extra_col=Column("legacy", MTINYINT()))])
        result = diff_schemas(cloud, local, [rule])
        # 两列都被忽略 → 无结构差异
        assert result.changes == []


class TestIndexAndPk:
    def test_add_drop_index(self):
        cloud = _md([_dict_table])

        def local_no_idx(md):
            t = Table("sys_dict", md,
                      Column("code", VARCHAR(32), primary_key=True),
                      Column("value", Text))
            Index("idx_other", t.c.code)  # 本地有的是另一个索引
            return t

        result = diff_schemas(cloud, _md([local_no_idx]), [_rule("sys_dict")])
        kinds = [c.kind for c in result.changes]
        assert "add_index" in kinds and "drop_index" in kinds
        add = next(c for c in result.changes if c.kind == "add_index")
        assert "idx_value" in add.ddl
        drop = next(c for c in result.changes if c.kind == "drop_index")
        assert drop.destructive

    def test_pk_change(self):
        cloud = _md([lambda md: Table(
            "sys_dict", md, Column("code", VARCHAR(32), primary_key=True),
            Column("seq", Integer, primary_key=True))])
        local = _md([lambda md: Table(
            "sys_dict", md, Column("code", VARCHAR(32), primary_key=True))])
        result = diff_schemas(cloud, local, [_rule("sys_dict")])
        pk = [c for c in result.changes if c.kind == "change_pk"]
        assert len(pk) == 1 and pk[0].destructive
        assert "ADD PRIMARY KEY (`code`, `seq`)" in pk[0].ddl

    def test_index_and_pk_changes_carry_db(self):
        """多库模式：既有表的索引/主键差异必须带库名（否则备份与 DDL 报 1046）。

        2026-11-04 现场：add_index 的 db 为空 → G1 备份对空库执行
        CREATE TABLE ... LIKE，报 No database selected，整次更新中止。
        """
        db = "callfans-standard"

        def _indexed(md, idx_name, col):
            t = Table("t_idx", md,
                      Column("id", Integer, primary_key=True),
                      Column("v", String(10)), schema=db)
            Index(idx_name, t.c[col])
            return t

        cloud, local = MetaData(), MetaData()
        _indexed(cloud, "idx_cloud", "v")
        _indexed(local, "idx_local", "id")
        result = diff_schemas(cloud, local, [_rule("t_idx")], db=db)
        add = next(c for c in result.changes if c.kind == "add_index")
        drop = next(c for c in result.changes if c.kind == "drop_index")
        assert add.db == db and drop.db == db
        assert f"`{db}`" in add.ddl and f"`{db}`" in drop.ddl

        cloud_pk, local_pk = MetaData(), MetaData()
        Table("t_pk", cloud_pk, Column("id", Integer, primary_key=True),
              Column("v", String(5)), schema=db)
        Table("t_pk", local_pk, Column("id", Integer, primary_key=True),
              Column("v", String(5), primary_key=True), schema=db)
        pk = next(c for c in diff_schemas(cloud_pk, local_pk, [_rule("t_pk")], db=db).changes
                  if c.kind == "change_pk")
        assert pk.db == db


class TestResultApi:
    def test_counts_and_summary(self):
        manifest = [_rule("sys_config"), _rule("sys_dict")]
        cloud = _md([_config_table, _dict_table])
        local = _md([lambda md: _config_table(md, with_remark=False)])
        result = diff_schemas(cloud, local, manifest)
        assert result.statement_count == len(result.changes) >= 2
        assert result.affected_tables == {"sys_config", "sys_dict"}
        assert result.destructive == []
        assert "新增列" in result.summary() and "新建表" in result.summary()


def test_add_table_records_fk_dependencies():
    """add_table 记录外键父表（父表先建），自引用不记为依赖。"""
    md = MetaData()
    Table("parent", md, Column("id", Integer, primary_key=True))
    Table("child", md,
          Column("id", Integer, primary_key=True),
          Column("parent_id", Integer, ForeignKey("parent.id")))
    Table("node", md,
          Column("id", Integer, primary_key=True),
          Column("parent_id", Integer, ForeignKey("node.id")))

    result = diff_schemas(md, MetaData(),
                          [_rule("parent"), _rule("child"), _rule("node")])
    deps = {c.table: c.depends_on for c in result.changes if c.kind == "add_table"}
    assert deps["parent"] == ()
    assert deps["child"] == ("parent",)
    assert deps["node"] == ()            # 自引用忽略


def test_skip_index_implicitly_created_by_foreign_key():
    """建表带 FK 时 MySQL 自动生成同名索引，计划不得再 CREATE INDEX（1061）。"""
    md = MetaData()
    Table("parent", md, Column("id", Integer, primary_key=True))
    Table("child", md,
          Column("id", Integer, primary_key=True),
          Column("parent_id", Integer, nullable=False),
          Column("v", Integer),
          ForeignKeyConstraint(["parent_id"], ["parent.id"],
                               name="fk_child_parent"),
          Index("fk_child_parent", "parent_id"),   # FK 自动索引（同名同列）
          Index("idx_v", "v"))

    result = diff_schemas(md, MetaData(), [_rule("parent"), _rule("child")])
    child_indexes = [c for c in result.changes
                     if c.kind == "add_index" and c.table == "child"]
    assert [c.detail for c in child_indexes] == ["v"]
    assert any(c.kind == "add_table" and c.table == "child"
               for c in result.changes)


def test_ddl_keeps_single_percent_for_raw_execution():
    """编译 DDL 不做 %% 转义（执行器原样下发，转义不会被还原）。"""
    md = MetaData()
    Table("t_pct", md, Column("id", Integer, primary_key=True),
          Column("d", String(10), server_default="100%"), schema="std")
    result = diff_schemas(md, MetaData(), [_rule("t_pct")], db="std")
    ddl = next(c for c in result.changes if c.kind == "add_table").ddl
    assert "'100%'" in ddl and "%%" not in ddl
