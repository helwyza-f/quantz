from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantz.memory import JsonlExperienceStore
from quantz.models import ExperienceRecord


SCHEMA_VERSION = 1


class SQLiteExperienceStore:
    """Structured experience memory backed by local SQLite.

    The canonical row keeps the full raw experience JSON for backward
    compatibility, while indexed columns make reports and learning queries
    cheaper as the agent collects more decisions.
    """

    def __init__(self, path: str | Path = "data/quantz-memory.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def append(self, record: ExperienceRecord) -> None:
        row = self._record_to_dict(record)
        self.append_raw(row)

    def append_raw(self, row: dict[str, Any]) -> None:
        decision = row.get("decision", {})
        context = row.get("context", {})
        market = context.get("market", {})
        account = context.get("account", {})
        risk = row.get("risk", {})
        execution = row.get("execution") or {}
        outcome = row.get("outcome") or {}
        decision_id = str(decision.get("decision_id") or "")
        if not decision_id:
            raise ValueError("experience row missing decision.decision_id")

        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into experiences (
                    decision_id,
                    symbol,
                    timestamp,
                    action,
                    side,
                    confidence,
                    risk_status,
                    execution_accepted,
                    closed_position_count,
                    reason_codes_json,
                    rejection_reasons_json,
                    market_snapshot_json,
                    account_snapshot_json,
                    execution_json,
                    outcome_json,
                    raw_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    str(decision.get("symbol") or market.get("symbol") or ""),
                    str(decision.get("timestamp") or market.get("timestamp") or ""),
                    str(decision.get("action") or ""),
                    self._optional_str(decision.get("side")),
                    self._optional_float(decision.get("confidence")),
                    str(risk.get("status") or ""),
                    self._optional_bool_int(execution.get("accepted")),
                    len(outcome.get("closed_positions", []) if isinstance(outcome, dict) else []),
                    self._json(decision.get("reason_codes", [])),
                    self._json(risk.get("reasons", [])),
                    self._json(market),
                    self._json(account),
                    self._json(execution),
                    self._json(outcome),
                    self._json(row),
                ),
            )
            conn.execute("delete from executions where decision_id = ?", (decision_id,))
            if execution:
                self._append_execution(conn, decision_id, execution)
            conn.execute("delete from outcomes where decision_id = ?", (decision_id,))
            for position in outcome.get("closed_positions", []) if isinstance(outcome, dict) else []:
                if isinstance(position, dict):
                    self._append_outcome(conn, decision_id, position)

    def read_raw(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("select raw_json from experiences order by id").fetchall()
        return [json.loads(str(row["raw_json"])) for row in rows]

    def context_summary(self, symbol: str, limit: int = 80) -> dict[str, Any]:
        return JsonlExperienceStore.context_summary_from_rows(self.read_symbol_raw(symbol, limit), symbol)

    def read_symbol_raw(self, symbol: str, limit: int = 80) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select raw_json
                from experiences
                where symbol = ?
                order by id desc
                limit ?
                """,
                (symbol, limit),
            ).fetchall()
        return [json.loads(str(row["raw_json"])) for row in reversed(rows)]

    def import_jsonl(self, path: str | Path) -> int:
        rows = JsonlExperienceStore(path).read_raw()
        imported = 0
        for row in rows:
            try:
                self.append_raw(row)
            except ValueError:
                continue
            imported += 1
        return imported

    def decision_lifecycle(self, decision_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            experience = conn.execute(
                """
                select *
                from experiences
                where decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            executions = conn.execute(
                """
                select *
                from executions
                where decision_id = ?
                order by id
                """,
                (decision_id,),
            ).fetchall()
            outcomes = conn.execute(
                """
                select *
                from outcomes
                where decision_id = ?
                order by id
                """,
                (decision_id,),
            ).fetchall()
        if experience is None:
            return {"decision_id": decision_id, "found": False}

        raw = json.loads(str(experience["raw_json"]))
        status = self._lifecycle_status(raw, executions, outcomes)
        return {
            "decision_id": decision_id,
            "found": True,
            "status": status,
            "decision": raw.get("decision", {}),
            "risk": raw.get("risk", {}),
            "execution": raw.get("execution"),
            "outcomes": [json.loads(str(row["raw_json"])) for row in outcomes],
            "execution_rows": [self._row_dict(row) for row in executions],
            "raw": raw,
        }

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("pragma journal_mode = wal")
            conn.execute(
                """
                create table if not exists schema_meta (
                    key text primary key,
                    value text not null
                )
                """
            )
            conn.execute(
                """
                insert or replace into schema_meta (key, value)
                values ('schema_version', ?)
                """,
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                """
                create table if not exists experiences (
                    id integer primary key autoincrement,
                    decision_id text not null unique,
                    symbol text not null,
                    timestamp text not null,
                    action text not null,
                    side text,
                    confidence real,
                    risk_status text not null,
                    execution_accepted integer,
                    closed_position_count integer not null default 0,
                    reason_codes_json text not null,
                    rejection_reasons_json text not null,
                    market_snapshot_json text not null,
                    account_snapshot_json text not null,
                    execution_json text not null,
                    outcome_json text not null,
                    raw_json text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists executions (
                    id integer primary key autoincrement,
                    decision_id text not null,
                    client_order_id text,
                    broker_order_id text,
                    accepted integer not null,
                    message text not null,
                    filled_price real,
                    timestamp text,
                    raw_json text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists outcomes (
                    id integer primary key autoincrement,
                    decision_id text not null,
                    position_id text,
                    symbol text not null,
                    side text,
                    opened_at text,
                    closed_at text,
                    entry_price real,
                    exit_price real,
                    stop_loss real,
                    take_profit real,
                    r_multiple real,
                    exit_reason text,
                    reason_codes_json text not null,
                    raw_json text not null
                )
                """
            )
            conn.execute("create index if not exists idx_experiences_symbol on experiences(symbol)")
            conn.execute("create index if not exists idx_experiences_timestamp on experiences(timestamp)")
            conn.execute("create index if not exists idx_experiences_action on experiences(action)")
            conn.execute("create index if not exists idx_experiences_risk_status on experiences(risk_status)")
            conn.execute("create index if not exists idx_executions_decision_id on executions(decision_id)")
            conn.execute("create index if not exists idx_executions_broker_order_id on executions(broker_order_id)")
            conn.execute("create index if not exists idx_outcomes_symbol on outcomes(symbol)")
            conn.execute("create index if not exists idx_outcomes_decision_id on outcomes(decision_id)")

    @contextmanager
    def _connect(self) -> Any:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _append_execution(self, conn: sqlite3.Connection, decision_id: str, execution: dict[str, Any]) -> None:
        raw = execution.get("raw", {}) if isinstance(execution, dict) else {}
        request = raw.get("request", {}) if isinstance(raw, dict) else {}
        conn.execute(
            """
            insert into executions (
                decision_id,
                client_order_id,
                broker_order_id,
                accepted,
                message,
                filled_price,
                timestamp,
                raw_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                self._optional_str(request.get("client_order_id") if isinstance(request, dict) else None),
                self._optional_str(execution.get("broker_order_id")),
                1 if bool(execution.get("accepted")) else 0,
                str(execution.get("message") or ""),
                self._optional_float(execution.get("filled_price")),
                self._optional_str(execution.get("timestamp")),
                self._json(execution),
            ),
        )

    def _append_outcome(self, conn: sqlite3.Connection, decision_id: str, position: dict[str, Any]) -> None:
        conn.execute(
            """
            insert into outcomes (
                decision_id,
                position_id,
                symbol,
                side,
                opened_at,
                closed_at,
                entry_price,
                exit_price,
                stop_loss,
                take_profit,
                r_multiple,
                exit_reason,
                reason_codes_json,
                raw_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                self._optional_str(position.get("position_id")),
                str(position.get("symbol") or ""),
                self._optional_str(position.get("side")),
                self._optional_str(position.get("opened_at")),
                self._optional_str(position.get("closed_at")),
                self._optional_float(position.get("entry_price")),
                self._optional_float(position.get("exit_price")),
                self._optional_float(position.get("stop_loss")),
                self._optional_float(position.get("take_profit")),
                self._optional_float(position.get("r_multiple")),
                self._optional_str(position.get("exit_reason")),
                self._json(position.get("reason_codes", [])),
                self._json(position),
            ),
        )

    def _record_to_dict(self, record: ExperienceRecord) -> dict[str, Any]:
        return json.loads(self._json(record))

    def _json(self, value: Any) -> str:
        return json.dumps(value, default=self._json_default, sort_keys=True)

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "value"):
            return value.value
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    def _optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _optional_bool_int(self, value: Any) -> int | None:
        if value is None:
            return None
        return 1 if bool(value) else 0

    def _optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def _row_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload.pop("raw_json", None)
        return payload

    def _lifecycle_status(
        self,
        raw: dict[str, Any],
        executions: list[sqlite3.Row],
        outcomes: list[sqlite3.Row],
    ) -> dict[str, Any]:
        decision = raw.get("decision", {})
        risk = raw.get("risk", {})
        execution = raw.get("execution")
        action = decision.get("action")
        risk_status = risk.get("status")
        execution_accepted = bool((execution or {}).get("accepted")) if isinstance(execution, dict) else False
        gaps: list[str] = []

        if action == "open_position" and risk_status == "approved" and execution is None:
            gaps.append("approved_open_position_missing_execution")
        if execution is not None and not executions:
            gaps.append("execution_not_indexed")
        if execution_accepted and not outcomes:
            gaps.append("accepted_execution_without_closed_outcome")

        terminal = False
        if risk_status == "rejected":
            terminal = True
        elif execution is not None and not execution_accepted:
            terminal = True
        elif execution_accepted and outcomes:
            terminal = True

        return {
            "action": action,
            "risk_status": risk_status,
            "execution_accepted": execution_accepted,
            "outcome_count": len(outcomes),
            "terminal": terminal,
            "gaps": gaps,
        }
