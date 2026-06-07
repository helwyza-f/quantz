from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TeacherExample:
    example_id: str
    symbol: str
    playbook: str
    setup: str
    decision: str
    outcome_label: str
    lesson: str
    market: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TeacherExampleStore:
    def __init__(self, paths: list[str | Path] | None = None) -> None:
        self.paths = [Path(path) for path in (paths or [])]

    def load(self) -> list[TeacherExample]:
        examples: list[TeacherExample] = []
        for path in self.paths:
            examples.extend(self._load_path(path))
        return examples

    def for_symbol(self, symbol: str, limit: int = 6) -> list[dict[str, Any]]:
        selected = [
            example.to_dict()
            for example in self.load()
            if example.symbol.upper() == symbol.upper()
        ]
        return selected[:limit]

    def _load_path(self, path: Path) -> list[TeacherExample]:
        if not path.exists():
            return []
        if path.is_dir():
            examples: list[TeacherExample] = []
            for child in sorted(path.glob("*.json")):
                examples.extend(self._load_file(child))
            return examples
        return self._load_file(path)

    def _load_file(self, path: Path) -> list[TeacherExample]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload if isinstance(payload, list) else [payload]
        return [self._example(row) for row in rows if isinstance(row, dict)]

    def _example(self, row: dict[str, Any]) -> TeacherExample:
        return TeacherExample(
            example_id=str(row["example_id"]),
            symbol=str(row["symbol"]).upper(),
            playbook=str(row.get("playbook", "")),
            setup=str(row.get("setup", "")),
            decision=str(row.get("decision", "hold")),
            outcome_label=str(row.get("outcome_label", "teaching")),
            lesson=str(row["lesson"]),
            market=dict(row.get("market", {})),
            tags=[str(tag) for tag in row.get("tags", [])],
        )
