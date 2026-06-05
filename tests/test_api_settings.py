from __future__ import annotations

from quantz.api.settings import SettingsPatch, SettingsStore


def test_settings_store_persists_public_runtime_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = SettingsStore(tmp_path / "quantz.db")

    store.apply_patch(
        SettingsPatch(
            default_agent_config="mt5-demo-live.json",
            default_symbol="xauusd",
            max_decisions=42,
            decision_gap_seconds=2.5,
            openai_api_key="sk-test-secret-value",
        )
    )
    store.apply_environment()

    snapshot = store.public_snapshot(configs=["mt5-demo-live.json"])

    assert snapshot["default_agent_config"] == "mt5-demo-live.json"
    assert snapshot["default_symbol"] == "XAUUSD"
    assert snapshot["max_decisions"] == 42
    assert snapshot["decision_gap_seconds"] == 2.5
    assert snapshot["openai_api_key_set"] is True
    assert snapshot["openai_api_key_source"] == "sqlite"
    assert snapshot["openai_api_key_preview"] == "configured"
    assert snapshot["openai_api_key_preview"] != "sk-test-secret-value"
    assert snapshot["configs"] == ["mt5-demo-live.json"]
    assert store.get("openai_api_key") == "sk-test-secret-value"


def test_settings_store_can_clear_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-value")
    store = SettingsStore(tmp_path / "quantz.db")
    store.apply_patch(SettingsPatch(openai_api_key="sk-sqlite-value"))

    store.apply_patch(SettingsPatch(clear_openai_api_key=True))
    snapshot = store.public_snapshot()

    assert store.get("openai_api_key", "") == ""
    assert snapshot["openai_api_key_set"] is True
    assert snapshot["openai_api_key_source"] == "environment"
