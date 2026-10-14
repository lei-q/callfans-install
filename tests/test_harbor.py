"""harbor.py：/api/v2.0 路径编码与容错（MockTransport，无真实 Harbor 依赖）。

2026-10-14 实测（47.87.66.98）：仓库名含 '/' 时，单次编码 %2F 会被 Harbor 前置
nginx 解码回 '/' 导致 404（route 不匹配）；必须双重编码 %252F。
首例：callfans/xuxueli/xxl-job-admin（第三方镜像推入项目，名字带斜杠）。
"""

import httpx
import pytest

from callfans.core.harbor import HarborClient


def make_client(handler) -> HarborClient:
    hc = HarborClient("http://harbor.example.com", "callfans", "u", "p")
    hc._http = httpx.Client(
        base_url=hc.base_url, auth=("u", "p"), timeout=30, trust_env=False,
        transport=httpx.MockTransport(handler),
    )
    return hc


def test_repo_tags_double_encodes_slash_in_repo_name():
    """仓库名含 '/' 时路径必须双重编码（%252F），否则 Harbor 404。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=[{
            "digest": "sha256:x", "push_time": "2026-10-01T00:00:00.000Z",
            "annotations": {}, "tags": [{"name": "v1", "push_time": "2026-10-01T00:00:00.000Z"}],
        }])

    hc = make_client(handler)
    tags = hc.repo_tags("callfans/xuxueli/xxl-job-admin")

    assert len(seen) == 1
    path, _, query = seen[0].partition("?")
    assert path == "/api/v2.0/projects/callfans/repositories/xuxueli%252Fxxl-job-admin/artifacts"
    assert "with_tag=true" in query
    assert [t.tag for t in tags] == ["v1"]


def test_repo_tags_plain_repo_name_unchanged():
    """不含 '/' 的仓库名不应被无谓编码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"xuxueli" not in request.url.raw_path or b"%25" not in request.url.raw_path
        return httpx.Response(200, json=[])

    hc = make_client(handler)
    assert hc.repo_tags("callfans/api") == []


def test_repo_tags_404_raises_harbor_error():
    """真 404（仓库不存在）仍按 HarborError 上抛——由 checker 层容错跳过。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND", "message": "x"}]})

    hc = make_client(handler)
    with pytest.raises(Exception):
        hc.repo_tags("callfans/gone")
