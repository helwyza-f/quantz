from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DecisionAuditStore:
    """Append-only audit trail for AI decisions and policy decisions."""

    def __init__(self, path: str | Path = "data/decision-audit.jsonl") -> None:
        self.path = Path(path)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "audit_id": event.get("audit_id") or self._audit_id(event),
            "timestamp": event.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=self._json_default, sort_keys=True) + "\n")
        return payload

    def read_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        return rows[-limit:]

    def _audit_id(self, event: dict[str, Any]) -> str:
        body = json.dumps(event, default=self._json_default, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "value"):
            return value.value
        return str(value)


def stable_hash(payload: Any) -> str:
    body = json.dumps(payload, default=str, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
