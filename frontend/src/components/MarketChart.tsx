import type { Dict } from "@/lib/api";

type Props = {
  rows: Dict[];
  overlays: Dict;
  latestTick: Dict;
};

export function MarketChart({ rows, overlays, latestTick }: Props) {
  const selected = rows.slice(-Math.max(rows.length, 1));
  if (!selected.length) {
    return <div className="chart-shell" />;
  }

  const width = 1100;
  const height = 520;
  const left = 58;
  const right = 112;
  const top = 42;
  const bottom = 42;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const values = selected.flatMap((row) => [num(row.high), num(row.low), num(row.close), num(row.open)]);
  const bid = num(latestTick.bid);
  const ask = num(latestTick.ask);
  if (bid) values.push(bid);
  if (ask) values.push(ask);
  const lower = Math.min(...values);
  const upper = Math.max(...values);
  const span = upper - lower || 1;
  const yFor = (price: number) => top + ((upper - price) / span) * plotHeight;
  const count = Math.max(selected.length - 1, 1);
  const candleWidth = Math.max(4, Math.min(14, plotWidth / Math.max(selected.length, 1) - 4));

  return (
    <div className="chart-shell">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Market candlestick chart">
        <rect width={width} height={height} fill="#0f1720" />
        <text x={left} y={26} fill="#e8eef6" fontWeight={800}>
          {String(selected[0]?.symbol || latestTick.symbol || "XAUUSD")} Candlestick
        </text>
        {[0, 1, 2, 3, 4].map((step) => {
          const y = top + (step / 4) * plotHeight;
          const price = upper - (step / 4) * span;
          return (
            <g key={step}>
              <line x1={left} y1={y} x2={width - right} y2={y} stroke="#223041" />
              <text x={width - right + 8} y={y + 4} fill="#9aa8ba" fontSize={12}>
                {price.toFixed(2)}
              </text>
            </g>
          );
        })}
        {selected.map((row, index) => {
          const open = num(row.open || row.close);
          const high = num(row.high || open);
          const low = num(row.low || open);
          const close = num(row.close || open);
          const x = left + (index / count) * plotWidth;
          const up = close >= open;
          return (
            <g key={`${row.timestamp || row.step || index}`}>
              <line x1={x} y1={yFor(high)} x2={x} y2={yFor(low)} stroke={up ? "#1fbf86" : "#e05f5f"} strokeWidth={1.4} />
              <rect
                x={x - candleWidth / 2}
                y={Math.min(yFor(open), yFor(close))}
                width={candleWidth}
                height={Math.max(2, Math.abs(yFor(open) - yFor(close)))}
                fill={up ? "#16a06a" : "#d94c4c"}
              />
            </g>
          );
        })}
        <PriceLine label="BID" price={bid} color="#22c55e" yFor={yFor} width={width} left={left} right={right} />
        <PriceLine label="ASK" price={ask} color="#ef4444" yFor={yFor} width={width} left={left} right={right} dash />
        {Array.isArray(overlays.signals)
          ? overlays.signals.slice(-12).map((signal, index) => {
              const candleIndex = Math.max(0, selected.length - 12 + index);
              const candle = selected[candleIndex] || selected[selected.length - 1];
              const x = left + (candleIndex / count) * plotWidth;
              const y = yFor(num(candle.low || candle.close)) + 22;
              return (
                <g key={`${signal.timestamp || index}`}>
                  <circle cx={x} cy={Math.min(height - bottom - 12, y)} r={8} fill="#f2c94c" />
                  <text x={x - 3} y={Math.min(height - bottom - 8, y + 4)} fill="#111827" fontSize={10} fontWeight={800}>
                    H
                  </text>
                </g>
              );
            })
          : null}
      </svg>
    </div>
  );
}

function PriceLine({
  label,
  price,
  color,
  yFor,
  width,
  left,
  right,
  dash,
}: {
  label: string;
  price: number;
  color: string;
  yFor: (price: number) => number;
  width: number;
  left: number;
  right: number;
  dash?: boolean;
}) {
  if (!price) return null;
  const y = yFor(price);
  return (
    <g>
      <line x1={left} y1={y} x2={width - right} y2={y} stroke={color} strokeWidth={2} strokeDasharray={dash ? "6 4" : undefined} />
      <rect x={width - right + 8} y={y - 14} width={92} height={22} rx={4} fill={color} />
      <text x={width - right + 14} y={y + 1} fill="#fff" fontSize={11} fontWeight={800}>
        {label} {price.toFixed(3)}
      </text>
    </g>
  );
}

function num(value: unknown): number {
  const next = Number(value || 0);
  return Number.isFinite(next) ? next : 0;
}
