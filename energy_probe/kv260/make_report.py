"""Build report.html (figures + explanations + statistics + how to state the conclusion) from summary.json.
usage: python make_report.py <data dir> [earlier same-setup data dir] [earlier no-ethernet V8b data dir]"""
import json, math, os, statistics as st, sys, datetime
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

d = sys.argv[1]
prev = sys.argv[2] if len(sys.argv) > 2 else None
pre_eth = sys.argv[3] if len(sys.argv) > 3 else None
S = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
L = json.load(open(os.path.join(d, "runs.json"), encoding="utf-8"))
R = S["runs"]
V = ["V8b", "OpenFHE4", "OpenFHE1"]
NAME = {"V8b": "V8b（FPGA + ARM）", "OpenFHE1": "OpenFHE 單執行緒（純 ARM）", "OpenFHE4": "OpenFHE 4 執行緒（純 ARM）"}
SHORT = {"V8b": "V8b", "OpenFHE1": "OpenFHE 單執行緒", "OpenFHE4": "OpenFHE 4 執行緒"}
SW = {"V8b": "var(--blue)", "OpenFHE4": "var(--aqua)", "OpenFHE1": "var(--orange)"}
by = {v: [r for r in R if r["version"] == v] for v in V}
n = {v: len(by[v]) for v in V}

def m(v, k): return st.mean(r[k] for r in by[v])
def sd(v, k): return st.stdev(r[k] for r in by[v]) if n[v] > 1 else 0.0
def ci(v, k):
    h = stats.t.ppf(0.975, n[v] - 1) * sd(v, k) / math.sqrt(n[v]) if n[v] > 1 else 0.0
    return m(v, k) - h, m(v, k) + h
def ratio(a, b, k):
    """mean(a)/mean(b) with a 95% CI from the log-ratio (delta method, Welch df)."""
    ra, rb = sd(a, k) / m(a, k), sd(b, k) / m(b, k)
    va, vb = ra ** 2 / n[a], rb ** 2 / n[b]
    df = (va + vb) ** 2 / (va ** 2 / (n[a] - 1) + vb ** 2 / (n[b] - 1))
    h = stats.t.ppf(0.975, df) * math.sqrt(va + vb)
    r = m(a, k) / m(b, k)
    return r, r * math.exp(-h), r * math.exp(h)
def welch_p(a, b, k):
    return stats.ttest_ind([r[k] for r in by[a]], [r[k] for r in by[b]], equal_var=False).pvalue
def drift(v, k):
    xs = list(range(n[v])); ys = [r[k] for r in by[v]]
    sl = stats.linregress(xs, ys).slope if n[v] > 2 else 0.0
    return sl * (n[v] - 1) / m(v, k) * 100           # % change from first to last run along the fitted line
def f0(x): return f"{x:,.0f}"
def fW(x): return f"{x:.3f} W"
def pstr(p): return "&lt; 10<sup>−10</sup>" if p < 1e-10 else f"{p:.1e}"

t0 = datetime.datetime.utcfromtimestamp(L["idle"][0]["start"]); t1 = datetime.datetime.utcfromtimestamp(L["runs"][-1]["t_end"])
r4g = ratio("V8b", "OpenFHE4", "rpj_gross"); r1g = ratio("V8b", "OpenFHE1", "rpj_gross")
r4n = ratio("V8b", "OpenFHE4", "rpj_net"); r4s = ratio("V8b", "OpenFHE4", "rec_s")
r4p = ratio("V8b", "OpenFHE4", "P_run")
uj = {v: 1e6 / m(v, "rpj_gross") for v in V}; mj = {v: 2048e3 / m(v, "rpj_gross") for v in V}
inc = {v: m(v, "P_run") - m(v, "P_idle") for v in V}
idle_v8, idle_of = S["idle_W"]["V8b"], S["idle_W"]["OpenFHE"]
diffs = [abs(r["esp_rpj"] / r["rpj_gross"] - 1) * 100 for r in R]
worst_diff = max(diffs)
drifts = {v: drift(v, "rpj_gross") for v in V}
speed41 = m("OpenFHE4", "rec_s") / m("OpenFHE1", "rec_s")
nn = n["V8b"]

