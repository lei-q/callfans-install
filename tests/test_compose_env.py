"""compose_env：image 模板解析、tag 变量、.env 读写。"""

from callfans.core.updaters.compose_env import (
    extract_tag_var,
    find_image_template,
    read_env,
    render_ref,
    strip_tag,
    write_env,
)

COMPOSE = """\
services:
  api:
    image: harbor.example.com/callfans/api:${API_TAG}
    ports:
      - "8080:8080"
  worker:
    image: harbor.example.com/callfans/worker:20260901102030-abc1234
  web:
    image: harbor.example.com/callfans/web:${WEB_TAG}
"""


class TestStripTag:
    def test_tag(self):
        assert strip_tag("host/callfans/api:v1") == "host/callfans/api"

    def test_host_port_no_tag(self):
        assert strip_tag("host:5000/callfans/api") == "host:5000/callfans/api"

    def test_host_port_with_tag(self):
        assert strip_tag("host:5000/callfans/api:v1") == "host:5000/callfans/api"

    def test_digest(self):
        assert strip_tag("host/callfans/api@sha256:xxx") == "host/callfans/api"


class TestTemplate:
    def test_find_with_var(self):
        t = find_image_template(COMPOSE, "callfans/api", "callfans")
        assert t == "harbor.example.com/callfans/api:${API_TAG}"
        assert extract_tag_var(t) == "API_TAG"

    def test_find_fixed_tag_no_var(self):
        t = find_image_template(COMPOSE, "callfans/worker", "callfans")
        assert t is not None
        assert extract_tag_var(t) is None

    def test_not_found(self):
        assert find_image_template(COMPOSE, "callfans/other", "callfans") is None

    def test_render(self):
        assert render_ref("harbor.example.com/callfans/api:${API_TAG}", "T1") == \
            "harbor.example.com/callfans/api:T1"
        assert render_ref("harbor.example.com/callfans/api:$API_TAG", "T1") == \
            "harbor.example.com/callfans/api:T1"

    def test_render_resolves_other_vars_from_env(self):
        ref = render_ref(
            "${HARBOR_REGISTRY}/callfans/api:${API_TAG}", "T1", "API_TAG",
            env={"HARBOR_REGISTRY": "172.25.1.220"},
        )
        assert ref == "172.25.1.220/callfans/api:T1"

    def test_render_leaves_unknown_var(self):
        from callfans.core.updaters.compose_env import has_unresolved_vars

        ref = render_ref("${HARBOR_REGISTRY}/callfans/api:${API_TAG}", "T1", "API_TAG", env={})
        assert ref == "${HARBOR_REGISTRY}/callfans/api:T1"
        assert has_unresolved_vars(ref)
        assert not has_unresolved_vars("host/callfans/api:T1")


class TestEnvFile:
    def test_read(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# 注释\nAPI_TAG=old\nOTHER = keep me\n", encoding="utf-8")
        env = read_env(p)
        assert env == {"API_TAG": "old", "OTHER": "keep me"}

    def test_read_gbk_encoded(self, tmp_path):
        """Windows 记事本 ANSI/GBK 保存（含中文注释）也能读。"""
        p = tmp_path / ".env"
        p.write_bytes("# 部署配置\nAPI_TAG=old\n".encode("gbk"))
        assert read_env(p) == {"API_TAG": "old"}
        # write_env 保留注释（正确解码）并统一为 UTF-8
        write_env(p, {"API_TAG": "new"})
        content = p.read_text(encoding="utf-8")
        assert "部署配置" in content
        assert "API_TAG=new" in content

    def test_read_bom(self, tmp_path):
        p = tmp_path / ".env"
        p.write_bytes("K=V\n".encode("utf-8-sig"))
        assert read_env(p) == {"K": "V"}

    def test_write_preserves_comments(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# 注释\nAPI_TAG=old\nOTHER=1\n", encoding="utf-8")
        write_env(p, {"API_TAG": "new"})
        content = p.read_text(encoding="utf-8")
        assert "# 注释" in content
        assert "API_TAG=new" in content
        assert "OTHER=1" in content
        assert "API_TAG=old" not in content

    def test_write_append_and_delete(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("A=1\n", encoding="utf-8")
        write_env(p, {"B": "2", "A": None})
        assert p.read_text(encoding="utf-8").splitlines() == ["B=2"]

    def test_write_creates(self, tmp_path):
        p = tmp_path / "sub" / ".env"
        write_env(p, {"X": "1"})
        assert read_env(p) == {"X": "1"}
