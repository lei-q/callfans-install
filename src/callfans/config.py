"""配置：.env 加载与校验（python-dotenv）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# 默认值（.env 可不填，代码兜底；地址统一指向 47.87.66.98）
_DEFAULT_HARBOR_API_URL = "http://47.87.66.98/"
_DEFAULT_HARBOR_PROJECT = "callfans"
_DEFAULT_HARBOR_USER = "admin"
_DEFAULT_HARBOR_PASSWORD = "Callfans@123"
_DEFAULT_MYSQL_HOST = "127.0.0.1"
_DEFAULT_MYSQL_USER = "root"
_DEFAULT_MYSQL_PASSWORD = "123456"


class ConfigError(Exception):
    """配置缺失或非法。"""


def load_env_loose(env_path: Path) -> None:
    """容错加载 .env：UTF-8 优先 → GBK（Windows 记事本 ANSI / 安装器写入）→ latin-1 兜底。

    v0.7.4 实测：安装器 Inno 的 SaveStringsToFile 按 ANSI(GBK) 写回 .env，
    中文注释变 GBK 字节，python-dotenv 按 UTF-8 读直接崩（UnicodeDecodeError）。
    latin-1 永不失败（乱码但不崩），服务可用 ASCII 键值正常。
    """
    from dotenv import load_dotenv

    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            load_dotenv(env_path, encoding=enc)
            return
        except UnicodeDecodeError:
            continue
        except Exception:
            return  # 其他错误（文件锁等）跳过，走默认值


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
    app_root: Path | None = None              # 更新程序根目录（安装器写入）
    deploy_root: Path | None = None           # 平台部署根目录（安装器写入）
    compose_file: Path | None = None
    frontend_output_dir: Path | None = None
    mysql_host: str | None = None             # M2 使用
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = None
    mysql_database: str | None = None
    health_wait_seconds: int = 60
    docker_stop_timeout: int = 15
    docker_pull_confirm_seconds: int = 600   # 单个镜像 pull 超过该时长弹"继续/中断"确认
    docker_pull_timeout_seconds: int = 3600  # 单个镜像 pull 硬上限（2GB 镜像实测 >30min）
    bind_port: int | None = None

    @classmethod
    def from_env(cls, env_path: Path | str = ".env") -> "Config":
        env_path = Path(env_path)
        if env_path.exists():
            load_env_loose(env_path)

        cfg = cls(
            harbor_api_url=_get("HARBOR_API_URL", _DEFAULT_HARBOR_API_URL).rstrip("/"),
            harbor_project=_get("HARBOR_PROJECT", _DEFAULT_HARBOR_PROJECT),
            harbor_username=_get("HARBOR_USERNAME", _DEFAULT_HARBOR_USER),
            harbor_password=_get("HARBOR_PASSWORD", _DEFAULT_HARBOR_PASSWORD),
            check_interval_hours=float(_get("CHECK_INTERVAL_HOURS", "6")),
            tag_exclude=[t.strip() for t in _get("TAG_EXCLUDE", "latest,dev").split(",") if t.strip()],
            app_root=Path(p) if (p := _get("APP_ROOT")) else None,
            deploy_root=Path(p) if (p := _get("DEPLOY_ROOT")) else None,
            compose_file=Path(p) if (p := _get("COMPOSE_FILE")) else None,
            frontend_output_dir=Path(p) if (p := _get("FRONTEND_OUTPUT_DIR")) else None,
            mysql_host=_get("MYSQL_HOST", _DEFAULT_MYSQL_HOST),
            mysql_port=int(_get("MYSQL_PORT", "3306")),
            mysql_user=_get("MYSQL_USER", _DEFAULT_MYSQL_USER),
            mysql_password=_get("MYSQL_PASSWORD", _DEFAULT_MYSQL_PASSWORD),
            mysql_database=_get("MYSQL_DATABASE"),
            health_wait_seconds=int(_get("HEALTH_WAIT_SECONDS", "60")),
            docker_stop_timeout=int(_get("DOCKER_STOP_TIMEOUT", "15")),
            docker_pull_confirm_seconds=int(_get("DOCKER_PULL_CONFIRM_SECONDS", "600")),
            docker_pull_timeout_seconds=int(_get("DOCKER_PULL_TIMEOUT_SECONDS", "3600")),
            bind_port=int(p) if (p := _get("BIND_PORT")) else None,
        )
        if cfg.check_interval_hours <= 0:
            raise ConfigError("CHECK_INTERVAL_HOURS 必须大于 0")
        return cfg