prev_line = ""
if prev and os.path.exists(os.path.join(prev, "summary.json")):
    P = json.load(open(os.path.join(prev, "summary.json"), encoding="utf-8"))["runs"]
    pv = {v: st.mean(r["rpj_gross"] for r in P if r["version"] == v) for v in V}
    dv = {v: (m(v, "rpj_gross") / pv[v] - 1) * 100 for v in V}
    prev_line = ("<li><b>可重現。</b>同日稍早在相同接線下各跑 3 輪的結果，與本次 " + str(nn) + " 輪平均相差："
                 + "、".join(f"{SHORT[v]} {dv[v]:+.1f}%" for v in V) + "。</li>")
pre_line = ""
if pre_eth and os.path.exists(os.path.join(pre_eth, "summary.json")):
    Q = json.load(open(os.path.join(pre_eth, "summary.json"), encoding="utf-8"))["runs"]
    q = [r for r in Q if r["version"] == "V8b"]
    pre_line = (f"<li><b>接上乙太網路後整板功率高約 1 W。</b>同日更早、尚未接乙太網路時，V8b 的每秒筆數相近（{f0(st.mean(r['rec_s'] for r in q))}），"
                f"加密時功率為 {st.mean(r['P_run'] for r in q):.3f} W，每焦耳筆數為 {f0(st.mean(r['rpj_gross'] for r in q))}。"
                "不同時段、不同接線的絕對數值不宜直接比較；同一次量測內的倍數不受影響。</li>")

CSS = """
  @page { size: A4; margin: 16mm 15mm 16mm 15mm; }
  :root { --ink:#1b1d21; --quiet:#52514e; --rule:#dcdad4; --tint:#f3f2ee; --blue:#2a78d6; --orange:#eb6834; --aqua:#1baf7a; --say:#eef4fc; }
  body { font-family: "Microsoft JhengHei", "Noto Sans TC", sans-serif; color: var(--ink); font-size: 10.5pt; line-height: 1.65; margin: 0; background: #fff; }
  h1 { font-size: 19pt; margin: 0 0 2pt; letter-spacing: .02em; }
  h2 { font-size: 13.5pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5px solid var(--ink); }
  h3 { font-size: 11.5pt; margin: 12pt 0 4pt; }
  p { margin: 4pt 0; }
  .sub { color: var(--quiet); font-size: 10pt; }
  table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 9.6pt; font-variant-numeric: tabular-nums; }
  th, td { border-bottom: 1px solid var(--rule); padding: 4pt 6pt; text-align: left; vertical-align: top; }
  th { background: var(--tint); font-weight: 700; }
  td.n, th.n { text-align: right; white-space: nowrap; }
  .key { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8pt; margin: 8pt 0 4pt; }
  .key div { background: var(--tint); border-radius: 6px; padding: 7pt 9pt; }
  .key b { display: block; font-size: 16pt; line-height: 1.2; font-variant-numeric: tabular-nums; }
  .key span { color: var(--quiet); font-size: 9.3pt; }
  figure { margin: 10pt 0 4pt; break-inside: avoid; }
  figure img { width: 100%; display: block; }
  .read { display: grid; grid-template-columns: 4.2em 1fr; gap: 2pt 8pt; margin: 4pt 0 12pt; font-size: 10pt; }
  .read dt { font-weight: 700; color: var(--quiet); }
  .read dd { margin: 0; }
  .fig { break-inside: avoid; }
  ul { margin: 4pt 0; padding-left: 18pt; } li { margin: 2pt 0; }
  .swatch { display: inline-block; width: 9pt; height: 9pt; border-radius: 2px; vertical-align: -1pt; margin-right: 3pt; }
  code { font-family: Consolas, monospace; font-size: 9.2pt; background: var(--tint); padding: 0 3pt; border-radius: 3px; }
  .say { background: var(--say); border-radius: 6px; padding: 8pt 11pt; margin: 6pt 0; }
  .say p { margin: 2pt 0; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 10pt; }
  .two > div { min-width: 0; }
  .pb { break-before: page; }
"""

