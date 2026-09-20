"""配置：.env 加载与校验（python-dotenv）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_REQUIRED = ("HARBOR_API_URL", "HARBOR_PROJECT")

# 账号类默认值（2026-09-21 决策：.env 可不填，代码兜底）
_DEFAULT_HARBOR_USER = "admin"
_DEFAULT_HARBOR_PASSWORD = "Callfans@123"
_DEFAULT_MYSQL_HOST = "127.0.0.1"
_DEFAULT_MYSQL_USER = "root"
_DEFAULT_MYSQL_PASSWORD = "callfans@123"


class ConfigError(Exception):
    """配置缺失或非法。"""


def _get(key: str, default: str | None = None) -> str | None:
    import os

    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    return val.strip()


@dataclass
class Config:
    harbor_api_url: str
    harbor_project: str
    harbor_username: str
    harbor_password: str
    check_interval_hours: float = 6.0
    tag_exclude: list[str] = field(default_factory=lambda: ["latest", "dev"])
    compose_file: Path | None = None          # M2 使用
    frontend_output_dir: Path | None = None   # M2 使用
    mysql_host: str | None = None             # M2 使用
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = None
    mysql_database: str | None = None
    health_wait_seconds: int = 60
    docker_stop_timeout: int = 15
    bind_port: int | None = None

    @classmethod
    def from_env(cls, env_path: Path | str = ".env") -> "Config":
        env_path = Path(env_path)
        if env_path.exists():
            load_dotenv(env_path)
        import os

        missing = [k for k in _REQUIRED if not (os.environ.get(k) or "").strip()]
        if missing:
            raise ConfigError(
                f"缺少配置项: {', '.join(missing)}（读取文件: {env_path.resolve()}，"
                f"参考 .env.example）"
            )
        cfg = cls(
            harbor_api_url=_get("HARBOR_API_URL").rstrip("/"),
            harbor_project=_get("HARBOR_PROJECT"),
            harbor_username=_get("HARBOR_USERNAME", _DEFAULT_HARBOR_USER),
            harbor_password=_get("HARBOR_PASSWORD", _DEFAULT_HARBOR_PASSWORD),
            check_interval_hours=float(_get("CHECK_INTERVAL_HOURS", "6")),
            tag_exclude=[t.strip() for t in _get("TAG_EXCLUDE", "latest,dev").split(",") if t.strip()],
            compose_file=Path(p) if (p := _get("COMPOSE_FILE")) else None,
            frontend_output_dir=Path(p) if (p := _get("FRONTEND_OUTPUT_DIR")) else None,
            mysql_host=_get("MYSQL_HOST", _DEFAULT_MYSQL_HOST),
            mysql_port=int(_get("MYSQL_PORT", "3306")),
            mysql_user=_get("MYSQL_USER", _DEFAULT_MYSQL_USER),
            mysql_password=_get("MYSQL_PASSWORD", _DEFAULT_MYSQL_PASSWORD),
            mysql_database=_get("MYSQL_DATABASE"),
            health_wait_seconds=int(_get("HEALTH_WAIT_SECONDS", "60")),
            docker_stop_timeout=int(_get("DOCKER_STOP_TIMEOUT", "15")),
            bind_port=int(p) if (p := _get("BIND_PORT")) else None,
        )
        if cfg.check_interval_hours <= 0:
            raise ConfigError("CHECK_INTERVAL_HOURS 必须大于 0")
        return cfg
