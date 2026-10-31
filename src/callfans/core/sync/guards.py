"""自动执行护栏（§1.2，2026-09-20 确认）。

全自动 + 破坏性（Q2+Q5）下护栏是唯一安全网：
- G1 备份强制：无评估逻辑，执行器必须先走 BackupManager（不可关）
- G2 漂移规模 / G3 云库完整性 / G4 删除行数：fatal，触发即中止整次 apply 并告警
  （例外：G3 个别清单表过期、G2 首次全量 → non-fatal 告警/跳过，2026-10-27 确认）
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


# 清单里指向历史备份表的行（云端早已删除）自动忽略，不参与 G3/G2（2026-10-27 确认）
STALE_BACKUP_PREFIX = "_cf_bak_"

# G3：缺失比例达到该值视为"大比例缺失"（含整库缺失）→ 仍 fatal；
# 低于该值视为个别清单行失效 → 跳过这些表并告警（non-fatal）
G3_MISSING_RATIO = 0.5


def is_stale_backup_table(name: str) -> bool:
    return name.startswith(STALE_BACKUP_PREFIX)


def check_cloud_integrity(cloud_tables_present: set[str],
                          manifest: list[TableRule],
                          label: str = "") -> list[GuardViolation]:
    """G3：清单表在云库缺失。

    整库/大比例缺失（≥ G3_MISSING_RATIO）→ fatal（疑似云库未就绪/被清空/连错库）；
    个别表缺失 → non-fatal（疑似清单行过期，计划里已自动跳过这些表）。
    `_cf_bak_*` 行不参与判定。
    """
    expected = {r.name for r in manifest if not is_stale_backup_table(r.name)}
    if not expected:
        return []
    missing = sorted(expected - cloud_tables_present)
    if not missing:
        return []
    where = f"云库 {label} " if label else "云库"
    ratio = len(missing) / len(expected)
    shown = f"{', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}"
    if ratio >= G3_MISSING_RATIO:
        return [GuardViolation(
            "G3",
            f"{where}缺少清单中的 {len(missing)}/{len(expected)} 张表（{ratio:.0%}）: "
            f"{shown}（疑似云库未就绪/被清空/连错库）",
            tables=missing,
        )]
    return [GuardViolation(
        "G3",
        f"{where}缺少清单中的 {len(missing)}/{len(expected)} 张表（{ratio:.0%}）: "
        f"{shown}（疑似清单行过期，已跳过这些表）",
        fatal=False, tables=missing,
    )]


def check_plan_scale(statement_count: int, affected_tables: set[str],
                     manifest: list[TableRule], policy: SyncPolicy,
                     first_sync: bool = False) -> list[GuardViolation]:
    """G2：漂移规模超阈值 → 疑似云库被大改，中止。

    比例闸门仅在清单 ≥ 10 张表时生效：小清单（如新客户 2-3 张表）比例无
    统计意义，由语句数上限 + G3/G4/G5 兜底（2026-09-20 实测修订）。

    first_sync：B 库无任何清单目标表（首次全量）→ 超限降级为非致命告警
    （2026-10-27 确认：首次同步天然是大规模纯新增，不应拦截）。
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
    if first_sync:
        for v in violations:
            v.fatal = False
            v.message += "（首次全量同步，B 库无对应表，自动放行）"
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