def sw(v): return f'<span class="swatch" style="background:{SW[v]}"></span>'

rows = "".join(
    f"<tr><td>{sw(v)}{SHORT[v]}</td><td class='n'>{n[v]}</td><td class='n'>{f0(m(v,'rec_s'))}</td><td class='n'>{fW(m(v,'P_run'))}</td>"
    f"<td class='n'>{fW(m(v,'P_idle'))}</td><td class='n'>{f0(m(v,'rpj_gross'))}</td><td class='n'>{f0(ci(v,'rpj_gross')[0])}–{f0(ci(v,'rpj_gross')[1])}</td>"
    f"<td class='n'>{f0(m(v,'rpj_net'))}</td><td class='n'>{mj[v]:.1f} mJ</td><td class='n'>{m(v,'cpu')*100:.0f}%</td></tr>" for v in V)

html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>V8b 與 OpenFHE 能耗比較</title><style>{CSS}</style></head><body>

<h1>V8b 與 OpenFHE 加密能耗比較</h1>
<p class="sub">量測時間：{t0:%Y-%m-%d %H:%M}–{t1:%H:%M} UTC　每種設定 {nn} 輪，每輪 30 秒　平台：KV260（Ubuntu 22.04，4 × Cortex-A53 1.333 GHz）　量測：ESP32-S3 + INA228 整板探針（12 V 輸入端）</p>

<h2>摘要</h2>
<div class="key">
  <div><b>{r4g[0]:.2f} 倍</b><span>V8b 每焦耳加密筆數相對 OpenFHE 4 執行緒（95% 信賴區間 {r4g[1]:.2f}–{r4g[2]:.2f} 倍）</span></div>
  <div><b>{r4s[0]:.2f} 倍</b><span>V8b 每秒加密筆數相對 OpenFHE 4 執行緒</span></div>
  <div><b>{m('V8b','cpu')*100:.0f}% 對 {m('OpenFHE4','cpu')*100:.0f}%</b><span>加密時 4 顆 ARM 核心的平均使用率（V8b 對 OpenFHE 4 執行緒）</span></div>
</div>
<ul>
  <li>V8b（ARM 搭配 FPGA）每加密 2048 筆資料消耗約 <b>{mj['V8b']:.1f} mJ</b>；純 ARM 的 OpenFHE 4 執行緒約 {mj['OpenFHE4']:.1f} mJ，單執行緒約 {mj['OpenFHE1']:.1f} mJ。</li>
  <li>兩者的整板功率相近（{fW(m('V8b','P_run'))} 對 {fW(m('OpenFHE4','P_run'))}），能耗比的差距主要來自處理速度。</li>
  <li>扣除待機功率後的淨值，結論方向相同：V8b 為 OpenFHE 4 執行緒的 {r4n[0]:.1f} 倍。</li>
  <li>{nn} 輪之間的變異很小（V8b 標準差 {sd('V8b','rpj_gross')/m('V8b','rpj_gross')*100:.2f}%），探針螢幕即時算出的數值與電腦分析的差異全部在 {worst_diff:.2f}% 以內。</li>
</ul>

