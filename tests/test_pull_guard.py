"""PullGuard 决策闸门 + pull 超时配置（docker pull 继续/中断，2026-10-14）。"""

import threading
import time
from pathlib import Path

from callfans.config import Config
from callfans.core.updaters.docker_cli import PullGuard


def test_request_decision_timeout_defaults_to_continue():
    g = PullGuard(confirm_seconds=600)
    assert g.request_decision(grace=0.05) == "continue"


def test_decide_abort_wakes_request():
    g = PullGuard(confirm_seconds=600)
    result: list[str] = []

    def wait():
        result.append(g.request_decision(grace=5.0))

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.05)  # 等 request_decision 进入阻塞
    g.decide("abort")
    t.join(timeout=2)
    assert result == ["abort"]


def test_request_decision_discards_stale_decision():
    """上一句条目的残留点击不误判到当前条目：request 入口清空旧决策。"""
    g = PullGuard(confirm_seconds=600)
    g.decide("abort")  # 残留（上一个 pull 的点击）
    assert g.request_decision(grace=0.05) == "continue"  # 已清空 → 超时兜底继续


def test_config_pull_timeouts_from_env(monkeypatch):
    monkeypatch.setenv("DOCKER_PULL_CONFIRM_SECONDS", "900")
    monkeypatch.setenv("DOCKER_PULL_TIMEOUT_SECONDS", "7200")
    cfg = Config.from_env(Path("/nonexistent/callfans.env"))
    assert cfg.docker_pull_confirm_seconds == 900
    assert cfg.docker_pull_timeout_seconds == 7200


def test_config_pull_timeout_defaults():
    cfg = Config(harbor_api_url="u", harbor_project="p", harbor_username="a", harbor_password="b")
    assert cfg.docker_pull_confirm_seconds == 600
    assert cfg.docker_pull_timeout_seconds == 3600
