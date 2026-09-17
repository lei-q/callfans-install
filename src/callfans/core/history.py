"""更新历史记录（update_history.jsonl）。

记录每次更新中每个制品的结果：ts / name / type / old / new /
result(success|failed|rolled_back) / error。M2 更新器写入，此处先落基础件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def append_record(path: Path, record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
