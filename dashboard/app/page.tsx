import { connection } from 'next/server';
import AutoRefresh from './components/AutoRefresh';
import Comparison from './components/Comparison';
import { BarList, ConfusionMatrix, RocChart, Sparkbars, Stat, num, pct, sci } from './components/charts';
import { featureName, loadDashboard } from '@/lib/data';
import type { CkksCheck, DatasetManifest, TrainingReport } from '@/lib/data';

const SPLIT_NAMES: Record<string, string> = { train: '訓練', validation: '驗證', test: '測試' };
const PHASE_NAMES: Record<string, string> = {
  encode: '編碼',
  sample: '取樣',
  encrypt: '加密',
  cloud_affine: '雲端計算',
  decrypt_decode: '解密與解碼',
};

function Missing({ title, command }: { title: string; command: string }) {
  return (
    <section className="card missing">
      <h2>{title}</h2>
      <p className="lead">還沒有產生這份結果。在 C:/GitHub/Grad 執行：</p>
      <pre>{command}</pre>
    </section>
  );
}

function Headline({ report, ckks }: { report: TrainingReport; ckks: CkksCheck | null }) {
  const test = report.metrics.test;
  const certain = ckks ? ckks.decisions.agree + ckks.decisions.disagree : 0;
  return (
    <section className="stats" aria-label="重點指標">
      <Stat label="測試集 AUC" value={test.auc.toFixed(3)} note={`驗證集 ${report.metrics.validation.auc.toFixed(3)}`} />
      <Stat label="召回率（抓到的糖尿病患者）" value={pct(test.sensitivity)} note={`${num(test.confusion.tp)} / ${num(test.confusion.tp + test.confusion.fn)} 人`} />
      <Stat label="特異度（正確排除）" value={pct(test.specificity)} note={`誤報 ${num(test.confusion.fp)} 人`} />
      {ckks ? (
        <>
          <Stat
            label="明文與 CKKS 判斷一致"
            value={pct(certain ? ckks.decisions.agree / certain : 0, 2)}
            note={`${ckks.decisions.disagree} 筆不一致、${ckks.decisions.uncertain} 筆不確定`}
            tone={ckks.decisions.disagree === 0 ? 'good' : 'warn'}
          />
          <Stat
            label="最大加密誤差"
            value={sci(ckks.max_abs_logit_diff)}
            note={`上限 ${sci(ckks.error_bound)}`}
            tone={ckks.all_within_bound ? 'good' : 'warn'}
          />
        </>
      ) : null}
    </section>
  );
}

