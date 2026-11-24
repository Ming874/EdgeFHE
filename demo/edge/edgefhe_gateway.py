#!/usr/bin/env python3
"""EdgeFHE demo gateway on the KV260, the owner side (research demonstration; not a medical diagnosis).

Per form, the gateway validates the answers, commits to them with a salted hash, writes a root-only CSV on tmpfs,
computes the invocation contract and the single-use PL permit, and starts the unchanged efnet_client as its direct
child (--backend pl --mode serial). The client generates a fresh CKKS key, encodes on the ARM, encrypts on the PL,
sends only ciphertext over TLS 1.3 (mutual authentication, two-sided pinning) and writes the decrypted logit to a
0600 score file. The gateway then checks the client report, the DMA interrupt counters and |logit_ckks - logit_plain|
<= 2e-3, maps the logit to a risk level (never a probability) and wipes the private files. The gateway itself never
opens a network socket, and it never signals a running client (an interrupted PL ENC quarantines the driver).

usage (as root; Python 3.10 standard library only; CFG defaults to /etc/edgefhe-demo/gateway.json):
  python3 -I -B edgefhe_gateway.py preflight  [--config CFG] [--backend pl|spec|cpu]
  python3 -I -B edgefhe_gateway.py submit     [--config CFG] --genhlth N --highbp N --bmi X --age N
                                              [--backend pl|spec|cpu] [--expect OWNER_CREATE] [--quiet]
                                              [--owner-events]
  python3 -I -B edgefhe_gateway.py gates      [--config CFG] --backends cpu,spec,pl [--expect OWNER_CREATE]
  python3 -I -B edgefhe_gateway.py throughput [--config CFG] [--backend pl] [--seconds 30]
  python3 -I -B edgefhe_gateway.py disclose   [--config CFG] REQUEST_SHA256|RUN
  python3 -I -B edgefhe_gateway.py serve      [--config CFG] [--rehearsal-listen ADDR] [--progress-ms N] [--quiet]

stdout carries machine-readable lines: 'EVT <envelope JSON>' per public event (demo/edge/EVENTS.md; none with
--quiet; owner-class events too only with submit --owner-events, for local debugging) and one final marker
(GATEWAY_PREFLIGHT_PASS, GATEWAY_SUBMIT_RESULT, GATEWAY_GATES_PASS, ...); stderr carries the zh-TW messages.
submit shows the risk level of the answers on its own terminal (stderr and GATEWAY_SUBMIT_RESULT): it belongs to the
owner domain and must never run on a console attached to the cloud PC, such as the board's serial console during
setup. exit status: 0 pass; 1 a run or check failed; 2 the form was refused locally (no process, no network);
3 the gateway did not start a run (not root, busy, PID 1, device held); 4 usage, configuration or model error.
"""
import argparse
import glob
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

from efgw import config, model, permit, records, runner  # noqa: E402

EXIT_PASS, EXIT_FAIL, EXIT_REFUSED, EXIT_NOT_STARTED, EXIT_USAGE = 0, 1, 2, 3, 4
DISCLAIMER = '本展示為研究用途，並非醫療診斷；本系統僅顯示風險等級，不顯示機率。'
TOOL_DIRS = ('/usr/sbin', '/usr/bin', '/sbin', '/bin')
_lock = threading.Lock()


def out(line):
    with _lock:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


def say(text):
    with _lock:
        sys.stderr.write(text + '\n')
        sys.stderr.flush()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        say('GATEWAY_USAGE %s' % message)
        sys.exit(EXIT_USAGE)


def tool(name):
    for directory in TOOL_DIRS:
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return None


# ------------------------------------------------------------------------------------------------- helpers
def make_gateway(cfg, quiet=False, owner_events=False):
    """The gateway of one command; its events go to stdout as 'EVT' lines: public events only, owner-class events
    (answers, normalized values, logits, level) too only with owner_events, none at all with quiet."""
    gateway = runner.Gateway(cfg)
    if not quiet:
        classes = ('owner', 'public') if owner_events else ('public',)

        def print_event(envelope):
            if envelope.get('class') in classes:
                out('EVT ' + records.dumps(envelope))
        gateway.events.add_listener(print_event)

    def on_signal(signum):
        gateway.request_stop(signum)
        notice = ('\nGATEWAY_STOP_REQUESTED signal=%s：本閘道將等待執行中的加密程式自行結束後再停止，'
                  '操作人員不得再次中斷或強制終止本閘道。\n' % runner._signal_name(signum))
        try:
            os.write(2, notice.encode('utf-8'))
        except OSError:
            pass

    runner.install_stop_signals(on_signal)
    return gateway


