// Group A (pure software) vs group B (hardware/software) from tools/compare_results.py.
import { BarList, Stat, num } from './charts';
import type { Comparison as ComparisonData } from '@/lib/data';

const STEPS: [string, string][] = [
  ['standardize', '① 標準化'],
  ['encode', '② 編碼'],
  ['sample', '③ 取樣'],
  ['encrypt', '④⑤ 加密'],
  ['cloud', '⑥–⑧ 雲端'],
  ['decrypt', '⑨ 解密'],
  ['decode', '⑩ 判斷'],
];

const groupOf = (backend: string) => (backend.startsWith('cpu-') ? 'A' : backend.startsWith('fpga-') ? 'B' : '?');

const BANNERS: Record<'real' | 'simulated' | 'mixed' | 'none', { tone: string; text: string }> = {
  real: { tone: 'good', text: 'KV260 實板量測（FPGA 列是實際 DMA 與 PL 時間）' },
  simulated: { tone: 'warn', text: 'FPGA 列來自模擬裝置：時間是 CPU 時間，不是 FPGA 時間。' },
  mixed: { tone: 'warn', text: '實板與模擬裝置的 FPGA 結果混在一起：標「模擬」的列不是 FPGA 時間，也不會被選為最佳 B。' },
  none: { tone: 'info', text: '這份報表只有組 A（純軟體）的結果，沒有 FPGA 列。' },
};
const times = (value: number) => `${value.toFixed(2)}×`;

export default function Comparison({ file, data }: { file: string; data: ComparisonData }) {
  const latency = data.latency_rows;
  const longest = Math.max(0, ...data.throughput_rows.map((r) => r.batches));
  // Longest stream only, and per configuration only its best pipeline setting.
  const full = Object.values(
    data.throughput_rows
      .filter((r) => r.batches === longest)
      .reduce<Record<string, ComparisonData['throughput_rows'][number]>>((best, r) => {
        if (!best[r.label] || r.records_per_s > best[r.label].records_per_s) best[r.label] = r;
        return best;
      }, {}),
  );
  const slowest = Math.max(1e-9, ...latency.map((r) => r.edge_p50_ms));
  const fastest = Math.max(1e-9, ...full.map((r) => r.records_per_s));
  const env = data.environment;
  const hasB = data.has_group_b ?? [...latency, ...data.throughput_rows].some((r) => groupOf(r.backend) === 'B');
  const device = data.fpga_device ?? (!hasB ? 'none' : data.real_fpga ? 'real' : 'simulated');
  const banner = BANNERS[device];
  const mark = (r: { backend: string; label: string; real_device?: boolean }) =>
    r.label + (groupOf(r.backend) === 'B' && r.real_device === false ? '（模擬）' : '');
  return (
    <section className="card">
      <h2>兩組對照：純軟體（組 A）vs 軟硬體協同（組 B）</h2>
      <p className="lead">
        同一份 {num(data.inputs.records)} 筆測試資料、同一把金鑰與參數，只換多項式乘法由誰算。來源：results/{file}
      </p>
      <p className={`banner ${banner.tone}`}>{banner.text}</p>
      <section className="stats" aria-label="對照摘要">
        {data.correctness ? (
          <Stat
            label={hasB ? '正確性（所有設定逐位元相同）' : '正確性（組 A 各設定逐位元相同）'}
            value={data.correctness.bit_exact && data.correctness.all_pass ? '通過' : '未通過'}
            note="密文與解密結果的 SHA-256 一致"
            tone={data.correctness.bit_exact && data.correctness.all_pass ? 'good' : 'warn'}
          />
        ) : null}
        {data.latency ? (
          <>
            <Stat label="單批延遲：最佳 A" value={`${data.latency.best_A_ms.toFixed(1)} ms`} note={data.latency.best_A} />
            <Stat label="單批延遲：最佳 B" value={`${data.latency.best_B_ms.toFixed(1)} ms`} note={data.latency.best_B} />
            <Stat
              label="延遲：B 比 A 快"
              value={times(data.latency.B_speedup_over_A)}
              note={data.latency.B_speedup_over_A > 1 ? 'B 較快' : 'B 較慢'}
              tone={data.latency.B_speedup_over_A > 1 ? 'good' : 'warn'}
            />
          </>
        ) : null}
        {data.throughput ? (
          <Stat
            label={`吞吐量 B ÷ A（${data.throughput.batches} 批）`}
            value={times(data.throughput.B_over_A)}
            note={`${num(Math.round(data.throughput.best_B_records_per_s))} vs ${num(Math.round(data.throughput.best_A_records_per_s))} 筆/秒`}
            tone={data.throughput.B_over_A > 1 ? 'good' : 'warn'}
          />
        ) : null}
        {data.energy ? (
          <Stat
            label="每焦耳筆數 B ÷ A"
            value={times(data.energy.B_over_A)}
            note={`感測器 ${data.energy.sensor}`}
            tone={data.energy.B_over_A > 1 ? 'good' : 'warn'}
          />
        ) : null}
      </section>
      <div className="grid" style={{ marginTop: 20 }}>
        {latency.length ? (
          <div className="span-6">
            <h2>單批邊緣延遲（p50，越短越好）</h2>
            <BarList
              rows={latency.map((r) => ({
                label: mark(r),
                detail: `組 ${groupOf(r.backend)}`,
                value: r.edge_p50_ms,
                highlight: groupOf(r.backend) === 'B',
              }))}
              domain={[0, slowest]}
              format={(v) => `${v.toFixed(1)} ms`}
            />
          </div>
        ) : null}
        {full.length ? (
          <div className="span-6">
            <h2>吞吐量（連續 {longest} 批，各設定最佳，越高越好）</h2>
            <BarList
              rows={full.map((r) => ({
                label: `${mark(r)} ${r.pipeline === 'serial' ? '串行' : `管線(${r.capacity})`}`,
                detail: `組 ${groupOf(r.backend)}`,
                value: r.records_per_s,
                highlight: groupOf(r.backend) === 'B',
              }))}
              domain={[0, fastest]}
              format={(v) => `${num(Math.round(v))} 筆/秒`}
            />
          </div>
        ) : null}
      </div>
      {latency.length ? (
        <div className="table-wrap">
          <table>
            <caption>每個步驟的 p50（ms）：和簡報資料流圖的編號相同</caption>
            <thead>
              <tr>
                <th>設定</th>
                {STEPS.map(([key, name]) => (
                  <th key={key} className="num">
                    {name}
                  </th>
                ))}
                <th className="num">邊緣合計</th>
              </tr>
            </thead>
            <tbody>
              {latency.map((r) => (
                <tr key={r.label}>
                  <td>
                    {mark(r)} <small>組 {groupOf(r.backend)}</small>
                  </td>
                  {STEPS.map(([key]) => (
                    <td key={key} className="num">
                      {r.steps_p50_ms[key] === undefined ? '—' : r.steps_p50_ms[key].toFixed(2)}
                    </td>
                  ))}
                  <td className="num">{r.edge_p50_ms.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="note">
        機器：{env.machine}，{env.cpu_model}，{env.online_cpus} 核，綁核 {env.affinity}（{env.pinned ? '固定' : '未固定'}）。公平檢查：
        {data.fairness_problems.length ? data.fairness_problems.join('；') : '通過（同資料、同模型、同參數、同機器、同量測設定）'}。
        邊緣合計不含雲端步驟。
      </p>
    </section>
  );
}
