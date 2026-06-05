from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS: dict[str, str] = {
    "default_agent_config": "mt5-demo-live.json",
    "default_symbol": "XAUUSD",
    "max_decisions": "100",
    "decision_gap_seconds": "1",
}

@dataclass(frozen=True)
class SettingsPatch:
    default_agent_config: str | None = None
    default_symbol: str | None = None
    max_decisions: int | None = None
    decision_gap_seconds: float | None = None
    openai_api_key: str | None = None
    clear_openai_api_key: bool = False


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists app_settings (
                    key text primary key,
                    value text not null,
                    is_secret integer not null default 0,
                    updated_at text not null
                )
                """
            )
            now = _now()
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    """
                    insert or ignore into app_settings (key, value, is_secret, updated_at)
                    values (?, ?, 0, ?)
                    """,
                    (key, value, now),
                )

    def get(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(float(self.get(key, str(default))))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: str, *, is_secret: bool = False) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into app_settings (key, value, is_secret, updated_at)
                values (?, ?, ?, ?)
                on conflict(key) do update set
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = excluded.updated_at
                """,
                (key, value, 1 if is_secret else 0, _now()),
            )

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from app_settings where key = ?", (key,))

    def apply_patch(self, patch: SettingsPatch) -> None:
        if patch.default_agent_config is not None:
            self.set("default_agent_config", patch.default_agent_config.strip())
        if patch.default_symbol is not None:
            self.set("default_symbol", patch.default_symbol.strip().upper())
        if patch.max_decisions is not None:
            self.set("max_decisions", str(int(patch.max_decisions)))
        if patch.decision_gap_seconds is not None:
            self.set("decision_gap_seconds", str(float(patch.decision_gap_seconds)))
        if patch.clear_openai_api_key:
            self.delete("openai_api_key")
        elif patch.openai_api_key is not None and patch.openai_api_key.strip():
            self.set("openai_api_key", patch.openai_api_key.strip(), is_secret=True)

    def public_snapshot(self, configs: list[str] | None = None) -> dict[str, Any]:
        api_key = self.get("openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")
        return {
            "database_path": str(self.path),
            "configs": configs or [],
            "default_agent_config": self.get("default_agent_config", DEFAULT_SETTINGS["default_agent_config"]),
            "default_symbol": self.get("default_symbol", DEFAULT_SETTINGS["default_symbol"]),
            "max_decisions": self.get_int("max_decisions", int(DEFAULT_SETTINGS["max_decisions"])),
            "decision_gap_seconds": self.get_float(
                "decision_gap_seconds",
                float(DEFAULT_SETTINGS["decision_gap_seconds"]),
            ),
            "openai_api_key_set": bool(api_key),
            "openai_api_key_source": "sqlite" if self.get("openai_api_key", "") else ("environment" if os.getenv("OPENAI_API_KEY") else "missing"),
            "openai_api_key_preview": _mask_secret(api_key),
            "updated_at": self.last_updated_at(),
        }

    def last_updated_at(self) -> str:
        with self._connect() as conn:
            row = conn.execute("select max(updated_at) as updated_at from app_settings").fetchone()
        return str(row["updated_at"] or "") if row else ""

    def apply_environment(self) -> None:
        api_key = self.get("openai_api_key", "")
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key


def settings_db_path(root: str | Path) -> Path:
    env_path = os.getenv("QUANTZ_DB_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return Path(root) / "data" / "quantz.db"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    return "configured"