def finish(gateway, code):
    if gateway.stop_signal is not None:
        out('GATEWAY_STOP_REQUESTED signal=%s' % runner._signal_name(gateway.stop_signal))
    return code


def form_value(text, kind):
    """A command-line answer as the JSON value the HTTP API would carry (the form rules decide the rest)."""
    if re.fullmatch(r'[+-]?[0-9]{1,9}', text):
        return int(text)
    if kind == 'number' and re.fullmatch(r'[+-]?[0-9]{1,6}\.[0-9]{1,12}', text):
        return float(text)
    return text


def irq_text(delta):
    return '/'.join('%+d' % delta[k] for k in sorted(delta)) if delta else '-'


_SUPERSCRIPT = str.maketrans('-0123456789', '⁻⁰¹²³⁴⁵⁶⁷⁸⁹')


def sci(x, digits=2):
    """x as 'a×10ⁿ' with `digits` significant digits, as the owner screen writes it (demo/ui/lib.mjs fmtSci):
    2.4e-06 -> '2.4×10⁻⁶', 0.002 -> '2×10⁻³'."""
    if type(x) not in (int, float) or not math.isfinite(x):
        return '—'
    if x == 0:
        return '0'
    e = math.floor(math.log10(abs(x)))
    m = float('%.*g' % (digits, x / 10.0 ** e))
    if abs(m) >= 10:
        m, e = m / 10, e + 1
    if abs(m) < 1:
        m, e = m * 10, e - 1
    return '%s×10%s' % ('%.*g' % (digits, m), str(e).translate(_SUPERSCRIPT))


def refusal_exit(gateway, error):
    say(error.message())
    out('GATEWAY_RUN_NOT_STARTED code=%s' % error.code)
    return finish(gateway, EXIT_NOT_STARTED)


def say_failure(message):
    """The one-sentence message of a failed run, then the fixed no-retry line (EVENTS.md section 9)."""
    if message:
        say(message)
    say(runner.NO_RETRY_LINE)


def say_retention(outcome):
    """What happened to the private files of a form (the wiped event and its retained_private)."""
    wiped = outcome.get('wiped')
    if wiped and wiped.get('problems'):
        say('本閘道未能清除此筆表單的部分私有檔案（%s），操作人員應以 root 手動刪除這些檔案。'
            % '、'.join(wiped['problems']))
    if outcome.get('retained_private'):
        say('本閘道已將此筆表單的私有紀錄保存於板端 tmpfs，直到執行 disclose 或板端重新開機為止。')
    elif wiped is None:
        say('本閘道未寫入此筆表單的私有檔案，且未保存任何私有紀錄。')
    elif not wiped.get('problems'):
        say('本閘道已以零覆寫並刪除此筆表單的私有檔案，且未保存任何私有紀錄。')
    else:
        say('本閘道未保存此筆表單的私有紀錄。')


