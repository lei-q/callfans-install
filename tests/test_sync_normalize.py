"""归一化规则矩阵（M1 验收）：显示宽度/类型族/默认值/指纹稳定性。"""

from sqlalchemy import (
    BIGINT, Column, Integer, MetaData, String, Table, Text, VARCHAR,
)

from callfans.core.sync.normalize import (
    column_fingerprint, index_fingerprint, normalize_default, normalize_type,
    table_fingerprint,
)


class TestNormalizeType:
    def test_int_display_width_stripped(self):
        assert normalize_type("int(11)") == "int"
        assert normalize_type("INT(11)") == "int"
        assert normalize_type("bigint(20)") == "bigint"
        assert normalize_type("TINYINT(1)") == "tinyint"
        assert normalize_type("integer(10)") == "int"  # integer → int 族统一

    def test_unsigned_zerofill_significant(self):
        assert normalize_type("int(11) unsigned") == "int unsigned"
        assert normalize_type("int unsigned") == "int unsigned"  # 与上等价
        assert normalize_type("int(10) unsigned zerofill") == "int unsigned zerofill"
        assert normalize_type("int unsigned zerofill") != normalize_type("int unsigned")

    def test_precision_kept(self):
        assert normalize_type("varchar(32)") == "varchar(32)"
        assert normalize_type("decimal(10,2)") == "decimal(10,2)"
        assert normalize_type("datetime(6)") == "datetime(6)"
        assert normalize_type("text") == "text"

    def test_whitespace_folded(self):
        assert normalize_type("int ( 11 )  unsigned") == "int unsigned"


class TestNormalizeDefault:
    def test_null_semantics(self):
        assert normalize_default(None) is None
        assert normalize_default("NULL") is None
        assert normalize_default("") is None
        assert normalize_default("'abc'") == "abc"
        assert normalize_default('"abc"') == "abc"

    def test_numeric_normalized(self):
        assert normalize_default("0") == "0"
        assert normalize_default("1.0") == "1"
        assert normalize_default("2.50") == "2.5"


class TestFingerprints:
    def test_column_fingerprint_stable(self):
        from sqlalchemy.dialects.mysql import BIGINT as MYSQL_BIGINT

        md = MetaData()
        t1 = Table("t", md, Column("id", Integer, primary_key=True),
                   Column("name", VARCHAR(32), nullable=False))
        t2 = Table("t", MetaData(), Column("id", MYSQL_BIGINT(unsigned=True), primary_key=True),
                   Column("name", String(32), nullable=False))
        fp1 = {n: column_fingerprint(c) for n, c in t1.columns.items()}
        fp2 = {n: column_fingerprint(c) for n, c in t2.columns.items()}
        assert fp1["name"] == fp2["name"]       # varchar(32) == String(32)
        assert "unsigned" in fp2["id"][1]        # 类型差异可检出

    def test_table_fingerprint_structure(self):
        md = MetaData()
        t = Table("sys_config", md,
                  Column("id", Integer, primary_key=True),
                  Column("k", VARCHAR(64), nullable=False),
                  Column("v", Text))
        fp = table_fingerprint(t)
        assert fp["pk"] == ("id",)
        assert set(fp["columns"]) == {"id", "k", "v"}
        assert fp["indexes"] == []  # 无二级索引

    def test_index_fingerprint(self):
        md = MetaData()
        t = Table("t", md, Column("a", Integer), Column("b", Integer))
        idx = Table.Index.__getattr__ if False else None  # noqa
        from sqlalchemy import Index
        i = Index("idx_x", t.c.a, t.c.b, unique=True)
        cols, unique = index_fingerprint(i)
        assert cols == ("a", "b") and unique is True
