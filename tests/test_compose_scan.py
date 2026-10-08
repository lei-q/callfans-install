"""compose 清单扫描：镜像提取、仓库归一化、存在性判定。"""

from callfans.core.compose_scan import local_has_image, scan_compose_images
from callfans.core.local import DockerImage

COMPOSE = """\
services:
  callfans-db:
    image: mysql:8.0.24
    container_name: callfans-db
  admin:
    image: ${HARBOR_REGISTRY}/callfans/callfans-admin:${CALLFANS_ADMIN_TAG}
    container_name: callfans-admin
  redis:
    image: redis:5.0.14
  noimage:
    container_name: dummy
"""


def test_scan_extracts_all_images(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(COMPOSE, encoding="utf-8")
    images = scan_compose_images(f, "callfans")
    by_service = {i.service: i for i in images}
    assert set(by_service) == {"callfans-db", "admin", "redis"}  # noimage 已跳过
    assert by_service["callfans-db"].repo == "mysql"
    assert by_service["callfans-db"].is_harbor is False
    assert by_service["admin"].is_harbor is True
    assert by_service["admin"].harbor_repo == "callfans/callfans-admin"


def test_scan_missing_file(tmp_path):
    assert scan_compose_images(tmp_path / "nope.yml", "callfans") == []


class TestLocalHas:
    IMAGES = [
        DockerImage("mysql", "8.0.24", "sha256:a"),
        DockerImage("47.87.66.98/callfans/callfans-admin", "20260918-x", "sha256:b"),
        DockerImage("redis", "5.0.14", "sha256:c"),
    ]

    def test_plain_repo_exact_tag(self):
        assert local_has_image(self.IMAGES, "mysql", "8.0.24")
        assert not local_has_image(self.IMAGES, "mysql", "8.0.30")  # tag 不符

    def test_repo_only(self):
        assert local_has_image(self.IMAGES, "redis")

    def test_host_prefix_tolerated(self):
        # compose 里带 registry 前缀，本地 docker 可能不带（或反之）
        assert local_has_image(self.IMAGES, "callfans/callfans-admin")
        assert local_has_image(self.IMAGES, "callfans/callfans-admin", "20260918-x")

    def test_absent(self):
        assert not local_has_image(self.IMAGES, "nginx", "1.18.0")