# ------------------------------------------------------------------------------------------------- submit
def cmd_submit(args, cfg):
    gateway = make_gateway(cfg, args.quiet, args.owner_events)
    inference = gateway.inference
    raw = {'GenHlth': args.genhlth, 'HighBP': args.highbp, 'BMI': args.bmi, 'Age': args.age}
    kinds = {f['name']: f['kind'] for f in inference.fields}
    form = {name: form_value(text, kinds[name]) for name, text in raw.items()}
    try:
        outcome = gateway.run_form(form, args.backend, expect_stop=args.expect)
    except runner.GatewayError as e:
        return refusal_exit(gateway, e)
    run = outcome['run']
    if outcome['refused']:
        say(outcome['message'])
        say(inference.form.get('refusal_note', ''))
        out('GATEWAY_SUBMIT_REFUSED run=%s code=%s' % (run, outcome['refused']))
        return finish(gateway, EXIT_REFUSED)
    if args.expect:
        if outcome['ok']:
            say('加密程式已接受 FPGA 呼叫許可，並因 FPGA 裝置尚未載入而以 %s 停止（符合預期）。' % args.expect)
            out('GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE run=%s backend=%s' % (run, outcome['backend']))
            return finish(gateway, EXIT_PASS)
        say_failure(outcome['message'])
        out('GATEWAY_SUBMIT_FAILED run=%s code=%s' % (run, outcome['code']))
        return finish(gateway, EXIT_FAIL)
    result = outcome['result']
    if outcome['ok']:
        if result['level'] is not None:
            say('本次推論已完成：風險等級為%s（%s）。' % (result['level_name'], result['level_label']))
        else:
            say('本次推論已完成：%s。' % result['boundary_text'])
        say('CKKS 與明文模型相差 %s（容許 %s）。' % (sci(result['abs_error']), sci(result['max_abs_error'])))
        say(DISCLAIMER)
        say_retention(outcome)
        boundary = '%d/%d' % tuple(result['boundary']) if result['boundary'] else '-'
        out('GATEWAY_SUBMIT_RESULT run=%s backend=%s level=%s boundary=%s abs_error=%.6e passed=1 '
            'request_sha256=%s response_payload_sha256=%s commitment=%s'
            % (run, outcome['backend'], result['level'] if result['level'] is not None else '-', boundary,
               result['abs_error'], outcome['request_sha256'], outcome['response_payload_sha256'],
               outcome['commitment']))
        return finish(gateway, EXIT_PASS)
    say_failure(outcome['message'])
    say_retention(outcome)
    if outcome['stop']:
        out('GATEWAY_SUBMIT_STOP run=%s code=%s' % (run, outcome['stop']))
    else:
        out('GATEWAY_SUBMIT_FAILED run=%s code=%s' % (run, outcome['code']))
    return finish(gateway, EXIT_FAIL)


# -------------------------------------------------------------------------------------------- gates, window
def gate_fields(outcome):
    report = outcome['report'] or {}
    fields = ['backend=%s' % outcome['backend'], 'status=%s' % report.get('status'),
              'max_public_error=%s' % report.get('max_public_error'),
              'requests_over_5e4=%s' % report.get('requests_over_5e4'),
              'device_calls=%s' % report.get('device_calls'),
              'completed_requests=%s' % report.get('completed_requests')]
    if isinstance(report.get('records_per_s'), (int, float)):
        fields.append('records_per_s=%.0f' % report['records_per_s'])
    if outcome['backend'] == 'pl':
        fields += ['irq_delta=%s' % irq_text(outcome['irq_delta']), 'PL_hz=%s' % report.get('PL_hz')]
    return fields


def cmd_gates(args, cfg):
    backends = [b for b in args.backends.split(',') if b]
    if not backends or len(set(backends)) != len(backends) or any(b not in config.BACKENDS for b in backends):
        say('GATEWAY_USAGE --backends 必須是 cpu、spec、pl 中不重複的項目，以逗號分隔。')
        return EXIT_USAGE
    if args.expect and backends != ['pl']:
        say('GATEWAY_USAGE --expect OWNER_CREATE 只能搭配 --backends pl。')
        return EXIT_USAGE
    gateway = make_gateway(cfg, args.quiet)
    summary, worst = [], 0.0
    for backend in backends:
        if gateway.stopping:
            say(runner.describe('GATEWAY_STOPPING'))
            out('GATEWAY_GATES_FAIL backend=%s code=GATEWAY_STOPPING' % backend)
            return finish(gateway, EXIT_FAIL)
        try:
            outcome = gateway.run_gate(backend, expect_stop=args.expect)
        except runner.GatewayError as e:
            return refusal_exit(gateway, e)
        if args.expect:
            if outcome['ok']:
                say('加密程式已接受 FPGA 呼叫許可，並因 FPGA 裝置尚未載入而以 %s 停止（符合預期）。' % args.expect)
                out('GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE run=%s backend=pl' % outcome['run'])
                return finish(gateway, EXIT_PASS)
            say_failure(outcome['message'])
            out('GATEWAY_GATES_FAIL backend=pl code=%s' % outcome['code'])
            return finish(gateway, EXIT_FAIL)
        out('GATEWAY_GATE ' + ' '.join(gate_fields(outcome)))
        if not outcome['ok']:
            say_failure(outcome['message'])
            out('GATEWAY_GATES_FAIL backend=%s code=%s' % (backend, outcome['code']))
            return finish(gateway, EXIT_FAIL)
        report = outcome['report']
        worst = max(worst, report['max_public_error'])
        if backend != 'cpu':
            summary.append('%s.device_calls=%d' % (backend, report['device_calls']))
        if backend == 'pl':
            summary += ['pl.irq_delta=%s' % irq_text(outcome['irq_delta']), 'pl.PL_hz=%d' % report['PL_hz']]
    say('公開測試資料的推論誤差均在 2×10⁻³ 以內，裝置呼叫次數與中斷增量皆符合預期。')
    out('GATEWAY_GATES_PASS backends=%s max_public_error=%s %s' % (','.join(backends), worst, ' '.join(summary)))
    return finish(gateway, EXIT_PASS)