<h2>量測方法</h2>
<table>
  <tr><th style="width:21%">項目</th><th>內容</th></tr>
  <tr><td>比較對象</td><td>{sw('V8b')}V8b rev5：ARM 負責控制與搬運資料，FPGA 負責加密　{sw('OpenFHE4')}OpenFHE 1.5.1，4 執行緒　{sw('OpenFHE1')}OpenFHE 1.5.1，單執行緒（後兩者只用 ARM）</td></tr>
  <tr><td>共同參數</td><td>CKKS，N = 4096，每個密文 2048 個 slot，3 個 RNS 模數（約 88.6 位元），公鑰加密。OpenFHE 實際選出的模數為 1073692673／536813569／536903681，與 repo 文件（docs/NATIVE_V4.md 4.4 節）的 OpenFHE 對照組相同。</td></tr>
  <tr><td>工作定義</td><td>三種設定相同：每個 frame 8 個密文，8 筆明文事先編碼一次，迴圈內只做加密，連續 30 秒；第一個與最後一個 frame 皆檢查正確性（V8b 與規格模型逐位元比對；OpenFHE 解密誤差 &lt; 10<sup>−3</sup>，實測約 10<sup>−4</sup>）。</td></tr>
  <tr><td>筆數換算</td><td>每筆資料 4 個特徵（CDC 模型），每個密文裝 2048 筆的同一個特徵，故 1 個密文 = 512 筆。</td></tr>
  <tr><td>能量量測</td><td>INA228 串接於 KV260 的 12 V 輸入（整板），每 0.1 秒一筆；探針未校正（K = 1），兩種設定的比值不受校正影響。</td></tr>
  <tr><td>總值與淨值</td><td>總值 = 筆數 ÷ 這 30 秒的整板能量。淨值 = 筆數 ÷［（加密時功率 − 同一狀態的待機功率）× 秒數］。</td></tr>
  <tr><td>待機狀態</td><td>V8b：已載入 V8b 硬體與驅動，不執行加密（{fW(idle_v8)}）。OpenFHE：PL 清空，不載入任何 FPGA 設計，代表純 ARM 的部署方式（{fW(idle_of)}）。</td></tr>
  <tr><td>統計</td><td>每種設定 {nn} 輪；信賴區間以 t 分布計算，倍數的信賴區間以對數比值的誤差傳遞（Welch 自由度）計算。</td></tr>
</table>

<h2 class="pb">結論怎麼寫</h2>
<div class="say">
<p><b>建議寫法：</b>在相同 CKKS 參數（N = 4096、3 個 RNS 模數、約 88.6 位元）且只計公鑰加密的條件下，以外接探針量測 KV260 整板 12 V 輸入，將加密交給 FPGA 的 V8b（ARM + FPGA）每焦耳可加密 {f0(m('V8b','rpj_gross'))} 筆資料，為純 ARM 執行 OpenFHE 1.5.1（4 執行緒）的 {r4g[0]:.1f} 倍（95% 信賴區間 {r4g[1]:.1f}–{r4g[2]:.1f} 倍，各 {nn} 輪）；同時 CPU 使用率由 {m('OpenFHE4','cpu')*100:.0f}% 降到 {m('V8b','cpu')*100:.0f}%。</p>
</div>
<div class="two">
<div>
<h3>這份資料支持的說法</h3>
<ul>
  <li><b>加密這一步，ARM + FPGA 比純 ARM 的 OpenFHE 省電。</b>倍數的信賴區間下限 {r4g[1]:.1f} 倍遠大於 1，Welch t 檢定 p {pstr(welch_p('V8b','OpenFHE4','rpj_gross'))}。</li>
  <li><b>省電的原因是速度，不是功率。</b>兩者整板功率只差 {abs(r4p[0]-1)*100:.0f}%，V8b 的處理速度是 {r4s[0]:.1f} 倍。</li>
  <li><b>FPGA 設計本身的額外耗電很小。</b>載入 V8b 後的待機功率比 PL 清空只高 {(idle_v8-idle_of)*1000:.0f} mW。</li>
  <li><b>CPU 被釋放出來。</b>V8b 只用約 {m('V8b','cpu')*4:.1f} 顆核心，其餘可做編碼、網路等工作。</li>
  <li><b>量測可信。</b>整板外接量測、{nn} 輪變異小、兩種獨立計算方式一致（±{worst_diff:.2f}%）。</li>
