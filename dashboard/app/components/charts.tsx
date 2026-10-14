// Small hand-drawn charts (SVG and CSS) so the dashboard needs no chart library.
import type { Confusion } from '@/lib/data';

export const pct = (value: number, digits = 1) => `${(value * 100).toFixed(digits)}%`;
export const num = (value: number) => value.toLocaleString('en-US');

const SUPERSCRIPT: Record<string, string> = { '-': '⁻', '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹' };

export function sci(value: number, digits = 1) {
  if (value === 0) return '0';
  const [mantissa, exponent] = value.toExponential(digits).split('e');
  const power = String(Number(exponent)).replace(/[-0-9]/g, (c) => SUPERSCRIPT[c]);
  return `${mantissa} × 10${power}`;
}

export function Stat({ label, value, note, tone }: { label: string; value: string; note?: string; tone?: 'good' | 'warn' }) {
  return (
    <div className={`stat ${tone ?? ''}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {note ? <span className="stat-note">{note}</span> : null}
    </div>
  );
}

// Horizontal bars; `domain` sets the value that maps to an empty and a full bar.
export function BarList({
  rows,
  domain,
  format,
}: {
  rows: { label: string; value: number; highlight?: boolean; detail?: string }[];
  domain: [number, number];
  format: (value: number) => string;
}) {
  const [low, high] = domain;
  return (
    <ul className="bars">
      {rows.map((row) => (
        <li key={row.label} className={row.highlight ? 'highlight' : undefined}>
          <span className="bar-label">
            {row.label}
            {row.detail ? <small>{row.detail}</small> : null}
          </span>
          <span className="bar-track">
            <span className="bar-fill" style={{ width: `${Math.max(1, ((row.value - low) / (high - low)) * 100)}%` }} />
          </span>
          <span className="bar-value">{format(row.value)}</span>
        </li>
      ))}
    </ul>
  );
}

export function RocChart({ series }: { series: { name: string; points: [number, number][]; dashed?: boolean; auc: number }[] }) {
  const size = 280;
  const pad = 36;
  const inner = size - pad - 12;
  const x = (v: number) => pad + v * inner;
  const y = (v: number) => 12 + (1 - v) * inner;
  return (
    <figure className="roc">
      <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label="ROC 曲線：驗證集與測試集">
        <rect x={pad} y={12} width={inner} height={inner} className="roc-frame" />
        {[0.25, 0.5, 0.75].map((t) => (
          <g key={t} className="roc-grid">
            <line x1={x(t)} x2={x(t)} y1={12} y2={12 + inner} />
            <line x1={pad} x2={pad + inner} y1={y(t)} y2={y(t)} />
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} className="roc-chance" />
        {series.map((s, i) => (
          <polyline
            key={s.name}
            points={s.points.map(([fpr, tpr]) => `${x(fpr)},${y(tpr)}`).join(' ')}
            className={`roc-line roc-line-${i}`}
            strokeDasharray={s.dashed ? '5 4' : undefined}
          />
        ))}
        <text x={pad + inner / 2} y={size - 4} className="roc-axis" textAnchor="middle">誤報率（1 − 特異度）</text>
        <text x={10} y={12 + inner / 2} className="roc-axis" textAnchor="middle" transform={`rotate(-90 10 ${12 + inner / 2})`}>
          召回率
        </text>
      </svg>
      <figcaption>
        {series.map((s, i) => (
          <span key={s.name} className={`legend legend-${i}`}>
            {s.name}　AUC {s.auc.toFixed(3)}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

export function ConfusionMatrix({ title, data }: { title: string; data: Confusion }) {
  const total = data.tn + data.fp + data.fn + data.tp;
  const cell = (value: number, label: string, kind: string) => (
    <td className={kind}>
      <strong>{num(value)}</strong>
      <span>{label}</span>
      <span className="share">{pct(value / total)}</span>
    </td>
  );
  return (
    <table className="confusion">
      <caption>{title}</caption>
      <thead>
        <tr>
          <th />
          <th>預測：無</th>
          <th>預測：有</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th>實際：無</th>
          {cell(data.tn, '正確排除', 'right')}
          {cell(data.fp, '誤報', 'wrong')}
        </tr>
        <tr>
          <th>實際：有</th>
          {cell(data.fn, '漏抓', 'wrong')}
          {cell(data.tp, '正確抓到', 'right')}
        </tr>
      </tbody>
    </table>
  );
}

// One column per batch, height relative to the largest value.
export function Sparkbars({ values, label }: { values: number[]; label: string }) {
  const top = Math.max(...values);
  return (
    <div className="sparkbars" role="img" aria-label={label}>
      {values.map((v, i) => (
        <span key={i} style={{ height: `${Math.max(4, (v / top) * 100)}%` }} title={`第 ${i + 1} 批：${sci(v)}`} />
      ))}
    </div>
  );
}