def cmd_throughput(args, cfg):
    if not 1 <= args.seconds <= 60:
        say('GATEWAY_USAGE --seconds 必須介於 1 與 60 之間（client.cpp:134 的 --window-ms 上限為 60000）。')
        return EXIT_USAGE
    gateway = make_gateway(cfg, args.quiet)
    backend = args.backend or cfg['default_backend']
    try:
        outcome = gateway.run_throughput(backend, args.seconds)
    except runner.GatewayError as e:
        return refusal_exit(gateway, e)
    if not outcome['ok']:
        say_failure(outcome['message'])
        out('GATEWAY_THROUGHPUT_FAIL backend=%s code=%s' % (backend, outcome['code']))
        return finish(gateway, EXIT_FAIL)
    report = outcome['report']
    fields = gate_fields(outcome) + ['rows=%d' % report['rows'], 'window_ms=%d' % (args.seconds * 1000),
                                     'mode=pipeline', 'slots=%d' % report['slots'], 'inflight=%d' % report['inflight']]
    out('GATEWAY_THROUGHPUT_PASS ' + ' '.join(f for f in fields if not f.startswith('status=')))
    return finish(gateway, EXIT_PASS)


# ------------------------------------------------------------------------------------------------- disclose
def cmd_disclose(args, cfg):
    if os.geteuid() != 0:
        say(runner.describe('ROOT_REQUIRED'))
        out('GATEWAY_RUN_NOT_STARTED code=ROOT_REQUIRED')
        return EXIT_NOT_STARTED
    key = args.key
    if records.HEX64.fullmatch(key):
        existing = os.path.join(cfg['disclosure_dir'], key + '.json')
        if os.path.isfile(existing):
            out('GATEWAY_DISCLOSURE_WRITTEN request_sha256=%s path=%s existed=1' % (key, existing))
            return EXIT_PASS
    store = records.PrivateStore(os.path.join(cfg['workdir'], 'private'))
    record = store.find(key)
    if record is None:
        say('本閘道找不到此請求的私有紀錄（%s），可能的原因為設定值 retain_private 為 false、此紀錄已揭露過，或板端已重新'
            '開機（私有紀錄只保存在 tmpfs 上）。' % key)
        out('GATEWAY_DISCLOSURE_NOT_FOUND key=%s' % key)
        return EXIT_FAIL
    try:
        path, existed = records.write_disclosure(cfg['disclosure_dir'], record)
    except (OSError, ValueError) as e:
        say('本閘道無法寫出揭露檔（%s）。' % e)
        out('GATEWAY_DISCLOSURE_FAILED key=%s' % key)
        return EXIT_FAIL
    try:
        store.remove(record['run'])
    except OSError as e:
        say(runner.describe('WIPE_FAILED', path=store.path(record['run']), why=e))
    say('本閘道已寫出虛構表單的揭露檔（0600）；私有紀錄已清除。')
    out('GATEWAY_DISCLOSURE_WRITTEN request_sha256=%s run=%s path=%s existed=%d'
        % (record.get('request_sha256'), record['run'], path, int(existed)))
    return EXIT_PASS


