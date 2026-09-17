"""CLI 入口：callfans serve/check/pending/status。

优先连接运行中的服务（读 runtime.json）；服务不在时 check/pending 退化为本地直连执行。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx
import typer

from .config import Config, ConfigError
from .core.checker import Checker
from .core.harbor import HarborClient
from .core.local import StateStore
from .paths import runtime_file, state_file
from .service.runtime import read_runtime

app = typer.Typer(no_args_is_help=True, help="callfans 镜像更新器")

EnvOpt = typer.Option(Path(".env"), "--env", help=".env 配置路径")


def _service_client() -> httpx.Client | None:
    """服务可达则返回带鉴权的客户端，否则 None。"""
    rt = read_runtime(runtime_file())
    if not rt or not rt.get("port") or not rt.get("token"):
        return None
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{rt['port']}",
        headers={"Authorization": f"Bearer {rt['token']}"},
        timeout=5,
    )
    try:
        client.get("/api/v1/status").raise_for_status()
        return client
    except Exception:
        client.close()
        return None


def _print_plan(plan: dict) -> None:
    pending = plan.get("pending") or []
    typer.echo(f"检查时间: {plan.get('checked_at') or '-'}")
    if not pending:
        typer.secho("✅ 无待更新项", fg=typer.colors.GREEN)
        return
    typer.secho(f"发现 {len(pending)} 项待更新:", fg=typer.colors.YELLOW)
    for p in pending:
        new = p["new"] if isinstance(p["new"], str) else " → ".join(p["new"])
        line = f"  [{p['type']}] {p['name']}: {p.get('old') or '未安装'} → {new}"
        if p.get("alias"):
            line += f"  (alias={p['alias']})"
        typer.echo(line)
        cl = p.get("changelog")
        if isinstance(cl, dict):
            for tag, text in cl.items():
                typer.echo(f"      {tag}: {text}")
        elif cl:
            typer.echo(f"      changelog: {cl}")


@app.command()
def serve(
    env: Path = EnvOpt,
    port: Optional[int] = typer.Option(None, "--port", help="固定 IPC 端口（默认随机）"),
) -> None:
    """启动后台服务（常驻，含定时检查）。"""
    from .service.app import run_service

    try:
        run_service(env, port)
    except KeyboardInterrupt:
        pass


@app.command()
def check(env: Path = EnvOpt) -> None:
    """立即检查一次（优先走运行中的服务，否则本地直连）。"""
    client = _service_client()
    if client is not None:
        try:
            resp = client.post("/api/v1/check", timeout=300)
            resp.raise_for_status()
            _print_plan(resp.json())
            return
        except httpx.HTTPError as e:
            typer.secho(f"服务请求失败，转本地直连: {e}", fg=typer.colors.YELLOW)
        finally:
            client.close()
    logging.basicConfig(level=logging.INFO)
    try:
        cfg = Config.from_env(env)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        checker = Checker(cfg, HarborClient(
            cfg.harbor_api_url, cfg.harbor_project, cfg.harbor_username, cfg.harbor_password
        ))
        _print_plan(checker.run().to_dict())
    except Exception as e:
        typer.secho(f"检查失败: {type(e).__name__}: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command()
def pending(env: Path = EnvOpt) -> None:
    """查看最近一次检查结果（不发起新检查）。"""
    client = _service_client()
    if client is not None:
        try:
            resp = client.get("/api/v1/pending", timeout=10)
            resp.raise_for_status()
            _print_plan(resp.json())
            return
        except httpx.HTTPError as e:
            typer.secho(f"服务请求失败，读本地 state: {e}", fg=typer.colors.YELLOW)
        finally:
            client.close()
    st = StateStore(state_file())
    _print_plan(st.last_plan or {"checked_at": st.checked_at, "pending": []})


@app.command()
def update(env: Path = EnvOpt) -> None:
    """立即更新全部待更新项（SQL → server → 前端；优先走运行中的服务）。"""
    client = _service_client()
    if client is not None:
        try:
            resp = client.post("/api/v1/update", timeout=3600)
            resp.raise_for_status()
            if not _print_report(resp.json()):
                raise typer.Exit(1)
            return
        except httpx.HTTPError as e:
            typer.secho(f"服务请求失败，转本地直连: {e}", fg=typer.colors.YELLOW)
        finally:
            client.close()
    logging.basicConfig(level=logging.INFO)
    try:
        cfg = Config.from_env(env)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    from .core.updaters.runner import UpdateRunner

    try:
        checker = Checker(cfg, HarborClient(
            cfg.harbor_api_url, cfg.harbor_project, cfg.harbor_username, cfg.harbor_password
        ))
        plan = checker.run()
        runner = UpdateRunner(cfg, state=getattr(checker, "state", None))
        if not _print_report(runner.run(plan)):
            raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"更新失败: {type(e).__name__}: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


def _print_report(report: dict) -> bool:
    """打印更新报告；全部成功返回 True。"""
    if report.get("preflight_error"):
        typer.secho(f"前置检查未通过，未执行任何更新:\n  {report['preflight_error']}", fg=typer.colors.RED)
        return False
    items = report.get("items") or []
    if not items:
        typer.echo("没有待更新项")
        return True
    color = {"success": typer.colors.GREEN, "failed": typer.colors.RED, "rolled_back": typer.colors.YELLOW}
    for r in items:
        new = r.get("new") if isinstance(r.get("new"), str) else " → ".join(r.get("new") or [])
        typer.secho(f"[{r.get('result')}] {r.get('type')}/{r.get('name')}: {r.get('old') or '未安装'} → {new}",
                    fg=color.get(r.get("result"), typer.colors.WHITE))
        if r.get("error"):
            typer.echo(f"    {r['error']}")
    typer.echo(f"汇总: {report.get('summary')}")
    return all(r.get("result") == "success" for r in items)


@app.command()
def status(env: Path = EnvOpt) -> None:
    """查看服务状态。"""
    client = _service_client()
    if client is None:
        typer.secho("服务未运行（callfans serve 可启动）", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    try:
        resp = client.get("/api/v1/status", timeout=10)
        resp.raise_for_status()
        for k, v in resp.json().items():
            typer.echo(f"{k}: {v}")
    finally:
        client.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
