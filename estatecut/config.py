"""Config and facts loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schemas import ListingFacts
from .utils import read_json


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_facts(path: Path) -> ListingFacts:
    return ListingFacts.model_validate(read_json(path))