</ul>
</div>
<div>
<h3>還不能宣稱的說法</h3>
<ul>
  <li><b>「整個系統省 {r4g[0]:.0f} 倍」。</b>本次只量加密；ARM 端的正規化與編碼、網路傳輸、雲端計分與解密都不在內。端到端的倍數會較小，需另外量測。</li>
  <li><b>「比任何純 ARM 實作都省電」。</b>本次對照的是 OpenFHE 這個通用函式庫；若要宣稱勝過純 ARM，應再比較團隊自己最佳化過的 ARM 版本。</li>
  <li><b>「安全性完全相同」。</b>參數規模相同（N、模數位元數、三元秘密金鑰），但雜訊分布與取樣實作不同，應寫成「參數規模相同」。</li>
  <li><b>其他 EdgeFHE 版本的結論。</b>本次只量 V8b rev5；repo 中較早的 V5 實板結果（每次 FPGA 呼叫有約 4 ms 額外負擔）方向相反，不可混用。</li>
</ul>
</div>
</div>
<h3>簡報可以用的一句話</h3>
<div class="say"><p>「同樣加密一批 2048 筆資料，V8b 用 {mj['V8b']:.1f} mJ，純 ARM 的 OpenFHE 用 {mj['OpenFHE4']:.0f} mJ；速度快 {r4s[0]:.0f} 倍，CPU 只用 {m('V8b','cpu')*100:.0f}%。」</p></div>

<h2 class="pb">圖表與說明</h2>
<div class="fig">
<figure><img src="figures/fig1_power_timeline.png" alt="整段量測的功率曲線"></figure>
<dl class="read">
  <dt>圖說</dt><dd>整段量測。灰色底為待機，藍、橘、綠色底分別是 V8b、OpenFHE 單執行緒、OpenFHE 4 執行緒的 30 秒持續加密，每種 {nn} 輪。</dd>
  <dt>怎麼看</dt><dd>線的高度是 KV260 整塊板子從 12 V 吃進去的功率。每一輪開始時功率跳上去、結束時落回待機，代表量到的是加密本身造成的變化。</dd>
  <dt>重點</dt><dd>同一種設定的 {nn} 輪高度幾乎相同。V8b 約 {m('V8b','P_run'):.2f} W、OpenFHE 4 執行緒約 {m('OpenFHE4','P_run'):.2f} W、單執行緒約 {m('OpenFHE1','P_run'):.2f} W；功率差距不大，但處理速度差很多（圖 3）。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig2_records_per_joule.png" alt="每焦耳處理筆數"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每消耗 1 焦耳的整板能量可以加密多少筆資料。長條為 {nn} 輪平均，白點為每一輪。</dd>
  <dt>怎麼看</dt><dd>越長越省電。白點集中在長條末端，代表各輪之間差異很小。</dd>
  <dt>重點</dt><dd>V8b 為 {f0(m('V8b','rpj_gross'))} 筆／焦耳，是 OpenFHE 4 執行緒（{f0(m('OpenFHE4','rpj_gross'))}）的 {r4g[0]:.1f} 倍、單執行緒（{f0(m('OpenFHE1','rpj_gross'))}）的 {r1g[0]:.1f} 倍。換算成每筆資料：V8b 約 {uj['V8b']:.1f} µJ，OpenFHE 4 執行緒約 {uj['OpenFHE4']:.1f} µJ，單執行緒約 {uj['OpenFHE1']:.1f} µJ。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig3_throughput.png" alt="每秒處理筆數"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每秒能加密的資料筆數。</dd>
  <dt>怎麼看</dt><dd>越長越快。這張圖與圖 2 的形狀幾乎一樣，因為三種設定的功率相近，每焦耳筆數主要由速度決定。</dd>
  <dt>重點</dt><dd>V8b 每秒 {m('V8b','rec_s')/1e4:.0f} 萬筆，是 OpenFHE 4 執行緒（{m('OpenFHE4','rec_s')/1e4:.1f} 萬筆）的 {r4s[0]:.1f} 倍。OpenFHE 從單執行緒到 4 執行緒加速 {speed41:.1f} 倍，接近線性，代表純 ARM 版本已經用滿 4 顆核心。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig4_power_breakdown.png" alt="功率拆解"></figure>
