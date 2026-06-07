from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    health_url: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class StackConfig:
    root: Path
    host: str = "127.0.0.1"
    backend_port: int = 8787
    frontend_port: int = 3000
    bridge_port: int = 8765
    with_frontend: bool = True
    with_bridge: bool = False
    reload_backend: bool = False


def build_service_specs(config: StackConfig) -> list[ServiceSpec]:
    root = config.root.resolve()
    python = _venv_python(root)
    base_env = {
        "PYTHONPATH": "src",
        "QUANTZ_ROOT": str(root),
    }
    backend_command = [
        str(python),
        "-m",
        "uvicorn",
        "quantz.api.app:app",
        "--host",
        config.host,
        "--port",
        str(config.backend_port),
    ]
    if config.reload_backend:
        backend_command.append("--reload")
    specs = [
        ServiceSpec(
            name="backend",
            command=backend_command,
            cwd=root,
            env=base_env,
            health_url=f"http://{config.host}:{config.backend_port}/health",
            url=f"http://{config.host}:{config.backend_port}",
        )
    ]
    if config.with_frontend:
        specs.append(
            ServiceSpec(
                name="frontend",
                command=_frontend_command(config),
                cwd=root,
                env={
                    "NEXT_PUBLIC_API_BASE": f"http://{config.host}:{config.backend_port}",
                },
                health_url=f"http://{config.host}:{config.frontend_port}/control",
                url=f"http://{config.host}:{config.frontend_port}/control",
            )
        )
    if config.with_bridge:
        specs.append(
            ServiceSpec(
                name="mt5-bridge",
                command=[
                    str(python),
                    "scripts/mt5_bridge_server.py",
                    "--host",
                    config.host,
                    "--port",
                    str(config.bridge_port),
                ],
                cwd=root,
                env=base_env,
                health_url=f"http://{config.host}:{config.bridge_port}/account",
                url=f"http://{config.host}:{config.bridge_port}",
            )
        )
    return specs


def run_stack(config: StackConfig) -> int:
    specs = build_service_specs(config)
    processes: list[tuple[ServiceSpec, subprocess.Popen[str]]] = []
    stop_event = threading.Event()
    print("Quantz stack starting")
    for spec in specs:
        env = os.environ.copy()
        env.update(spec.env)
        process = subprocess.Popen(
            spec.command,
            cwd=spec.cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            creationflags=_creation_flags(),
        )
        processes.append((spec, process))
        threading.Thread(target=_stream_output, args=(spec.name, process.stdout, stop_event), daemon=True).start()
        print(f"[stack] started {spec.name} pid={process.pid}")

    try:
        _wait_for_health(specs)
        print("[stack] ready")
        for spec in specs:
            if spec.url:
                print(f"[stack] {spec.name}: {spec.url}")
        print("[stack] press Ctrl+C to stop")
        while True:
            for spec, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"[stack] {spec.name} exited with code {code}")
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stack] stopping")
        return 0
    finally:
        stop_event.set()
        _terminate(processes)


def _wait_for_health(specs: list[ServiceSpec], timeout_seconds: float = 45.0) -> None:
    deadline = time.time() + timeout_seconds
    pending = {spec.name: spec for spec in specs if spec.health_url}
    while pending and time.time() < deadline:
        for name, spec in list(pending.items()):
            if spec.health_url and _healthy(spec.health_url):
                print(f"[stack] {name} healthy")
                pending.pop(name)
        if pending:
            time.sleep(0.75)
    for name in pending:
        print(f"[stack] {name} health pending: {pending[name].health_url}")


def _healthy(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "QuantzStack"})
        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


def _terminate(processes: list[tuple[ServiceSpec, subprocess.Popen[str]]]) -> None:
    for _spec, process in reversed(processes):
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + 8
    for _spec, process in reversed(processes):
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass


def _stream_output(name: str, stream: TextIO | None, stop_event: threading.Event) -> None:
    if stream is None:
        return
    for line in stream:
        if stop_event.is_set():
            break
        print(f"[{name}] {line.rstrip()}")


def _frontend_command(config: StackConfig) -> list[str]:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is required to start the frontend")
    return [
        npm,
        "--prefix",
        "frontend",
        "run",
        "dev",
        "--",
        "--hostname",
        config.host,
        "--port",
        str(config.frontend_port),
    ]


def _venv_python(root: Path) -> Path:
    candidate = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP
