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
from quantz.cli import _monitor
from quantz.web import WebApp


class AgentSessionRequest(BaseModel):
    config: str | None = None
    max_iterations: int = Field(default=100, ge=1, le=10_000)
    interval_seconds: float = Field(default=1.0, ge=0.1, le=3600)
    trigger_mode: str = "stream"


class AppSettingsRequest(BaseModel):
    default_agent_config: str | None = None
    default_symbol: str | None = None
    max_decisions: int | None = Field(default=None, ge=1, le=10_000)
    decision_gap_seconds: float | None = Field(default=None, ge=0.1, le=3600)
    openai_api_key: str | None = None
    clear_openai_api_key: bool = False


def create_app(root: str | Path | None = None) -> FastAPI:
    root_path = Path(root or os.getenv("QUANTZ_ROOT", ".")).resolve()
    legacy = WebApp(root_path, monitor_fn=_monitor)
    settings_store = SettingsStore(settings_db_path(root_path))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings_store.apply_environment()
        legacy.start_tick_collector(interval_seconds=1.0)
        try:
            yield
        finally:
            legacy.stop_tick_collector()

    app = FastAPI(
        title="Quantz API",
        version="0.1.0",
        description="FastAPI backend for the Quantz operational console.",
        lifespan=lifespan,
    )
    app.state.quantz = legacy
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
            "service": "quantz-api",
            "status": "running",
            "frontend": "http://127.0.0.1:3000/control",
            "control": "/api/control",
            "events": "/events",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "database": str(settings_store.path)}

    @app.get("/api/control")
    def control(chart: bool = Query(default=True)) -> dict[str, Any]:
        payload = legacy._control_summary(include_chart=chart)
        payload["settings"] = _public_settings(legacy, settings_store)
        return payload

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return _public_settings(legacy, settings_store)

    @app.put("/api/settings")
    def update_settings(payload: AppSettingsRequest) -> dict[str, Any]:
        if payload.default_agent_config and payload.default_agent_config not in legacy._agent_config_names():
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
        return _public_settings(legacy, settings_store)

    @app.get("/api/agent-console")
    def agent_console() -> dict[str, Any]:
        return legacy._agent_console_summary()

    @app.get("/api/operations")
    def operations() -> dict[str, Any]:
        return legacy._operations_summary()

    @app.get("/api/pnl")
    def pnl() -> dict[str, Any]:
        return legacy._pnl_summary()

    @app.get("/api/market-chart")
    def market_chart(
        timeframe: str = Query(default="H1"),
        candles: int = Query(default=80, ge=20, le=300),
    ) -> dict[str, Any]:
        return legacy._market_chart_summary(timeframe=timeframe, candle_count=candles)

    @app.post("/api/agent/start")
    def start_agent(payload: AgentSessionRequest) -> JSONResponse:
        config_name = payload.config or settings_store.get("default_agent_config", "mt5-demo-live.json")
        result = legacy._start_monitor_from_form(
            {
                "config": [config_name],
                "max_iterations": [str(payload.max_iterations)],
                "interval_seconds": [str(payload.interval_seconds)],
                "trigger_mode": [payload.trigger_mode],
            }
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return JSONResponse(result)

    @app.post("/api/agent/stop")
    def stop_agent() -> dict[str, Any]:
        return legacy._stop_monitor()

    @app.post("/bridge/tick")
    async def bridge_tick(request: Request) -> dict[str, Any]:
        body = (await request.body()).decode("utf-8")
        return legacy._ingest_bridge_tick(body)

    @app.get("/bridge/tick")
    def bridge_tick_query(request: Request) -> dict[str, Any]:
        return legacy._ingest_bridge_tick_query(str(request.url.query))

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream():
            last_sequence = legacy.event_sequence
            yield _sse("snapshot", legacy._control_summary(include_chart=False))
            while True:
                sequence = await asyncio.to_thread(_wait_for_event, legacy, last_sequence)
                if sequence == last_sequence:
                    yield ": heartbeat\n\n"
                    continue
                last_sequence = sequence
                yield _sse("update", legacy._control_summary(include_chart=False))

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _public_settings(legacy: WebApp, settings_store: SettingsStore) -> dict[str, Any]:
    return settings_store.public_snapshot(configs=legacy._agent_config_names())


def _wait_for_event(legacy: WebApp, last_sequence: int) -> int:
    with legacy.event_condition:
        legacy.event_condition.wait(timeout=20)
        return legacy.event_sequence


def _sse(event_name: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return f"event: {event_name}\ndata: {body}\n\n"


app = create_app()
