"""compose 相关的轻量解析（不引入 YAML 依赖）：

- 从 compose.yml 原文里定位某 repo 的 `image:` 模板，提取 tag 变量（Q5：`${VAR}` 注入）
- compose 同目录 .env 的读写（保留注释与顺序；update 持久写，回滚写回）
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_IMAGE_LINE_RE = re.compile(r"^\s*-?\s*image:\s*(\S+)\s*$", re.M)
_VAR_RE = re.compile(r"\$\{?(\w+)\}?")


class ComposeError(RuntimeError):
    pass


def strip_tag(ref: str) -> str:
    """去掉 tag/digest，保留 repo（注意 host:port 形式）。"""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref = ref.rsplit(":", 1)[0]
    return ref


def find_image_template(text: str, repo_full: str, project: str) -> str | None:
    """在 compose 原文中找到引用 repo_full 的 image 模板（含 ${VAR}）。"""
    from ..local import harbor_repo_of

    short = repo_full.split("/", 1)[1]
    for m in _IMAGE_LINE_RE.finditer(text):
        template = m.group(1)
        plain = _VAR_RE.sub("", template)  # 去变量后比对 repo
        base = strip_tag(plain)
        if harbor_repo_of(base, project) == repo_full or base.endswith(f"/{short}") or base == short:
            return template
    return None


def extract_tag_var(template: str) -> str | None:
    """模板里最后一个变量即 tag 变量（如 harbor/callfans/api:${API_TAG}）。"""
    vars_ = _VAR_RE.findall(template)
    return vars_[-1] if vars_ else None


def iter_image_templates(text: str) -> list[str]:
    """compose 原文中全部 image 模板（含无变量的）。"""
    return [m.group(1) for m in _IMAGE_LINE_RE.finditer(text)]


def strip_vars(template: str) -> str:
    """去掉模板中全部变量后剩余部分（用于识别 repo）。"""
    return _VAR_RE.sub("", template)


def render_ref(template: str, tag: str, var: str | None = None, env: dict | None = None) -> str:
    """把 tag 变量替换为具体 tag；其余变量（如 ${HARBOR_REGISTRY}）用 env 值解析。

    env 中无值的变量保留原样（调用方需检测残留并报错）。
    """
    var = var or extract_tag_var(template)
    if var is not None:
        template = re.sub(rf"\$\{{?{var}\}}?", tag, template)
    if env:

        def _sub(m):
            name = m.group(1)
            return env[name] if env.get(name) else m.group(0)

        template = re.sub(r"\$\{?(\w+)\}?", _sub, template)
    return template


def has_unresolved_vars(ref: str) -> bool:
    return bool(re.search(r"\$\{?\w+\}?", ref))


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def write_env(path: Path, updates: dict[str, str | None]) -> None:
    """逐行重写：替换/追加/删除（value=None 删除）指定变量，保留其他行。原子写。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            val = remaining.pop(key)
            if val is not None:
                out.append(f"{key}={val}")
            # val 为 None → 删除该行
        else:
            out.append(line)
    for key, val in remaining.items():
        if val is not None:
            out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