<dl class="read">
  <dt>圖說</dt><dd>加密時的整板功率拆成兩段：灰色是同一狀態下不加密時的待機功率，彩色是加密多出來的功率。</dd>
  <dt>怎麼看</dt><dd>灰色段遠長於彩色段：板子本身的待機耗電約 {idle_of:.1f} W，加密只多出 {min(inc.values()):.2f} 至 {max(inc.values()):.2f} W。</dd>
  <dt>重點</dt><dd>V8b 加密時多出 {inc['V8b']:.2f} W，OpenFHE 4 執行緒多出 {inc['OpenFHE4']:.2f} W。V8b 用較少的額外功率完成約 {r4s[0]:.0f} 倍的工作量。由於待機占大部分，整板總值會低估加密本身的效率差距，因此另外計算淨值（圖 5）。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig5_gross_vs_net.png" alt="總值與淨值"></figure>
<dl class="read">
  <dt>圖說</dt><dd>左邊是整板總值（同圖 2），右邊是扣掉待機功率後的淨值。兩張圖的橫軸刻度不同。</dd>
  <dt>怎麼看</dt><dd>總值回答「這塊板子實際每焦耳做多少事」；淨值回答「加密這件事本身每焦耳做多少事」。</dd>
  <dt>重點</dt><dd>兩種算法都是 V8b 最高。淨值下 V8b 約 {m('V8b','rpj_net')/1e4:.0f} 萬筆／焦耳，是 OpenFHE 4 執行緒（{m('OpenFHE4','rpj_net')/1e4:.1f} 萬）的 {r4n[0]:.1f} 倍。淨值的分母是較小的差值，誤差會被放大，主要結論以總值為準。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig6_cpu.png" alt="CPU 使用率"></figure>
<dl class="read">
  <dt>圖說</dt><dd>加密期間 4 顆 ARM 核心的平均使用率（取每輪開始後第 5 至 25 秒）。</dd>
  <dt>怎麼看</dt><dd>100% 代表 4 顆核心全滿；25% 約等於 1 顆核心全滿。</dd>
  <dt>重點</dt><dd>V8b 只用約 {m('V8b','cpu')*100:.0f}% 的 CPU（約 {m('V8b','cpu')*4:.1f} 顆核心，負責搬運資料與處理 DMA 中斷），其餘 CPU 可以做其他工作。OpenFHE 4 執行緒用滿全部核心，速度仍只有 V8b 的 {1/r4s[0]*100:.0f}%。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig7_probe_vs_pc.png" alt="探針螢幕與電腦分析的一致性"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每一輪的每焦耳筆數由探針螢幕與電腦分析各算一次，點是兩者的差異百分比。</dd>
  <dt>怎麼看</dt><dd>探針螢幕用 INA228 晶片內建的能量累積器（連續累積），電腦分析用每 0.1 秒一筆的功率取平均；兩種算法的取樣方式不同。點越靠近 0，兩者越一致。</dd>
  <dt>重點</dt><dd>{len(R)} 輪全部在 ±{worst_diff:.2f}% 以內，兩種算法互相印證，量測流程沒有漏算或重算能量。</dd>
</dl>
</div>

<h2>結果總表</h2>
<table>
  <tr><th>設定</th><th class="n">輪數</th><th class="n">每秒筆數</th><th class="n">加密時功率</th><th class="n">待機功率</th><th class="n">每焦耳筆數（總值）</th><th class="n">95% 信賴區間</th><th class="n">淨值</th><th class="n">每 2048 筆</th><th class="n">CPU</th></tr>
  {rows}
