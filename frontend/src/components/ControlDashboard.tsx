"use client";

import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { API_BASE, getJson, postJson, putJson, type Dict } from "@/lib/api";
import { MarketChart } from "@/components/MarketChart";

type ControlPayload = {
  live?: Dict;
  agent?: Dict;
  operations?: Dict;
  chart?: Dict;
  ticks?: Dict[];
  stream?: Dict;
  settings?: AppSettings;
  runtime?: Dict;
};

type AppSettings = {
  configs?: string[];
  default_agent_config?: string;
  default_symbol?: string;
  max_decisions?: number;
  decision_gap_seconds?: number;
  openai_api_key_set?: boolean;
  openai_api_key_source?: string;
  openai_api_key_preview?: string;
  database_path?: string;
  updated_at?: string;
};

const timeframes = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const queryClient = new QueryClient();

export function ControlDashboard() {
  return (
    <QueryClientProvider client={queryClient}>
      <ControlDashboardInner />
    </QueryClientProvider>
  );
}

function ControlDashboardInner() {
  const [eventPayload, setEventPayload] = useState<ControlPayload | null>(null);
  const [chartOverride, setChartOverride] = useState<Dict | null>(null);
  const [timeframe, setTimeframe] = useState("H1");
  const [candles, setCandles] = useState(80);
  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState("");
  const [sessionBusy, setSessionBusy] = useState(false);
  const [sessionMessage, setSessionMessage] = useState("");

  const controlQuery = useQuery({
    queryKey: ["control"],
    queryFn: () => getJson<ControlPayload>("/api/control?chart=true"),
  });

  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => getJson<AppSettings>("/api/settings"),
  });

  const payload = useMemo<ControlPayload | null>(() => {
    const merged = { ...(controlQuery.data || {}), ...(eventPayload || {}) } as ControlPayload;
    if (chartOverride || controlQuery.data?.chart || eventPayload?.chart) {
      merged.chart = chartOverride || eventPayload?.chart || controlQuery.data?.chart;
    }
    return Object.keys(merged).length ? merged : null;
  }, [chartOverride, controlQuery.data, eventPayload]);

  async function loadControl(withChart = true) {
    const next = await getJson<ControlPayload>(`/api/control?chart=${withChart ? "true" : "false"}`);
    setEventPayload((current) => ({ ...(current || {}), ...next }));
  }

  async function loadChart(nextTimeframe = timeframe, nextCandles = candles) {
    const chart = await getJson<Dict>(`/api/market-chart?timeframe=${nextTimeframe}&candles=${nextCandles}`);
    setChartOverride(chart);
  }

  useEffect(() => {
    const source = new EventSource(`${API_BASE}/events`);
    source.addEventListener("open", () => setStatus("sse live"));
    source.addEventListener("error", () => setStatus("reconnecting"));
    const onMessage = (event: MessageEvent) => {
      const next = JSON.parse(event.data) as ControlPayload;
      setEventPayload((current) => ({ ...(current || {}), ...next }));
    };
    source.addEventListener("snapshot", onMessage);
    source.addEventListener("update", onMessage);
    return () => source.close();
  }, []);

  const queryError = controlQuery.error ? String(controlQuery.error) : "";
  const live = payload?.live || {};
  const summary = (live.summary || {}) as Dict;
  const tick = (live.latest_tick || {}) as Dict;
  const agent = payload?.agent || {};
  const monitor = (agent.monitor || {}) as Dict;
  const latest = (agent.latest_decision || {}) as Dict;
  const runtime = payload?.runtime || {};
  const settings = settingsQuery.data || payload?.settings || {};
  const chart = useMemo(() => (payload?.chart || {}) as Dict, [payload?.chart]);
  const selectedSymbol = ((chart.symbols as string[] | undefined) || ["XAUUSD"])[0] || "XAUUSD";

  const chartRows = useMemo(() => {
    const series = (chart.series || {}) as Record<string, Dict[]>;
    return series[selectedSymbol] || [];
  }, [chart, selectedSymbol]);

  const overlays = useMemo(() => {
    const all = (chart.overlays || {}) as Record<string, Dict>;
    return all[selectedSymbol] || {};
  }, [chart, selectedSymbol]);

  async function startSession() {
    setError("");
    setSessionBusy(true);
    setSessionMessage("Starting agent session...");
    try {
      await postJson("/api/agent/start", {
        config: settings.default_agent_config,
        max_iterations: settings.max_decisions || 100,
        interval_seconds: settings.decision_gap_seconds || 1,
        trigger_mode: "stream",
      });
      setSessionMessage("Agent session running.");
      await controlQuery.refetch();
      await loadControl(false);
    } catch (exc) {
      setSessionMessage("");
      setError(String(exc));
    } finally {
      setSessionBusy(false);
    }
  }

  async function stopSession() {
    setError("");
    setSessionBusy(true);
    setSessionMessage("Stopping agent session...");
    try {
      await postJson("/api/agent/stop");
      setSessionMessage("Agent session stopped.");
      await controlQuery.refetch();
      await loadControl(false);
    } catch (exc) {
      setSessionMessage("");
      setError(String(exc));
    } finally {
      setSessionBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Quantz Control</h1>
          <p>Market stream, agent reasoning, positions, and LLM audit trail.</p>
        </div>
        <span className="pill">{status}</span>
      </header>

      {error || queryError ? <section className="panel">{error || queryError}</section> : null}

      <section className="grid control-grid">
        <div className="panel">
          <h2>Live Market</h2>
          <MetricStrip
            items={[
              ["Symbol", String(summary.latest_symbol || selectedSymbol)],
              ["Bid", value(summary.latest_bid)],
              ["Ask", value(summary.latest_ask)],
              ["Spread", value(summary.latest_spread)],
              ["Source", String(summary.latest_source_label || "")],
              ["Tick Time", String(tick.tick_time_display || "")],
              ["Agent", String(latest.action || "none")],
              ["Risk", String(latest.risk_status || "")],
            ]}
          />
          <div className="toolbar">
            <label>
              Symbol
              <select value={selectedSymbol} disabled>
                <option>{selectedSymbol}</option>
              </select>
            </label>
            <label>
              Timeframe
              <select
                value={timeframe}
                onChange={(event) => {
                  setTimeframe(event.target.value);
                  loadChart(event.target.value, candles).catch((exc) => setError(String(exc)));
                }}
              >
                {timeframes.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              Candles
              <input
                type="number"
                min={20}
                max={300}
                step={10}
                value={candles}
                onChange={(event) => {
                  const next = Number(event.target.value);
                  setCandles(next);
                  loadChart(timeframe, next).catch((exc) => setError(String(exc)));
                }}
              />
            </label>
            <button
              onClick={() => {
                const next = Math.max(20, candles - 10);
                setCandles(next);
                loadChart(timeframe, next).catch((exc) => setError(String(exc)));
              }}
            >
              Zoom In
            </button>
            <button
              className="secondary"
              onClick={() => {
                const next = Math.min(300, candles + 10);
                setCandles(next);
                loadChart(timeframe, next).catch((exc) => setError(String(exc)));
              }}
            >
              Zoom Out
            </button>
            <button
              className="secondary"
              onClick={() => {
                setCandles(80);
                loadChart(timeframe, 80).catch((exc) => setError(String(exc)));
              }}
            >
              Reset
            </button>
          </div>
          <MarketChart rows={chartRows} overlays={overlays} latestTick={tick} />
        </div>

        <aside className="grid">
          <RuntimeStatusPanel runtime={runtime} status={status} />

          <SettingsPanel
            settings={settings}
            loading={settingsQuery.isLoading}
            onSaved={async () => {
              await settingsQuery.refetch();
              await loadControl(false);
            }}
          />

          <section className="panel emphasis">
            <h2>Agent Session</h2>
            <MetricStrip
              items={[
                ["Monitor", String(monitor.status || "stopped")],
                ["Trigger", String(monitor.trigger_mode || "")],
                ["Config", String(agent.config || "")],
                ["Brain", String(agent.brain || "")],
                ["Mode", String(agent.mode || "")],
                ["MT5 Open", value(agent.mt5_open_position_count)],
              ]}
            />
            <div className="actions">
              <button onClick={startSession} disabled={Boolean(monitor.running) || sessionBusy}>
                Start Session
              </button>
              <button className="secondary" onClick={stopSession} disabled={!monitor.running || sessionBusy}>
                Stop
              </button>
            </div>
            {sessionMessage ? <p className="feedback">{sessionMessage}</p> : null}
          </section>

          <section className="panel">
            <h2>Latest Decision</h2>
            <DecisionCard decision={latest} />
          </section>

          <section className="panel">
            <h2>Tick Health</h2>
            <div className="metrics">
              <Metric label="Source" value={String(summary.latest_source_label || "")} />
              <Metric label="Tape" value={value(summary.count)} />
              <Metric label="Ticks/min" value={value(summary.ticks_per_minute)} />
              <Metric label="Age" value={summary.latest_age_seconds ? `${summary.latest_age_seconds}s` : ""} />
              <Metric label="Delta" value={value(summary.bid_delta)} />
              <Metric label="Spread" value={`${value(summary.spread_min)} / ${value(summary.spread_max)}`} />
            </div>
          </section>
        </aside>
      </section>

      <section className="grid lower-grid">
        <section className="panel">
          <h2>MT5 Positions</h2>
          <PositionsTable rows={(agent.mt5_open_positions || []) as Dict[]} empty="No open MT5 positions." />
        </section>
        <section className="panel">
          <h2>Paper Positions</h2>
          <PositionsTable rows={(agent.open_positions || []) as Dict[]} empty="No open paper positions." />
        </section>
        <section className="panel wide">
          <h2>Decision History</h2>
          <DecisionHistory rows={(agent.recent_decisions || []) as Dict[]} />
        </section>
      </section>
    </main>
  );
}

function RuntimeStatusPanel({ runtime, status }: { runtime: Dict; status: string }) {
  const backend = (runtime.backend || {}) as Dict;
  const agent = (runtime.agent || {}) as Dict;
  const ai = (runtime.ai || {}) as Dict;
  const market = (runtime.market || {}) as Dict;
  const memory = (runtime.memory || {}) as Dict;
  const bridge = (runtime.bridge || {}) as Dict;
  const monitor = (runtime.monitor || {}) as Dict;
  const overall = String(runtime.overall || "unknown");
  return (
    <section className="panel runtime-panel">
      <div className="panel-title-row">
        <h2>Runtime</h2>
        <span className={`pill compact ${overallClass(overall)}`}>{labelForOverall(overall)}</span>
      </div>
      <div className="runtime-grid">
        <StatusLine label="Backend" value={String(backend.status || "unknown")} state={String(backend.status || "unknown")} />
        <StatusLine label="SSE" value={status} state={status === "sse live" ? "ok" : "warn"} />
        <StatusLine label="AI Key" value={ai.api_key_set ? "set" : "missing"} state={ai.api_key_set ? "ok" : "blocked"} />
        <StatusLine label="EA Tick" value={market.ea_tick_active ? `${value(market.latest_tick_age_seconds)}s` : "waiting"} state={market.ea_tick_active ? "ok" : "warn"} />
        <StatusLine label="Bridge" value={bridge.expected ? (bridge.reachable ? "reachable" : "offline") : "not required"} state={!bridge.expected || bridge.reachable ? "ok" : "warn"} />
        <StatusLine label="Monitor" value={String(monitor.status || "stopped")} state={monitor.status === "error" ? "blocked" : "ok"} />
      </div>
      <MetricStrip
        items={[
          ["Config", String(agent.config || "")],
          ["Planner", String(agent.planner || "")],
          ["Mode", String(agent.mode || "")],
          ["Market", String(market.source || "")],
          ["Experience", value(memory.experience_count)],
          ["Vector", `${value(memory.vector_count)} ${memory.vector_enabled ? "on" : "off"}`],
          ["Audit", value(memory.audit_count_recent)],
          ["TPM", value(market.ticks_per_minute)],
        ]}
      />
    </section>
  );
}

function StatusLine({ label, value: item, state }: { label: string; value: string; state: string }) {
  return (
    <div className="status-line">
      <span className={`status-dot ${statusClass(state)}`} />
      <div>
        <span>{label}</span>
        <strong>{item}</strong>
      </div>
    </div>
  );
}

function statusClass(state: string): string {
  if (["ok", "running", "sse live", "set", "reachable"].includes(state)) return "ok";
  if (["blocked", "error", "missing"].includes(state)) return "blocked";
  return "warn";
}

function overallClass(overall: string): string {
  if (overall === "ready") return "ok";
  if (overall.includes("missing") || overall.includes("error")) return "blocked";
  return "warn";
}

function labelForOverall(overall: string): string {
  return overall.replaceAll("_", " ");
}

function SettingsPanel({
  settings,
  loading,
  onSaved,
}: {
  settings: AppSettings;
  loading: boolean;
  onSaved: () => Promise<void>;
}) {
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const configs = settings.configs?.length ? settings.configs : ["mt5-demo-live.json"];

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setSaving(true);
    setMessage("Saving settings...");
    const form = new FormData(formElement);
    try {
      await putJson<AppSettings>("/api/settings", {
        default_agent_config: String(form.get("default_agent_config") || settings.default_agent_config || "mt5-demo-live.json"),
        default_symbol: String(form.get("default_symbol") || settings.default_symbol || "XAUUSD"),
        max_decisions: Number(form.get("max_decisions") || settings.max_decisions || 100),
        decision_gap_seconds: Number(form.get("decision_gap_seconds") || settings.decision_gap_seconds || 1),
        openai_api_key: String(form.get("openai_api_key") || ""),
        clear_openai_api_key: Boolean(form.get("clear_openai_api_key")),
      });
      setMessage("Settings saved.");
      await onSaved();
      formElement.reset();
    } catch (exc) {
      setMessage(`Save failed: ${String(exc)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-title-row">
        <h2>Runtime Settings</h2>
        <span className={settings.openai_api_key_set ? "pill compact" : "pill compact muted"}>{settings.openai_api_key_set ? "LLM ready" : "No key"}</span>
      </div>
      <form className="settings-form" key={settings.updated_at || "settings"} onSubmit={saveSettings}>
        <label>
          OpenAI API key
          <input name="openai_api_key" type="password" autoComplete="off" placeholder={settings.openai_api_key_set ? "paste new key to replace" : "paste key here"} />
        </label>
        <div className="settings-note">
          Source: <strong>{settings.openai_api_key_source || "missing"}</strong>
          {settings.openai_api_key_preview ? ` (${settings.openai_api_key_preview})` : ""}
        </div>
        <label className="check-row">
          <input name="clear_openai_api_key" type="checkbox" value="true" />
          Clear stored key
        </label>
        <div className="form-grid">
          <label>
            Agent config
            <select name="default_agent_config" defaultValue={settings.default_agent_config || configs[0]}>
              {configs.map((config) => (
                <option key={config} value={config}>
                  {config}
                </option>
              ))}
            </select>
          </label>
          <label>
            Symbol
            <input name="default_symbol" defaultValue={settings.default_symbol || "XAUUSD"} />
          </label>
          <label>
            Max decisions
            <input name="max_decisions" type="number" min={1} max={10000} defaultValue={settings.max_decisions || 100} />
          </label>
          <label>
            Decision gap
            <input name="decision_gap_seconds" type="number" min={0.1} max={3600} step={0.1} defaultValue={settings.decision_gap_seconds || 1} />
          </label>
        </div>
        <button type="submit" disabled={saving || loading}>
          {saving ? "Saving..." : "Save Settings"}
        </button>
      </form>
      {message ? <p className="feedback">{message}</p> : null}
    </section>
  );
}

function MetricStrip({ items }: { items: [string, string][] }) {
  return (
    <div className="strip">
      {items.map(([label, item]) => (
        <Metric key={label} label={label} value={item} />
      ))}
    </div>
  );
}

function Metric({ label, value: item }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{item}</strong>
    </div>
  );
}

function DecisionCard({ decision }: { decision: Dict }) {
  if (!decision.action) return <p className="empty">No decisions recorded yet.</p>;
  return (
    <div className="decision-grid">
      {[
        ["Action", decision.action],
        ["Confidence", decision.confidence],
        ["Risk", decision.risk_status],
        ["Brain", decision.analyst_model],
        ["Bias", decision.analyst_bias],
        ["Regime", decision.analyst_regime],
      ].map(([label, item]) => (
        <Metric key={String(label)} label={String(label)} value={value(item)} />
      ))}
      <DecisionBrief brief={(decision.llm_brief || {}) as Dict} />
      <ReasonBlock title="Reasons" items={(decision.reasons || []) as string[]} />
      <ReasonBlock title="Risk Notes" items={(decision.analyst_risk_notes || []) as string[]} />
      <LlmTrace trace={(decision.llm_trace || {}) as Dict} />
    </div>
  );
}

function DecisionHistory({ rows }: { rows: Dict[] }) {
  if (!rows.length) return <p className="empty">No decisions recorded yet.</p>;
  return (
    <div className="history-list">
      {rows.map((row, index) => (
        <article className="history-item" key={String(row.decision_id || row.timestamp_raw || index)}>
          <div className="history-head">
            {[
              ["Time", row.timestamp],
              ["Symbol", row.symbol],
              ["Action", row.action],
              ["Confidence", row.confidence],
              ["Risk", row.risk_status],
              ["Brain", row.analyst_model],
              ["Execution", row.execution || "not sent"],
            ].map(([label, item]) => (
              <Metric key={String(label)} label={String(label)} value={value(item)} />
            ))}
          </div>
          <DecisionBrief brief={(row.llm_brief || {}) as Dict} />
          <ReasonBlock title="Reasons" items={(row.reasons || []) as string[]} />
          <LlmTrace trace={(row.llm_trace || {}) as Dict} />
        </article>
      ))}
    </div>
  );
}

function DecisionBrief({ brief }: { brief: Dict }) {
  if (!Object.keys(brief).length) return null;
  const observations = (brief.key_observations || []) as string[];
  const memoryNotes = (brief.memory_notes || []) as string[];
  return (
    <div className="decision-brief full">
      <span>LLM Decision Brief</span>
      {brief.decision_brief ? <strong>{value(brief.decision_brief)}</strong> : null}
      <div className="brief-grid">
        {brief.market_read ? <BriefItem label="Market read" value={brief.market_read} /> : null}
        {brief.entry_plan ? <BriefItem label="Entry plan" value={brief.entry_plan} /> : null}
        {brief.invalidation ? <BriefItem label="Invalidation" value={brief.invalidation} /> : null}
      </div>
      <ReasonBlock title="Key Observations" items={observations} />
      <ReasonBlock title="Memory Notes" items={memoryNotes} />
    </div>
  );
}

function BriefItem({ label, value: item }: { label: string; value: unknown }) {
  return (
    <div>
      <span>{label}</span>
      <p>{value(item)}</p>
    </div>
  );
}

function ReasonBlock({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="full">
      <span>{title}</span>
      <ul className="reason-list">
        {(items.length ? items : ["none"]).map((item, index) => (
          <li key={`${item}-${index}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function LlmTrace({ trace }: { trace: Dict }) {
  if (!Object.keys(trace).length) return null;
  const response = (trace.response || {}) as Dict;
  return (
    <div className="trace full">
      <span>LLM Trace</span>
      <details>
        <summary>Request sent to LLM</summary>
        <pre>{JSON.stringify(trace.request || {}, null, 2)}</pre>
      </details>
      <details>
        <summary>Response from LLM</summary>
        <pre>{JSON.stringify(response, null, 2)}</pre>
      </details>
    </div>
  );
}

function PositionsTable({ rows, empty }: { rows: Dict[]; empty: string }) {
  if (!rows.length) return <p className="empty">{empty}</p>;
  const columns = ["symbol", "side", "volume", "entry_price", "stop_loss", "take_profit", "profit", "opened_at"];
  return (
    <table>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column}>{column}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={String(row.ticket || index)}>
            {columns.map((column) => (
              <td key={column}>{value(row[column])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function value(input: unknown): string {
  if (input === null || input === undefined) return "";
  if (typeof input === "number") return Number.isInteger(input) ? String(input) : input.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return String(input);
}
