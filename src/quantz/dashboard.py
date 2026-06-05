from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


class DashboardRenderer:
    def render_experiment(self, experiment_dir: str | Path, output_path: str | Path) -> Path:
        root = Path(experiment_dir)
        output = Path(output_path)
        data = {
            "base_report": self._read_json(root / "base-report.json"),
            "candidate_report": self._read_json(root / "candidate-report.json"),
            "review": self._read_json(root / "review.json"),
            "comparison": self._read_json(root / "comparison.json"),
            "base_config": self._read_json(root / "base-config.json"),
            "candidate_config": self._read_json(root / "candidate-config.json"),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self._html(data), encoding="utf-8")
        return output

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _html(self, data: dict[str, dict[str, Any]]) -> str:
        comparison = data["comparison"]
        review = data["review"]
        base = data["base_report"]
        candidate = data["candidate_report"]
        verdict = comparison.get("verdict", "unknown")

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quantz Experiment Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687385;
      --line: #dbe1e8;
      --accent: #116149;
      --warn: #946200;
      --bad: #a32c2c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    header {{ display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; margin-bottom: 22px; }}
    h1 {{ font-size: 28px; margin: 0 0 6px; letter-spacing: 0; }}
    h2 {{ font-size: 17px; margin: 0 0 14px; }}
    h3 {{ font-size: 14px; margin: 0 0 10px; color: var(--muted); font-weight: 700; text-transform: uppercase; }}
    p {{ margin: 0; color: var(--muted); }}
    .badge {{ border: 1px solid var(--line); background: var(--panel); border-radius: 999px; padding: 7px 11px; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 18px; }}
    section, .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .metric .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .metric .value {{ font-size: 24px; font-weight: 750; }}
    .positive {{ color: var(--accent); }}
    .negative {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 5px 0; }}
    code {{ background: #eef2f5; border-radius: 5px; padding: 2px 5px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #111827; color: #f9fafb; border-radius: 8px; padding: 14px; overflow: auto; }}
    @media (max-width: 860px) {{
      main {{ padding: 18px; }}
      header, .two {{ display: block; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      section {{ margin-bottom: 14px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Quantz Experiment Dashboard</h1>
      <p>Base vs candidate paper-trading experiment summary.</p>
    </div>
    <div class="badge">{self._escape(verdict)}</div>
  </header>

  <div class="grid">
    {self._metric("Base Total R", base.get("total_r_multiple", 0))}
    {self._metric("Candidate Total R", candidate.get("total_r_multiple", 0))}
    {self._metric("Rejected Delta", comparison.get("metric_deltas", {}).get("rejected_count", 0), invert=True)}
    {self._metric("Closed Delta", comparison.get("metric_deltas", {}).get("closed_position_count", 0))}
  </div>

  <div class="two">
    <section>
      <h2>Review</h2>
      <h3>Promotion Status</h3>
      <p><code>{self._escape(review.get("promotion_status", "unknown"))}</code></p>
      <h3 style="margin-top:16px">Risk Notes</h3>
      {self._list(review.get("risk_notes", []))}
    </section>
    <section>
      <h2>Comparison Notes</h2>
      {self._list(comparison.get("notes", []))}
    </section>
  </div>

  <section style="margin-bottom:18px">
    <h2>Recommendations</h2>
    {self._recommendations(review.get("recommendations", []))}
  </section>

  <div class="two">
    <section>
      <h2>Base By Symbol</h2>
      {self._symbol_table(base.get("by_symbol", {}))}
    </section>
    <section>
      <h2>Candidate By Symbol</h2>
      {self._symbol_table(candidate.get("by_symbol", {}))}
    </section>
  </div>

  <section>
    <h2>Raw Comparison</h2>
    <pre>{self._escape(json.dumps(comparison, indent=2, sort_keys=True))}</pre>
  </section>
</main>
</body>
</html>"""

    def _metric(self, label: str, value: Any, invert: bool = False) -> str:
        numeric = float(value or 0)
        css = "positive" if numeric > 0 else "negative" if numeric < 0 else ""
        if invert and numeric < 0:
            css = "positive"
        elif invert and numeric > 0:
            css = "negative"
        return f"""<div class="metric"><div class="label">{self._escape(label)}</div><div class="value {css}">{self._escape(value)}</div></div>"""

    def _list(self, values: list[Any]) -> str:
        if not values:
            return "<p>None</p>"
        items = "".join(f"<li>{self._escape(value)}</li>" for value in values)
        return f"<ul>{items}</ul>"

    def _recommendations(self, values: list[dict[str, Any]]) -> str:
        if not values:
            return "<p>No recommendations.</p>"
        rows = []
        for item in values:
            rows.append(
                "<tr>"
                f"<td>{self._escape(item.get('type', ''))}</td>"
                f"<td>{self._escape(item.get('target', ''))}</td>"
                f"<td>{self._escape(item.get('confidence', ''))}</td>"
                f"<td>{self._escape(item.get('reason', ''))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Type</th><th>Target</th><th>Confidence</th><th>Reason</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _symbol_table(self, by_symbol: dict[str, dict[str, Any]]) -> str:
        if not by_symbol:
            return "<p>No symbol data.</p>"
        rows = []
        for symbol, item in sorted(by_symbol.items()):
            rows.append(
                "<tr>"
                f"<td>{self._escape(symbol)}</td>"
                f"<td>{self._escape(item.get('decisions', 0))}</td>"
                f"<td>{self._escape(item.get('executed', 0))}</td>"
                f"<td>{self._escape(item.get('closed', 0))}</td>"
                f"<td>{self._escape(item.get('open_positions', 0))}</td>"
                f"<td>{self._escape(item.get('total_r_multiple', 0))}</td>"
                f"<td>{self._escape(item.get('average_r_multiple', 0))}</td>"
                "</tr>"
            )
        return "<table><thead><tr><th>Symbol</th><th>Decisions</th><th>Executed</th><th>Closed</th><th>Open</th><th>Total R</th><th>Avg R</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"

    def _escape(self, value: Any) -> str:
        return html.escape(str(value), quote=True)