</table>
<table>
  <tr><th>比較（V8b ÷ 對照組）</th><th class="n">倍數</th><th class="n">95% 信賴區間</th><th class="n">Welch t 檢定 p 值</th></tr>
  <tr><td>每焦耳筆數（總值），對 OpenFHE 4 執行緒</td><td class="n">{r4g[0]:.2f}</td><td class="n">{r4g[1]:.2f}–{r4g[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rpj_gross'))}</td></tr>
  <tr><td>每焦耳筆數（總值），對 OpenFHE 單執行緒</td><td class="n">{r1g[0]:.1f}</td><td class="n">{r1g[1]:.1f}–{r1g[2]:.1f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE1','rpj_gross'))}</td></tr>
  <tr><td>每焦耳筆數（淨值），對 OpenFHE 4 執行緒</td><td class="n">{r4n[0]:.2f}</td><td class="n">{r4n[1]:.2f}–{r4n[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rpj_net'))}</td></tr>
  <tr><td>每秒筆數，對 OpenFHE 4 執行緒</td><td class="n">{r4s[0]:.2f}</td><td class="n">{r4s[1]:.2f}–{r4s[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rec_s'))}</td></tr>
</table>

<h2>限制與注意事項</h2>
<ul>
  <li><b>探針未校正。</b>絕對數值可能有約 1% 的偏差（主要來自分流電阻公差）；同一次量測內的比較與倍數不受影響。</li>
  {pre_line}
  {prev_line}
  <li><b>量測順序固定。</b>依序跑完 V8b 的 {nn} 輪，再跑 OpenFHE。各設定第 1 輪到第 {nn} 輪的趨勢變化：{"、".join(f"{SHORT[v]} {drifts[v]:+.2f}%" for v in V)}，遠小於兩者的差距。</li>
  <li><b>只量加密。</b>三種設定都不含 ARM 端的正規化與編碼、雲端計分與解密；端到端的每焦耳筆數會比本表低。</li>
  <li><b>待機狀態不同。</b>V8b 的待機包含已載入的 FPGA 設計；OpenFHE 的待機為 PL 清空。兩者相差 {(idle_v8-idle_of)*1000:.0f} mW。</li>
  <li><b>同一個 frame 重複加密。</b>三種設定都把同一批明文反覆加密；加密的計算量與明文內容無關，但實際應用中每批資料都不同。</li>
  <li><b>OpenFHE 的用法。</b>4 執行緒版本以 OpenMP 平行加密不同的密文，OpenFHE 內部的平行區段不再巢狀展開；這是本次採用的設定，不代表 OpenFHE 的所有最佳化方式。</li>
</ul>

<h2>資料與程式位置</h2>
<table>
  <tr><th style="width:28%">內容</th><th>位置</th></tr>
  <tr><td>原始資料</td><td>資料夾 <code>{os.path.basename(os.path.normpath(d))}</code> 的 <code>probe.csv</code>（探針每 0.1 秒一筆）、<code>runs.json</code>（每輪板子輸出與時間點）</td></tr>
  <tr><td>分析結果與圖表</td><td>同一資料夾的 <code>summary.json</code>、<code>figures/*.png</code>、<code>figures/summary_table.csv</code></td></tr>
  <tr><td>量測與分析工具</td><td>repository 的 <code>energy_probe/kv260/</code>（見其中的 README.md）</td></tr>
  <tr><td>板上程式</td><td>V8b：<code>~/edgefhe-v8b-r5/test/board_enc_check sustain 30 8</code>；OpenFHE：<code>~/openfhe_bench/build/openfhe_sustain 30 8 {{1|4}}</code>（OpenFHE 1.5.1 安裝於 <code>~/.local/openfhe-v1.5.1</code>）</td></tr>
</table>
</body></html>
"""
open(os.path.join(d, "report.html"), "w", encoding="utf-8").write(html)
print("report.html written")
print(f"ratio gross x4 {r4g}, x1 {r1g}, net x4 {r4n}, speed x4 {r4s}, worst diff {worst_diff:.2f}%, drifts {drifts}")