# ------------------------------------------------------------------------------------------------ preflight
def mount_of(path, mounts_text):
    """(mount point, fs type) of the mount that contains path (the longest matching mount point)."""
    best = ('', '')
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), parts[1])
        prefix = point.rstrip('/') + '/'
        if (path == point or path.startswith(prefix) or point == '/') and len(point) >= len(best[0]):
            best = (point, parts[2])
    return best


def cert_window(path):
    """(notBefore, notAfter) as Unix seconds; the standard ssl decoder, else openssl x509."""
    import ssl
    try:
        info = ssl._ssl._test_decode_cert(path)
        return ssl.cert_time_to_seconds(info['notBefore']), ssl.cert_time_to_seconds(info['notAfter'])
    except Exception:
        openssl = tool('openssl')
        if openssl is None:
            raise
        text = subprocess.run([openssl, 'x509', '-in', path, '-noout', '-startdate', '-enddate'],
                              capture_output=True, text=True, timeout=30, check=True).stdout
        found = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        return ssl.cert_time_to_seconds(found['notBefore']), ssl.cert_time_to_seconds(found['notAfter'])


def root_owned_file(path, executable=False):
    st = os.stat(path)
    problems = []
    if not stat.S_ISREG(st.st_mode):
        problems.append('不是一般檔案')
    if st.st_uid != 0:
        problems.append('擁有者不是 root（uid %d）' % st.st_uid)
    if st.st_mode & 0o022:
        problems.append('群組或其他使用者可寫入（%o）' % (st.st_mode & 0o777))
    if executable and not os.access(path, os.X_OK):
        problems.append('不可執行')
    return problems