function Dataset({ manifest }: { manifest: DatasetManifest }) {
  return (
    <section className="card span-5">
      <h2>資料集</h2>
      <p className="lead">
        {manifest.dataset}（美國 CDC BRFSS 健康調查），依標籤分層切分，種子 {manifest.seed}
      </p>
      <table>
        <thead>
          <tr>
            <th>份</th>
            <th className="num">筆數</th>
            <th className="num">糖尿病比例</th>
            <th className="num">2048 筆批次</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(manifest.splits).map(([name, split]) => (
            <tr key={name}>
              <td>{SPLIT_NAMES[name] ?? name}</td>
              <td className="num">{num(split.rows)}</td>
              <td className="num">{pct(split.positive_rate, 2)}</td>
              <td className="num">{split.full_2048_batches}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">
        來源：<a href={manifest.page}>UCI 891</a>。授權請到原始 Kaggle 頁面確認後再公開發表。
      </p>
    </section>
  );
}

function Selection({ report }: { report: TrainingReport }) {
  const top = Math.max(...report.single_feature.map((f) => f.validation_auc));
  return (
    <section className="card span-5">
      <h2>特徵挑選</h2>
      <p className="lead">單一特徵在驗證集上的 AUC；深色是最後選中的 4 個（逐步挑選，每步加入讓 AUC 最高的特徵）</p>
      <BarList
        rows={report.single_feature.map((f) => ({
          label: featureName(f.feature),
          detail: f.feature,
          value: f.validation_auc,
          highlight: report.features.includes(f.feature),
        }))}
        domain={[0.5, Math.max(top, 0.51)]}
        format={(v) => v.toFixed(3)}
      />
      <ol className="steps">
        {report.selection_path.map((step) => (
          <li key={step.step}>
            加入 <strong>{featureName(step.added)}</strong> → 驗證 AUC {step.validation_auc.toFixed(4)}{' '}
            <small>
              （次佳：{featureName(step.runner_up)} {step.runner_up_auc.toFixed(4)}）
            </small>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Model({ report }: { report: TrainingReport }) {
  const checks = report.system_checks;
  const clipped = Object.values(checks.clipped_rows).reduce((a, b) => a + b, 0);
  return (
    <section className="card span-7">
      <h2>模型參數（{report.model_id}）</h2>
      <p className="lead">權重與偏差給雲端；平均與標準差留在 KV260 做標準化</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>特徵</th>
              <th className="num">平均</th>
              <th className="num">標準差</th>
              <th className="num">權重</th>
            </tr>
          </thead>
          <tbody>
            {report.features.map((feature, i) => (
              <tr key={feature}>
                <td>
                  {featureName(feature)} <small>{feature}</small>
                </td>
                <td className="num">{report.mean[i].toFixed(3)}</td>
                <td className="num">{report.std[i].toFixed(3)}</td>
                <td className="num">{report.weights[i].toFixed(4)}</td>
              </tr>
            ))}
            <tr>
              <td>偏差</td>
              <td />
              <td />
              <td className="num">{report.bias.toFixed(4)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="formula">logit = Σ 權重 × z + 偏差，z = (x − 平均) ÷ 標準差；logit &gt; 0 判為「有」</p>
      <ul className="checks">
        <li className={checks.max_abs_weight <= 16 ? '' : 'fail'}>權重絕對值 ≤ 16（最大 {checks.max_abs_weight.toFixed(3)}）</li>
        <li className={checks.abs_bias <= 16 ? '' : 'fail'}>偏差絕對值 ≤ 16（{checks.abs_bias.toFixed(3)}）</li>
        <li className={checks.error_bound <= checks.max_logit_error ? '' : 'fail'}>
          CKKS 最壞誤差 {sci(checks.error_bound)} ≤ 上限 {sci(checks.max_logit_error)}
        </li>
        <li className={clipped === 0 ? '' : 'fail'}>標準化後超出 ±{checks.input_abs_max} 而被截斷的筆數：{clipped}</li>
      </ul>
    </section>
  );
}

function Performance({ report, ckks }: { report: TrainingReport; ckks: CkksCheck | null }) {
  const { validation, test } = report.metrics;
  const rows: [string, (m: typeof test) => string, string | null][] = [
    ['AUC', (m) => m.auc.toFixed(4), ckks ? ckks.ckks_metrics.auc.toFixed(4) : null],
    ['準確率', (m) => pct(m.accuracy), ckks ? pct(ckks.ckks_metrics.accuracy_certain) : null],
    ['召回率', (m) => pct(m.sensitivity), ckks ? pct(ckks.ckks_metrics.sensitivity) : null],
    ['特異度', (m) => pct(m.specificity), ckks ? pct(ckks.ckks_metrics.specificity) : null],
    ['精確率', (m) => pct(m.precision), null],
  ];
  return (
    <section className="card span-7">
      <h2>模型表現</h2>
      <p className="lead">
        只有 {pct(1 - report.baseline_test_accuracy_all_negative)} 是陽性：全部猜「無」也有{' '}
        {pct(report.baseline_test_accuracy_all_negative)} 準確率，所以看 AUC 與召回率
      </p>
      <div className="perf">
        <RocChart
          series={[
            { name: '驗證', points: validation.roc, dashed: true, auc: validation.auc },
            { name: '測試', points: test.roc, auc: test.auc },
          ]}
        />
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>指標</th>
                <th className="num">驗證</th>
                <th className="num">測試</th>
                <th className="num">CKKS</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([label, pick, encrypted]) => (
                <tr key={label}>
                  <td>{label}</td>
                  <td className="num">{pick(validation)}</td>
                  <td className="num">{pick(test)}</td>
                  <td className="num">{encrypted ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="note">
            CKKS 欄：同一份測試集經加密推論後的結果{ckks ? `（不含 ${ckks.decisions.uncertain} 筆不確定）` : '（尚未執行）'}
          </p>
        </div>
      </div>
      <div className="matrices">
        <ConfusionMatrix title={`測試集（明文，${num(test.rows)} 筆）`} data={test.confusion} />
        {ckks ? <ConfusionMatrix title="測試集（CKKS，不含不確定）" data={ckks.ckks_metrics.confusion} /> : null}
      </div>
    </section>
  );
}

function Ckks({ ckks }: { ckks: CkksCheck }) {
  const phases = Object.entries(ckks.timing.phase_ms);
  const slowest = Math.max(...phases.map(([, ms]) => ms));
  return (
    <section className="card">
      <h2>CKKS 加密推論驗證</h2>
      <p className="lead">
        測試集 {num(ckks.records)} 筆分成 {ckks.batches} 批送進 repo 的 V4 C++ CKKS 程式：加密 → 雲端只用密文與公開模型計算 →
        解密，再和明文結果逐筆比對
      </p>
      <section className="stats" aria-label="CKKS 驗證摘要">
        <Stat label="判斷一致" value={num(ckks.decisions.agree)} note={`不一致 ${ckks.decisions.disagree}、不確定 ${ckks.decisions.uncertain}`} tone="good" />
        <Stat label="平均加密誤差" value={sci(ckks.mean_abs_logit_diff)} note={`最大 ${sci(ckks.max_abs_logit_diff)}`} />
        <Stat label="處理速度" value={`${num(Math.round(ckks.timing.records_per_second))} 筆/秒`} note={`總計 ${num(Math.round(ckks.timing.total_ms))} ms`} />
        <Stat label="AUC（CKKS／明文）" value={ckks.ckks_metrics.auc.toFixed(4)} note={`明文 ${ckks.ckks_metrics.plain_auc.toFixed(4)}`} />
      </section>
      <div className="grid" style={{ marginTop: 20 }}>
        <div className="span-6">
          <h2>各階段總時間</h2>
          <BarList
            rows={phases.map(([phase, ms]) => ({ label: PHASE_NAMES[phase] ?? phase, value: ms }))}
            domain={[0, slowest]}
            format={(v) => `${v.toFixed(0)} ms`}
          />
        </div>
        <div className="span-6">
          <h2>每批最大誤差</h2>
          <Sparkbars values={ckks.per_batch.map((b) => b.max_abs_logit_diff)} label="每一批加密結果與明文的最大差距" />
          <p className="note">
            每一批都遠小於上限 {sci(ckks.error_bound)}；滑過長條可看數值。
          </p>
        </div>
      </div>
      <p className="note">{ckks.timing.note}。產生時間 {ckks.generated_utc}。</p>
    </section>
  );
}

export default async function Page() {
  await connection(); // render on every request: the JSON files change when the scripts re-run
  const { manifest, report, ckks, comparison } = await loadDashboard();
  return (
    <main>
      <header className="top">
        <div>
          <p className="eyebrow">EDGEFHE · CDC 糖尿病模型</p>
          <h1>模型訓練、CKKS 驗證與兩組對照</h1>
        </div>
        <AutoRefresh seconds={10} />
      </header>

      {report ? <Headline report={report} ckks={ckks} /> : null}

      <div className="grid">
        {manifest ? (
          <Dataset manifest={manifest} />
        ) : (
          <Missing title="資料集" command="python -I tools/prepare_datasets.py --out data/cdc_diabetes" />
        )}
        {report ? <Model report={report} /> : null}
        {report ? <Selection report={report} /> : null}
        {report ? <Performance report={report} ckks={ckks} /> : null}
        {!report ? <Missing title="模型訓練" command="training/.venv/Scripts/python training/train.py" /> : null}
        {ckks ? (
          <Ckks ckks={ckks} />
        ) : (
          <Missing title="CKKS 加密推論驗證" command='wsl -- bash -c "cd /mnt/c/GitHub/Grad && python3 -I training/ckks_check.py"' />
        )}
        {comparison ? (
          <Comparison file={comparison.file} data={comparison.data} />
        ) : (
          <Missing
            title="兩組對照（組 A vs 組 B）"
            command="把板上 build/comparison/ 複製到 results/<日期>/，再在 repo 根目錄執行 tools/compare_results.py（見 results/README.md）"
          />
        )}
      </div>

      {report ? (
        <footer>
          訓練於 {report.generated_utc}，Python {report.environment.python}、scikit-learn {report.environment.scikit_learn}，耗時{' '}
          {report.seconds} 秒。方法：{report.method.selection}；{report.method.standardization}。
        </footer>
      ) : null}
    </main>
  );
}
