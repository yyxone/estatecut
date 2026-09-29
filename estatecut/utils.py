"""Small shared utilities."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def natural_key(value: str) -> list[Any]:
    parts = re.split(r"(\d+)", value.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def stable_id(prefix: str, parts: Iterable[object]) -> str:
    raw = "_".join(str(p) for p in parts)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw).strip("_")
    return f"{prefix}_{safe[:80]}"


def require_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Path outside workspace is not allowed: {resolved}")
    return resolved


def require_outside(path: Path, root: Path) -> Path:
    """守源媒体（AGENTS 硬约束 1）：path 不能等于 root 或落在 root 内。

    防止输出目录指向源媒体目录树后，拼接 / 抽轨产物污染源文件。
    """
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        raise ValueError(f"Path must stay outside source media dir: {resolved} is inside {root_resolved}")
    return resolved


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0
