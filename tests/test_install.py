"""service install（Linux 用户级）单元件渲染 + runtime 路径探测。"""

from pathlib import Path

from callfans.paths import SYSTEM_RUNTIME_FILE, runtime_candidates, runtime_file
from callfans.service.install import render_ui_autostart, render_user_unit


class TestUserUnit:
    def test_render_contains_execstart_and_target(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("HARBOR_API_URL=x\n", encoding="utf-8")
        text = render_user_unit(env)
        assert "ExecStart=" in text
        assert str(env) in text
        assert "WantedBy=default.target" in text
        assert "Restart=on-failure" in text


class TestUiAutostart:
    def test_render(self):
        text = render_ui_autostart()
        assert text.startswith("[Desktop Entry]")
        assert "Exec=" in text
        # 打包/PATH 场景是 callfans-ui 二进制；开发环境回退 python -m
        assert ("callfans-ui" in text) or ("callfans.ui.app" in text)


class TestRuntimePaths:
    def test_env_override_write_path(self, tmp_path, monkeypatch):
        target = tmp_path / "shared" / "runtime.json"
        monkeypatch.setenv("CALLFANS_RUNTIME_FILE", str(target))
        assert runtime_file() == target
        # 写入路径的父目录自动创建
        assert target.parent.exists()

    def test_candidates_order(self, tmp_path, monkeypatch):
        override = tmp_path / "r.json"
        monkeypatch.setenv("CALLFANS_RUNTIME_FILE", str(override))
        cands = runtime_candidates()
        assert cands[0] == override
        assert cands[-1] == SYSTEM_RUNTIME_FILE  # 系统级兜底（路径对象比较，跨平台）

    def test_candidates_default_without_env(self, monkeypatch):
        monkeypatch.delenv("CALLFANS_RUNTIME_FILE", raising=False)
        cands = runtime_candidates()
        assert len(cands) == 2
        assert cands[-1] == SYSTEM_RUNTIME_FILE