def preflight_checks(cfg, backend):
    """[(name, ok, detail)]; reads files and kernel counters only, never opens a network connection."""
    checks = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, '%s: %s' % (type(e).__name__, e)
        checks.append((name, ok, detail))

    def uid():
        return os.getuid() == 0 and os.geteuid() == 0, 'uid=%d euid=%d' % (os.getuid(), os.geteuid())

    def python():
        return sys.version_info >= (3, 10), sys.version.split()[0]

    def config_file():
        problems = root_owned_file(cfg['config_path'])
        return not problems, '%s %s' % (cfg['config_path'], '；'.join(problems) or 'root 擁有且他人不可寫入')

    def workdir():
        with open('/proc/self/mounts', encoding='utf-8', errors='replace') as f:
            point, fstype = mount_of(cfg['workdir'], f.read())
        ok = fstype == 'tmpfs'
        if os.path.lexists(cfg['workdir']):
            st = os.lstat(cfg['workdir'])
            ok = ok and stat.S_ISDIR(st.st_mode) and st.st_uid == 0
        return ok, '%s 位於 %s（%s）' % (cfg['workdir'], point, fstype)

    def boot_id():
        return True, permit.read_boot_id()

    def binaries():
        lines, ok = [], True
        for key, executable in (('client', True), ('plugin', False)):
            problems = root_owned_file(cfg[key], executable)
            ok = ok and not problems
            lines.append('%s sha256=%s %s%s' % (key, runner.file_sha256(cfg[key]), cfg[key],
                                                 ' ' + '；'.join(problems) if problems else ''))
        libs = sorted(glob.glob(os.path.join(cfg['app_dir'], 'libef_app_*.so*')))
        if not libs:
            ok = False
            lines.append('app_dir %s 中沒有 libef_app_*.so' % cfg['app_dir'])
        for lib in libs:
            problems = root_owned_file(lib)
            ok = ok and not problems
            lines.append('%s sha256=%s%s' % (os.path.basename(lib), runner.file_sha256(lib),
                                              ' ' + '；'.join(problems) if problems else ''))
        return ok, ' | '.join(lines)

    def overlay():
        path = cfg['overlay_record']
        if not path:
            return False, '設定檔未指定 overlay_record（make_overlay.py 寫出的 <out>.OVERLAY_RECORD.json）'
        import json
        with open(path, encoding='utf-8') as f:
            record = json.load(f)
        values = set()

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str):
                values.add(node)
        walk(record)
        tree = record.get('staged_tree_id')
        ok = cfg['profile_hex'] in values and model.MODEL_SHA256 in values and records.HEX64.fullmatch(tree or '')
        return bool(ok), 'staged_tree_id=%s profile=%s model=%s' % (
            tree, 'OK' if cfg['profile_hex'] in values else '不符', 'OK' if model.MODEL_SHA256 in values else '不符')

    def form_model():
        inference = model.Inference.load(cfg['model_json'], cfg['form_json'])
        bad = []
        for preset in inference.presets():
            want = preset['expected']
            refused = inference.refusal(preset['form'])
            if refused != want['refused']:
                bad.append(preset['id'])
                continue
            if refused:
                continue
            x = inference.csv_values(inference.csv_text(preset['form']))
            logit = inference.reference(x)
            level, boundary = inference.classify(logit)
            if logit != want['logit_plain'] or level != want['level'] or boundary != want['boundary']:
                bad.append(preset['id'])
        return not bad, 'model sha256=%s presets=%d%s' % (inference.model_sha256, len(inference.presets()),
                                                          ' 不符：' + ','.join(bad) if bad else ' 全部相符')

    def pki():
        problems = []
        for key in ('ca', 'cert', 'key'):
            if not os.path.isfile(cfg[key]):
                problems.append('%s 不存在：%s' % (key, cfg[key]))
        if os.path.isfile(cfg['key']):
            mode = os.stat(cfg['key']).st_mode & 0o777
            if mode & 0o077:
                problems.append('私鑰權限為 %o（須為 0600）' % mode)
        return not problems, '；'.join(problems) or 'ca、cert、key 存在，私鑰權限 0600'

    def pins():
        detail = 'peer_sha256=%s… profile_hex=%s…' % (cfg['peer_sha256'][:16], cfg['profile_hex'][:16])
        pin_file = os.path.join(os.path.dirname(cfg['ca']), 'server.pin')
        if os.path.isfile(pin_file):
            with open(pin_file, encoding='ascii') as f:
                value = f.read().strip()
            if value != cfg['peer_sha256']:
                return False, detail + '；server.pin 與 peer_sha256 不符'
            detail += '；server.pin 相符'
        return True, detail

    def certificates():
        now = time.time()
        parts, ok = [], True
        for key in ('ca', 'cert'):
            start, end = cert_window(cfg[key])
            inside = start <= now <= end
            ok = ok and inside
            parts.append('%s %s..%s%s' % (key, time.strftime('%Y-%m-%d', time.gmtime(start)),
                                          time.strftime('%Y-%m-%d', time.gmtime(end)), '' if inside else ' 不涵蓋目前時間'))
        return ok, '板上時鐘 %s UTC；%s' % (time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)), '；'.join(parts))

    def dma():
        node = cfg['dma_node']
        st = os.stat(node)
        holders = runner.dma_holders(node)
        lines = runner.irq_snapshot(cfg['proc_interrupts'])
        is_char = stat.S_ISCHR(st.st_mode)
        ok = is_char and not holders and len(lines) == runner.DMA_LINES
        described = ', '.join('%s (%s) total=%d' % (k, v['desc'], v['count']) for k, v in sorted(lines.items()))
        return ok, '%s 字元裝置=%s 其他持有者=%s；DMA 中斷線 %d 條（預期 %d）：%s' % (
            node, is_char, holders or '無', len(lines), runner.DMA_LINES, described)

    def route():
        ip = tool('ip')
        if ip is None:
            return False, '找不到 ip 指令'
        r = subprocess.run([ip, 'route', 'get', cfg['host']], capture_output=True, text=True, timeout=10)
        text = (r.stdout or r.stderr).strip().splitlines()
        return r.returncode == 0, text[0] if text else 'ip route get %s 沒有輸出' % cfg['host']

    for name, fn in (('uid', uid), ('python', python), ('config_file', config_file), ('workdir_tmpfs', workdir),
                     ('boot_id', boot_id), ('binaries', binaries), ('overlay_record', overlay),
                     ('model_and_presets', form_model), ('pki', pki), ('pins', pins),
                     ('certificates', certificates)):
        check(name, fn)
    if backend == 'pl':
        check('dma', dma)
    check('route', route)
    return checks


