from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantz.models import ExperienceRecord


class JsonlExperienceStore:
    def __init__(self, path: str | Path = "data/experience.jsonl") -> None:
        self.path = Path(path)

    def append(self, record: ExperienceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=self._json_default, sort_keys=True) + "\n")

    def read_raw(self) -> list[dict[str, Any]]:
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
        return rows

    def context_summary(self, symbol: str, limit: int = 80) -> dict[str, Any]:
        rows = [
            row
            for row in self.read_raw()
            if row.get("context", {}).get("market", {}).get("symbol") == symbol
            or row.get("decision", {}).get("symbol") == symbol
        ][-limit:]
        return self.context_summary_from_rows(rows, symbol)

    @staticmethod
    def context_summary_from_rows(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
        if not rows:
            return {
                "symbol": symbol,
                "sample_size": 0,
                "recent_decisions": [],
                "rejection_reasons": {},
                "reason_quality": {},
                "closed_trade_summary": {
                    "count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "average_r": 0.0,
                    "total_r": 0.0,
                },
            }

        rejection_reasons: dict[str, int] = {}
        reason_stats: dict[str, dict[str, float]] = {}
        closed_r: list[float] = []
        recent_decisions: list[dict[str, Any]] = []

        for row in rows:
            decision = row.get("decision", {})
            risk = row.get("risk", {})
            execution = row.get("execution") or {}
            recent_decisions.append(
                {
                    "action": decision.get("action"),
                    "confidence": decision.get("confidence"),
                    "risk_status": risk.get("status"),
                    "execution_accepted": execution.get("accepted"),
                    "reason_codes": decision.get("reason_codes", [])[:8],
                    "timestamp": decision.get("timestamp"),
                }
            )
            if risk.get("status") == "rejected":
                for reason in risk.get("reasons", []):
                    key = str(reason)
                    rejection_reasons[key] = rejection_reasons.get(key, 0) + 1

            for position in (row.get("outcome") or {}).get("closed_positions", []):
                try:
                    r_multiple = float(position.get("r_multiple", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                closed_r.append(r_multiple)
                for reason in position.get("reason_codes", []) or []:
                    key = str(reason)
                    stats = reason_stats.setdefault(key, {"count": 0.0, "total_r": 0.0})
                    stats["count"] += 1
                    stats["total_r"] += r_multiple

        reason_quality = {
            reason: {
                "count": int(stats["count"]),
                "average_r": round(stats["total_r"] / stats["count"], 4) if stats["count"] else 0.0,
                "total_r": round(stats["total_r"], 4),
            }
            for reason, stats in sorted(reason_stats.items())
        }
        total_r = sum(closed_r)
        return {
            "symbol": symbol,
            "sample_size": len(rows),
            "recent_decisions": recent_decisions[-12:],
            "rejection_reasons": rejection_reasons,
            "reason_quality": reason_quality,
            "closed_trade_summary": {
                "count": len(closed_r),
                "win_count": sum(1 for value in closed_r if value > 0),
                "loss_count": sum(1 for value in closed_r if value < 0),
                "average_r": round(total_r / len(closed_r), 4) if closed_r else 0.0,
                "total_r": round(total_r, 4),
            },
        }

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "value"):
            return value.value
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def experience_store(path: str | Path = "data/experience.jsonl") -> Any:
    memory_path = Path(path)
    if memory_path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        from quantz.memory_sqlite import SQLiteExperienceStore

        return SQLiteExperienceStore(memory_path)
    return JsonlExperienceStore(memory_path)
