"use client";

import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { API_BASE, getJson, postJson, type Dict } from "@/lib/api";

type ReplayPayload = {
  state?: Dict;
  recent_events?: Dict[];
  latest_event?: Dict;
};

const queryClient = new QueryClient();

export function ReplayDashboard() {
  return (
    <QueryClientProvider client={queryClient}>
      <ReplayDashboardInner />
    </QueryClientProvider>
  );
}

function ReplayDashboardInner() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [eventPayload, setEventPayload] = useState<ReplayPayload | null>(null);
  const [streamStatus, setStreamStatus] = useState("connecting");
  const replayQuery = useQuery({
    queryKey: ["replay"],
    queryFn: () => getJson<ReplayPayload>("/api/replay"),
  });

  useEffect(() => {
    const source = new EventSource(`${API_BASE}/replay/events`);
    source.addEventListener("open", () => setStreamStatus("sse live"));
    source.addEventListener("error", () => setStreamStatus("reconnecting"));
    const onMessage = (event: MessageEvent) => {
      setEventPayload(JSON.parse(event.data) as ReplayPayload);
    };
    source.addEventListener("snapshot", onMessage);
    source.addEventListener("update", onMessage);
    return () => source.close();
  }, []);

  const payload = eventPayload || replayQuery.data || {};
  const state = (payload.state || {}) as Dict;
  const latest = (payload.latest_event || {}) as Dict;
  const events = ((payload.recent_events || []) as Dict[]).slice().reverse();
  const running = Boolean(state.running);
  const progress = useMemo(() => {
    const current = Number(state.candle_index || 0);
    const total = Number(state.total_candles || 0);
    return total ? Math.round((current / total) * 100) : 0;
  }, [state.candle_index, state.total_candles]);

  async function startReplay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setMessage("Starting replay...");
    try {
      await postJson("/api/replay/start", {
        config: String(form.get("config") || "paper-demo.json"),
        symbol: String(form.get("symbol") || "XAUUSD"),
        csv_path: String(form.get("csv_path") || "data/history/xauusd-sample.csv"),
        output_dir: String(form.get("output_dir") || ""),
        speed: Number(form.get("speed") || 1),
      });
      setMessage("Replay running.");
      await replayQuery.refetch();
    } catch (exc) {
      setMessage(`Replay failed: ${String(exc)}`);
    } finally {
      setBusy(false);
    }
  }

  async function stopReplay() {
    setBusy(true);
    setMessage("Stopping replay...");
    try {
      await postJson("/api/replay/stop");
      setMessage("Replay stopped.");
      await replayQuery.refetch();
    } catch (exc) {
      setMessage(`Stop failed: ${String(exc)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Replay Lab</h1>
          <p>Run historical candles step by step and inspect how the agent thinks.</p>
        </div>
        <span className="pill">{streamStatus} / {String(state.status || "stopped")}</span>
      </header>

      <section className="grid replay-grid">
        <section className="panel">
          <h2>Replay Controls</h2>
          <form className="settings-form" onSubmit={startReplay}>
            <div className="form-grid">
              <label>
                Config
                <input name="config" defaultValue={String(state.config || "paper-demo.json")} />
              </label>
              <label>
                Symbol
                <input name="symbol" defaultValue={String(state.symbol || "XAUUSD")} />
              </label>
              <label>
                CSV path
                <input name="csv_path" defaultValue={String(state.csv_path || "data/history/xauusd-sample.csv")} />
              </label>
              <label>
                Output dir
                <input name="output_dir" placeholder="auto if empty" />
              </label>
              <label>
                Speed
                <input name="speed" type="number" min={0.01} max={100} step={0.25} defaultValue={Number(state.speed || 1)} />
              </label>
            </div>
            <div className="actions">
              <button disabled={running || busy} type="submit">Start Replay</button>
              <button className="secondary" disabled={!running || busy} type="button" onClick={stopReplay}>Stop</button>
            </div>
          </form>
          {message ? <p className="feedback">{message}</p> : null}
          <div className="progress-bar"><span style={{ width: `${progress}%` }} /></div>
          <MetricStrip
            items={[
              ["Progress", `${value(state.candle_index)} / ${value(state.total_candles)} (${progress}%)`],
              ["Speed", value(state.speed)],
              ["Output", value(state.output_dir)],
              ["Error", value(state.error)],
            ]}
          />
        </section>

        <section className="panel emphasis">
          <h2>Latest Thought</h2>
          <DecisionEvent event={latest} />
        </section>
      </section>

      <section className="panel">
        <h2>Replay Timeline</h2>
        <div className="history-list">
          {events.length ? events.map((event, index) => <DecisionEvent key={`${event.index || index}`} event={event} compact />) : <p className="empty">No replay events yet.</p>}
        </div>
      </section>
    </main>
  );
}

function DecisionEvent({ event, compact = false }: { event: Dict; compact?: boolean }) {
  if (!Object.keys(event).length) return <p className="empty">No candle processed yet.</p>;
  const market = (event.market || {}) as Dict;
  const memory = (event.memory_context || {}) as Dict;
  const riskReasons = (event.risk_reasons || []) as string[];
  const reasonCodes = (event.reason_codes || []) as string[];
  const playbook = (event.playbook || {}) as Dict;
  const selected = (playbook.selected || {}) as Dict;
  const brief = (event.llm_brief || {}) as Dict;
  const trace = (event.llm_trace || {}) as Dict;

  return (
    <article className={compact ? "history-item" : "decision-grid"}>
      <MetricStrip
        items={[
          ["Candle", value(event.index)],
          ["Symbol", value(market.symbol)],
          ["Close", value((market.features as Dict | undefined)?.close)],
          ["Action", value(event.action)],
          ["Side", value(event.side)],
          ["Confidence", value(event.confidence)],
          ["Risk", value(event.risk_status)],
          ["Closed", value(event.closed_count)],
        ]}
      />
      <div className="brief-grid full">
        <Brief label="Playbook" value={`${value(selected.name || "none")} / ${value(playbook.status)}`} />
        <Brief label="Memory Regime" value={value(memory.market_regime)} />
        <Brief label="Memory Lessons" value={((memory.lessons || []) as string[]).join(" ")} />
        <Brief label="LLM Brief" value={value(brief.decision_brief)} />
      </div>
      <ReasonBlock title="Decision Reasons" items={reasonCodes} />
      <ReasonBlock title="Risk Reasons" items={riskReasons} />
      {Object.keys(trace).length ? (
        <details className="trace full">
          <summary>LLM trace</summary>
          <pre>{JSON.stringify(trace, null, 2)}</pre>
        </details>
      ) : null}
    </article>
  );
}

function MetricStrip({ items }: { items: [string, string][] }) {
  return (
    <div className="strip">
      {items.map(([label, item]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{item}</strong>
        </div>
      ))}
    </div>
  );
}

function Brief({ label, value: item }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <p>{item || "none"}</p>
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

function value(input: unknown): string {
  if (input === null || input === undefined) return "";
  if (typeof input === "number") return Number.isInteger(input) ? String(input) : input.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return String(input);
}