def cmd_preflight(args, cfg):
    backend = args.backend or cfg['default_backend']
    checks = preflight_checks(cfg, backend)
    for name, ok, detail in checks:
        out('PREFLIGHT_%s %s %s' % ('OK' if ok else 'FAIL', name, detail))
    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        say('預檢未通過的項目為 %s，操作人員應依操作手冊修正後再執行（預檢不會開啟任何網路連線）。' % '、'.join(failed))
        out('GATEWAY_PREFLIGHT_FAIL failed=%s backend=%s' % (','.join(failed), backend))
        return EXIT_FAIL
    out('GATEWAY_PREFLIGHT_PASS checks=%d backend=%s' % (len(checks), backend))
    return EXIT_PASS


# ---------------------------------------------------------------------------------------------------- serve
def cmd_serve(args, cfg, rest):
    try:
        from efgw import httpapi
    except ImportError as e:
        say('本閘道找不到 HTTP API 模組（efgw/httpapi.py）：%s' % e)
        out('GATEWAY_SERVE_UNAVAILABLE')
        return EXIT_USAGE
    gateway = runner.Gateway(cfg)
    return httpapi.serve(gateway, cfg, rehearsal_listen=args.rehearsal_listen, progress_ms=args.progress_ms,
                         argv=rest, quiet=args.quiet)


# ----------------------------------------------------------------------------------------------------- main
def build_parser():
    parser = Parser(prog='edgefhe_gateway.py', description='EdgeFHE demo gateway (KV260 owner side)')
    sub = parser.add_subparsers(dest='command', required=True)

    def common(p):
        p.add_argument('--config', default=config.DEFAULT_PATH)
        p.add_argument('--quiet', action='store_true', help='no EVT lines on stdout')
        return p

    common(sub.add_parser('preflight')).add_argument('--backend', choices=config.BACKENDS)
    p = common(sub.add_parser('submit'))
    for name in ('--genhlth', '--highbp', '--bmi', '--age'):
        p.add_argument(name, required=True)
    p.add_argument('--backend', choices=config.BACKENDS)
    p.add_argument('--expect', choices=runner.EXPECTABLE_STOPS)
    p.add_argument('--owner-events', action='store_true',
                   help='also print owner-class EVT lines (answers, logits, level); local debugging on the owner '
                        'console only, never on a console attached to the cloud PC')
    p = common(sub.add_parser('gates'))
    p.add_argument('--backends', required=True)
    p.add_argument('--expect', choices=runner.EXPECTABLE_STOPS)
    p = common(sub.add_parser('throughput'))
    p.add_argument('--backend', choices=config.BACKENDS)
    p.add_argument('--seconds', type=int, default=30)
    common(sub.add_parser('disclose')).add_argument('key', metavar='REQUEST_SHA256|RUN')
    p = common(sub.add_parser('serve'))
    p.add_argument('--rehearsal-listen')
    p.add_argument('--progress-ms', type=int)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if argv and argv[0] == 'serve':
        args, rest = parser.parse_known_args(argv)
    else:
        args, rest = parser.parse_args(argv), []
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        say(e.message())
        out('GATEWAY_CONFIG_INVALID code=%s' % e.code)
        return EXIT_USAGE
    try:
        if args.command == 'preflight':
            return cmd_preflight(args, cfg)
        if args.command == 'submit':
            return cmd_submit(args, cfg)
        if args.command == 'gates':
            return cmd_gates(args, cfg)
        if args.command == 'throughput':
            return cmd_throughput(args, cfg)
        if args.command == 'disclose':
            return cmd_disclose(args, cfg)
        return cmd_serve(args, cfg, rest)
    except model.ModelError as e:
        say('本閘道無法載入模型或表單定義：%s' % e)
        out('GATEWAY_MODEL_INVALID')
        return EXIT_USAGE
    except OSError as e:
        say('本閘道無法完成此指令（%s）。' % runner._oserror(e))
        out('GATEWAY_OS_ERROR')
        return EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
