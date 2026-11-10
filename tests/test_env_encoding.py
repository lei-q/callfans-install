""".env 编码容错：GBK/ANSI 文件不再崩溃（v0.7.4 实测）。"""

import pytest

from callfans.config import Config, load_env_loose


def test_gbk_env_file_loads(tmp_path, monkeypatch):
    """Windows 记事本 ANSI(GBK) 保存的 .env：UTF-8 读会崩，容错加载成功。"""
    env = tmp_path / ".env"
    env.write_bytes("# 中文注释\nHARBOR_API_URL=http://47.87.66.98/\n".encode("gbk"))
    monkeypatch.delenv("HARBOR_API_URL", raising=False)
    cfg = Config.from_env(env)
    assert cfg.harbor_api_url == "http://47.87.66.98"
    monkeypatch.delenv("HARBOR_API_URL", raising=False)


def test_utf8_env_still_works(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# UTF-8 注释\nHARBOR_API_URL=http://utf8host/\n", encoding="utf-8")
    monkeypatch.delenv("HARBOR_API_URL", raising=False)
    cfg = Config.from_env(env)
    assert cfg.harbor_api_url == "http://utf8host"
    monkeypatch.delenv("HARBOR_API_URL", raising=False)


def test_latin1_never_crashes(tmp_path):
    """任意乱码字节（latin-1 兜底）：不抛异常。"""
    env = tmp_path / ".env"
    env.write_bytes(b"\xff\xfe\xfa# \xd2\xd2\xcf\xc2 \xd7\xa2\xca\xcd\nKEY=ok\n")
    load_env_loose(env)  # 不应抛 UnicodeDecodeError
    import os

    os.environ.pop("KEY", None)


def test_missing_file_no_op(tmp_path):
    load_env_loose(tmp_path / "nope.env")


def test_env_file_overrides_ambient_env(tmp_path, monkeypatch):
    """应用 .env 是唯一事实源：系统/用户环境里残留的同名变量不得压过 .env。

    2026-11-10 现场 401 排查：dotenv 默认 override=False，旧现场环境变量
    HARBOR_PASSWORD=Harbor@12345（172 仓库密码）静默盖掉了 47 的 .env 值。
    """
    env = tmp_path / ".env"
    env.write_text("HARBOR_USERNAME=admin\nHARBOR_PASSWORD=Callfans@123\n", encoding="utf-8")
    monkeypatch.setenv("HARBOR_PASSWORD", "Harbor@12345")
    cfg = Config.from_env(env)
    assert cfg.harbor_password == "Callfans@123"
