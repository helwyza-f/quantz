from pathlib import Path

from quantz.stack import StackConfig, build_service_specs


def test_stack_builds_backend_and_frontend_specs(tmp_path):
    specs = build_service_specs(
        StackConfig(
            root=tmp_path,
            host="127.0.0.1",
            backend_port=8787,
            frontend_port=3000,
            with_frontend=True,
            with_bridge=False,
            reload_backend=True,
        )
    )

    assert [spec.name for spec in specs] == ["backend", "frontend"]
    assert specs[0].health_url == "http://127.0.0.1:8787/health"
    assert "--reload" in specs[0].command
    assert specs[0].env["QUANTZ_ROOT"] == str(tmp_path.resolve())
    assert specs[1].env["NEXT_PUBLIC_API_BASE"] == "http://127.0.0.1:8787"
    assert specs[1].url == "http://127.0.0.1:3000/control"


def test_stack_can_include_mt5_bridge(tmp_path):
    specs = build_service_specs(
        StackConfig(
            root=tmp_path,
            backend_port=8790,
            frontend_port=3001,
            bridge_port=8765,
            with_frontend=False,
            with_bridge=True,
        )
    )

    assert [spec.name for spec in specs] == ["backend", "mt5-bridge"]
    assert specs[1].command[-4:] == ["--host", "127.0.0.1", "--port", "8765"]
    assert specs[1].health_url == "http://127.0.0.1:8765/account"
