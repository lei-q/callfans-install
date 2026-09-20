"""自动执行护栏（§1.2，2026-09-20 确认）。

全自动 + 破坏性（Q2+Q5）下护栏是唯一安全网：
- G1 备份强制：无评估逻辑，执行器必须先走 BackupManager（不可关）
- G2 漂移规模 / G3 云库完整性 / G4 删除行数：fatal，触发即中止整次 apply 并告警
- G5 云库空表：non-fatal，跳过该表的数据同步并告警
触发护栏后人工排查，可 --force 越过（G5 除外，行为为跳过）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import SyncPolicy, TableRule


@dataclass
class GuardViolation:
    guard: str          # G2/G3/G4/G5
    message: str
    fatal: bool = True
    tables: list[str] = field(default_factory=list)


def check_cloud_integrity(cloud_tables_present: set[str],
                          manifest: list[TableRule]) -> list[GuardViolation]:
    """G3：云库可见配置表数 < 清单数 → 疑似云库被清空/同步半截，中止。"""
    expected = {r.name for r in manifest}
    missing = sorted(expected - cloud_tables_present)
    if missing:
        return [GuardViolation(
            "G3",
            f"云库缺少清单中的 {len(missing)} 张表: {', '.join(missing[:10])}"
            f"{'…' if len(missing) > 10 else ''}（疑似云库未就绪/被清空/连错库）",
            tables=missing,
        )]
    return []


def check_plan_scale(statement_count: int, affected_tables: set[str],
                     manifest: list[TableRule], policy: SyncPolicy) -> list[GuardViolation]:
    """G2：漂移规模超阈值 → 疑似云库被大改，中止。

    比例闸门仅在清单 ≥ 10 张表时生效：小清单（如新客户 2-3 张表）比例无
    统计意义，由语句数上限 + G3/G4/G5 兜底（2026-09-20 实测修订）。
    """
    violations: list[GuardViolation] = []
    if statement_count > policy.max_statements:
        violations.append(GuardViolation(
            "G2", f"变更语句数 {statement_count} 超过上限 {policy.max_statements}",
            tables=sorted(affected_tables),
        ))
    total = len(manifest)
    if total >= 10:
        ratio = len(affected_tables) / total
        if ratio > policy.max_table_ratio:
            violations.append(GuardViolation(
                "G2",
                f"涉及表 {len(affected_tables)}/{total}（{ratio:.0%}）超过比例上限 "
                f"{policy.max_table_ratio:.0%}",
                tables=sorted(affected_tables),
            ))
    return violations


def check_delete_rows(table: str, delete_count: int, policy: SyncPolicy) -> list[GuardViolation]:
    """G4：单表 DELETE 行数超上限 → 疑似云端误清数据，中止。"""
    if delete_count > policy.max_delete_rows:
        return [GuardViolation(
            "G4", f"表 {table} 删除行数 {delete_count} 超过上限 {policy.max_delete_rows}",
            tables=[table],
        )]
    return []


def check_empty_cloud_table(table: str, cloud_rows: int, local_rows: int) -> list[GuardViolation]:
    """G5：云库表为空而 B 库非空 → 跳过该表数据同步并告警（non-fatal）。"""
    if cloud_rows == 0 and local_rows > 0:
        return [GuardViolation(
            "G5",
            f"云库表 {table} 为空而 B 库有 {local_rows} 行，疑似云库数据缺失，已跳过该表数据同步",
            fatal=False, tables=[table],
        )]
    return []


def summarize(violations: list[GuardViolation]) -> str:
    fatals = [v for v in violations if v.fatal]
    warns = [v for v in violations if not v.fatal]
    parts = [f"[{v.guard}] {v.message}" for v in fatals]
    parts += [f"[{v.guard}]（跳过）{v.message}" for v in warns]
    return "\n".join(parts)
