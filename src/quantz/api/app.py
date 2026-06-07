from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from quantz.api.settings import SettingsPatch, SettingsStore, settings_db_path
from quantz.autonomous_runtime import AutonomousRuntime, json_default


class AgentSessionRequest(BaseModel):
    config: str | None = None
    max_iterations: int = Field(default=100, ge=1, le=10_000)
    interval_seconds: float = Field(default=1.0, ge=0.1, le=3600)
    trigger_mode: str = "interval"


class ReplaySessionRequest(BaseModel):
    config: str | None = None
    symbol: str = "XAUUSD"
    csv_path: str = "data/history/xauusd-sample.csv"
    output_dir: str | None = None
    speed: float = Field(default=1.0, ge=0.01, le=100)


class AppSettingsRequest(BaseModel):
    default_agent_config: str | None = None
    default_symbol: str | None = None
    max_decisions: int | None = Field(default=None, ge=1, le=10_000)
    decision_gap_seconds: float | None = Field(default=None, ge=0.1, le=3600)
    openai_api_key: str | None = None
    clear_openai_api_key: bool = False


def create_app(root: str | Path | None = None) -> FastAPI:
    root_path = Path(root or os.getenv("QUANTZ_ROOT", ".")).resolve()
    runtime = AutonomousRuntime(root_path)
    settings_store = SettingsStore(settings_db_path(root_path))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings_store.apply_environment()
        yield

    app = FastAPI(
        title="Quantz Autonomous API",
        version="0.2.0",
        description="AI-first backend for autonomous trading decisions, memory, vector recall, and audit.",
        lifespan=lifespan,
    )
    app.state.quantz_runtime = runtime
    app.state.settings_store = settings_store

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "service": "quantz-autonomous-api",
            "status": "running",
            "frontend": "http://127.0.0.1:3000/control",
            "control": "/api/control",
            "events": "/events",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "runtime": "autonomous",
            "database": str(settings_store.path),
        }

    @app.get("/api/control")
    def control(chart: bool = Query(default=True)) -> dict[str, Any]:
        payload = runtime.control_summary(include_chart=chart)
        payload["settings"] = _public_settings(runtime, settings_store)
        payload["runtime"] = runtime.runtime_status(payload["settings"])
        return payload

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return _public_settings(runtime, settings_store)

    @app.put("/api/settings")
    def update_settings(payload: AppSettingsRequest) -> dict[str, Any]:
        if payload.default_agent_config and payload.default_agent_config not in runtime.agent_config_names():
            raise HTTPException(status_code=400, detail="selected config is not an agent-capable config")
        settings_store.apply_patch(
            SettingsPatch(
                default_agent_config=payload.default_agent_config,
                default_symbol=payload.default_symbol,
                max_decisions=payload.max_decisions,
                decision_gap_seconds=payload.decision_gap_seconds,
                openai_api_key=payload.openai_api_key,
                clear_openai_api_key=payload.clear_openai_api_key,
            )
        )
        settings_store.apply_environment()
        return _public_settings(runtime, settings_store)

    @app.get("/api/agent-console")
    def agent_console() -> dict[str, Any]:
        return {"runtime": "autonomous", "audit": runtime.agent_summary(runtime._summary_settings()).get("audit_recent", [])}

    @app.get("/api/runtime-status")
    def runtime_status() -> dict[str, Any]:
        return runtime.runtime_status(_public_settings(runtime, settings_store))

    @app.get("/api/operations")
    def operations() -> dict[str, Any]:
        return runtime.operations_summary(runtime._summary_settings())

    @app.get("/api/pnl")
    def pnl() -> dict[str, Any]:
        agent = runtime.agent_summary(runtime._summary_settings())
        return {
            "total_r": agent["paper_total_r"],
            "win_rate": agent["win_rate"],
            "open_positions": agent["open_position_count"],
            "closed_positions": agent["closed_position_count"],
            "recent_decisions": agent["recent_decisions"],
        }

    @app.get("/api/market-chart")
    def market_chart(
        timeframe: str = Query(default="H1"),
        candles: int = Query(default=80, ge=20, le=300),
    ) -> dict[str, Any]:
        return runtime.market_chart_summary(timeframe=timeframe, candle_count=candles)

    @app.post("/api/agent/start")
    def start_agent(payload: AgentSessionRequest) -> JSONResponse:
        config_name = payload.config or settings_store.get("default_agent_config", "mt5-paper.json")
        if config_name not in runtime.agent_config_names():
            raise HTTPException(status_code=400, detail=f"unknown config: {config_name}")
        result = runtime.start_agent(
            config=config_name,
            max_iterations=payload.max_iterations,
            interval_seconds=payload.interval_seconds,
            trigger_mode=payload.trigger_mode,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return JSONResponse(result)

    @app.post("/api/agent/stop")
    def stop_agent() -> dict[str, Any]:
        return runtime.stop_agent()

    @app.get("/api/replay")
    def replay() -> dict[str, Any]:
        return runtime.replay_summary()

    @app.post("/api/replay/start")
    def start_replay(payload: ReplaySessionRequest) -> dict[str, Any]:
        return runtime.start_replay(payload.model_dump())

    @app.post("/api/replay/stop")
    def stop_replay() -> dict[str, Any]:
        return runtime.stop_replay()

    @app.post("/bridge/tick")
    async def bridge_tick(request: Request) -> dict[str, Any]:
        body = (await request.body()).decode("utf-8")
        return runtime.ingest_bridge_tick(body)

    @app.get("/bridge/tick")
    def bridge_tick_query(request: Request) -> dict[str, Any]:
        return runtime.ingest_bridge_tick_query(str(request.url.query))

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream():
            last_sequence = runtime.event_sequence
            yield _sse("snapshot", _control_payload(runtime, settings_store, include_chart=False))
            while True:
                sequence = await asyncio.to_thread(_wait_for_event, runtime, last_sequence)
                if sequence == last_sequence:
                    yield ": heartbeat\n\n"
                    continue
                last_sequence = sequence
                yield _sse("update", _control_payload(runtime, settings_store, include_chart=False))

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/replay/events")
    async def replay_events() -> StreamingResponse:
        async def stream():
            yield _sse("snapshot", runtime.replay_summary())
            while True:
                await asyncio.sleep(20)
                yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _control_payload(runtime: AutonomousRuntime, settings_store: SettingsStore, include_chart: bool) -> dict[str, Any]:
    payload = runtime.control_summary(include_chart=include_chart)
    payload["settings"] = _public_settings(runtime, settings_store)
    payload["runtime"] = runtime.runtime_status(payload["settings"])
    return payload


def _public_settings(runtime: AutonomousRuntime, settings_store: SettingsStore) -> dict[str, Any]:
    return settings_store.public_snapshot(configs=runtime.agent_config_names())


def _wait_for_event(runtime: AutonomousRuntime, last_sequence: int) -> int:
    with runtime.event_condition:
        runtime.event_condition.wait(timeout=20)
        return runtime.event_sequence


def _sse(event_name: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), default=json_default)
    return f"event: {event_name}\ndata: {body}\n\n"


app = create_app()
