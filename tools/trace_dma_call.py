"""Split one FPGA product call into its parts with the kernel's own tracer (group B).

The driver's elapsed_us covers only "DMA issued -> both channels done": about 0.54 ms of the
4.7 ms each call takes on the KV260 (docs/COMPARISON.md). This tool shows where the rest goes
without changing the frozen V3 driver. ftrace's function_graph tracer times edgefhe_ioctl,
edgefhe_submit_transfer and the kernel functions it calls; four kprobe events mark points inside
edgefhe_submit_transfer that no function call separates (after the input copy, after the
canonical-residue check, after clearing the output buffer, after the output copy). The offsets
were read from the prebuilt module with llvm-objdump and are used only when the loaded module
matches that build; otherwise the coarser function-level split is reported.

Board, as root after the live gates (docs/BOARD_GUIDE.md); the operator opens the device and
this tool only inherits the descriptor:
  sudo bash -c 'exec 3<>/dev/edgefhe_rns3_n4096_dma && cd ~/Grad &&
    PYTHONDONTWRITEBYTECODE=1 python3 tools/trace_dma_call.py --fd 3 --bin build/comparison/stage2_03/bin \\
      --data data/cdc_diabetes/splits/test.csv --model training/outputs/model.json \\
      --preprocess training/outputs/edge_preprocess.json --cpus 0-3 --output build/trace/dma_trace_01'
Anywhere: --parse saved/trace.txt re-analyses a saved trace; --self-test checks the parser.

The workload is v5_e2e (latency mode, one FPGA backend) on the inherited descriptor: ordinary
op2 requests, one in flight, once untraced (baseline) and once traced. As in run_comparison.py,
nothing here opens, resets or retries the device, and the first failure stops the run. The
tracing state is saved first and restored afterwards, also on failure; only this tool's own
kprobe events are added and removed. Standard library only.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
DEVICE = '/dev/edgefhe_rns3_n4096_dma'
MODULE = 'edgefhe_rns3_n4096_dma_proxy'
PREBUILT_KO = ROOT / 'firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/edgefhe_rns3_n4096_dma_proxy.ko'
# The one module build the kprobe offsets below belong to.
BUILD = {
    'kernel': '5.15.0-1027-xilinx-zynqmp',
    'srcversion': '6BDCA74AD872B2765D36882',
    'ko_sha256': '3590ae35109364c6d97bc7c8cb2220a005f2577ec96868d1741d3a336478d84e',
    # .text distances between functions, compared with /proc/kallsyms of the loaded module
    'layout': (('edgefhe_wipe_buffers', 'edgefhe_submit_transfer', 0x9a0),
               ('edgefhe_submit_transfer', 'edgefhe_ioctl', 0x5f0)),
}
GROUP = 'edgefhe_trace'
# kprobe events in edgefhe_submit_transfer: (event, offset, instruction there, what has just finished)
MARKS = (
    ('copied_in', 0xf0, 'ldr w0, [x20, #0x8]', '_copy_from_user returned'),
    ('validated', 0x1b8, 'ldr x0, [x23, #0xb0]', 'canonical-residue loop done'),
    ('cleared', 0x1cc, 'ldr x0, [x23, #0xa0]', 'memset of the RX buffer returned'),
    ('copied_out', 0x52c, 'mov x21, x0', '__arch_copy_to_user returned'),
)
# Only these functions are hooked; the graph starts at edgefhe_ioctl and stops at depth 3.
FILTER = ('edgefhe_ioctl', 'edgefhe_submit_transfer', 'edgefhe_wipe_buffers', 'mutex_lock', 'mutex_unlock',
          '__check_object_size', '__init_swait_queue_head', 'sg_init_table', '__msecs_to_jiffies', 'ktime_get',
          'wait_for_completion_timeout', 'completion_done')
REQUIRED = ('edgefhe_ioctl', 'edgefhe_submit_transfer', 'edgefhe_wipe_buffers')
OPTIONAL_GLOBS = ('xilinx_dma_*',)
OPTIONS = {'funcgraph-abstime': '1', 'funcgraph-tail': '1', 'funcgraph-irqs': '0', 'funcgraph-proc': '1',
           'funcgraph-cpu': '1', 'funcgraph-duration': '1', 'funcgraph-overhead': '1', 'sleep-time': '1'}
# (segment, what happens, bytes through the uncached DMA buffers or 0)
FINE = (
    ('enter', 'copy the 48-byte request in, take the driver mutex', 0),
    ('checks', 'request checks, hardened-usercopy object check', 0),
    ('copy_in', 'copy_from_user: 128 KiB into the uncached TX buffer', 131072),
    ('validate', 'canonical-residue check: 32,768 uncached 32-bit loads', 131072),
    ('clear_out', 'memset: 64 KiB of the uncached RX buffer', 65536),
    ('dma_setup', 'AXI-Lite registers, two descriptors prepared and submitted', 0),
    ('dma_wait', "DMA both ways + PL compute (the driver's elapsed_us window)", 196608),
    ('copy_out', 'copy_to_user: 64 KiB from the uncached RX buffer', 65536),
    ('wipe', 'memzero_explicit: 192 KiB TX + 64 KiB RX, uncached', 262144),
    ('leave', 'mutex unlock, copy the request back', 0),
)
COARSE = (
    FINE[0], FINE[1],
    ('copy_validate_clear', 'copy 128 KiB in, check it, clear 64 KiB (uncached), AXI-Lite registers', 0),
    ('dma_setup', 'two descriptors prepared and submitted', 0),
    FINE[6], FINE[7], FINE[8], FINE[9],
)

# ---------------------------------------------------------------- parsing (no root needed)
# function_graph line with funcgraph-abstime, -cpu, -proc, -duration, -overhead:
#   "  612.513484 |   2)  v5_e2e-1234  |   0.740 us    |    mutex_lock();"
LINE = re.compile(r'^\s*(?P<ts>\d+\.\d+)\s*\|\s*(?P<cpu>\d+)\)\s*(?:(?P<task>[^|]*?-\d+)\s*\|)?'
                  r'\s*(?:[+!#*@$]\s*)?(?:(?P<dur>\d+(?:\.\d+)?)\s*us\s*)?\|(?P<body>.*)$')
FUNC = r'(?P<name>[\w.$]+)(?: \[[\w-]+\])?'
ENTRY = re.compile('^' + FUNC + r'\(\) \{$')
LEAF = re.compile('^' + FUNC + r'\(\);$')
EXIT = re.compile(r'^\}(?: /\* ' + FUNC + r' \*/)?$')
MARK = re.compile(r'^/\* (?P<event>\w+): \(')


def parse_calls(text, root='edgefhe_ioctl'):
    """Group the graph lines into calls of `root`, per task. Times in microseconds."""
    open_calls, calls, lines = {}, [], 0
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        lines += 1
        seconds, fraction = m['ts'].split('.')
        ts = int(seconds) * 1000000 + int((fraction + '000000')[:6])  # exact microseconds
        dur = float(m['dur']) if m['dur'] else None
        task = (m['task'] or '').strip() or '*'
        body = m['body'].strip()
        call = open_calls.get(task)
        entry, leaf, done, mark = ENTRY.match(body), LEAF.match(body), EXIT.match(body), MARK.match(body)
        if entry:
            if call is not None:
                call['stack'].append((entry['name'], ts))
            elif entry['name'] == root:
                open_calls[task] = {'task': task, 'start': ts, 'stack': [(root, ts)], 'items': [], 'errors': []}
        elif call is None:
            continue
        elif leaf and dur is not None:
            call['items'].append((leaf['name'], ts, ts + dur))
        elif done:
            name, started = call['stack'].pop()
            if done['name'] and done['name'] != name:
                call['errors'].append(f'end of {done["name"]} while {name} was open')
            if call['stack']:
                call['items'].append((name, started, ts))
            else:
                call['end'] = ts
                calls.append(call)
                del open_calls[task]
        elif mark:
            call['items'].append(('@' + mark['event'], ts, ts))
    return calls, lines, len(open_calls)


def spans(items, name, lo=-math.inf, hi=math.inf):
    return [(s, e) for n, s, e in items if n == name and s >= lo and e <= hi]


def split_call(call):
    """One call -> ('fine' | 'coarse', {segment: microseconds}) or (None, reason)."""
    if call['errors']:
        return None, call['errors'][0]
    items, t0, t1 = call['items'], call['start'], call['end']
    submit = spans(items, 'edgefhe_submit_transfer')
    if len(submit) != 1:
        return None, f'edgefhe_submit_transfer seen {len(submit)} times'
    s0, s1 = submit[0]
    lock = spans(items, 'mutex_lock', hi=s0)
    b_lock = lock[-1][1] if lock else s0
    check = spans(items, '__check_object_size', s0, s1)
    b_check = check[0][1] if check else s0
    clock = spans(items, 'ktime_get', s0, s1)
    waits = spans(items, 'wait_for_completion_timeout', s0, s1)
    if len(clock) == 2:  # the driver's own start/end of elapsed_us
        dma_start, dma_end = clock[0][1], clock[1][0]
    elif waits:
        dma_start, dma_end = waits[0][0], waits[-1][1]
    else:
        return None, 'no DMA wait (failed request?)'
    wipe = spans(items, 'edgefhe_wipe_buffers', s0, s1)
    if len(wipe) != 1:
        return None, f'edgefhe_wipe_buffers seen {len(wipe)} times'
    marks = {name: [s for n, s, _ in items if n == '@' + name] for name, *_ in MARKS}
    if all(len(found) == 1 for found in marks.values()):
        mode, table = 'fine', FINE
        chain = [t0, b_lock, b_check, marks['copied_in'][0], marks['validated'][0], marks['cleared'][0],
                 dma_start, dma_end, marks['copied_out'][0], wipe[0][1], t1]
    elif any(marks.values()):
        return None, 'kprobe marks seen ' + ', '.join(f'{k}x{len(v)}' for k, v in marks.items())
    else:
        mode, table = 'coarse', COARSE
        swait = spans(items, '__init_swait_queue_head', b_check, dma_start)
        setup = swait[0][0] if swait else dma_start
        chain = [t0, b_lock, b_check, setup, dma_start, dma_end, wipe[0][0], wipe[0][1], t1]
    steps = [b - a for a, b in zip(chain, chain[1:])]
    if min(steps) < -1.0:  # stamps are printed to 1 us; anything larger is out of order
        return None, 'boundaries out of order'
    return mode, {key: max(0.0, step) for (key, _, _), step in zip(table, steps)}


def percentile(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(p * (len(sorted_values) - 1) + 0.5))]


def analyse(text):
    calls, lines, unfinished = parse_calls(text)
    split = [split_call(c) for c in calls]
    skipped = {}
    for mode, value in split:
        if mode is None:
            skipped[value] = skipped.get(value, 0) + 1
    modes = [mode for mode, _ in split if mode]
    result = {'graph_lines': lines, 'calls': len(calls), 'unfinished_calls': unfinished, 'skipped': skipped}
    if not modes:
        result.update(mode=None, complete_calls=0, segments=[])
        return result
    mode = max(set(modes), key=modes.count)
    rows = [value for m, value in split if m == mode]
    table = FINE if mode == 'fine' else COARSE
    totals = sorted(sum(r.values()) for r in rows)
    mean_total = sum(totals) / len(totals)
    segments = []
    for key, meaning, size in table:
        values = sorted(r[key] for r in rows)
        mean = sum(values) / len(values)
        median = percentile(values, 0.5)
        row = {'segment': key, 'meaning': meaning, 'median_us': round(median, 1),
               'p10_us': round(percentile(values, 0.1), 1), 'p90_us': round(percentile(values, 0.9), 1),
               'mean_us': round(mean, 1), 'share_of_mean': round(mean / mean_total, 4) if mean_total else 0}
        if size and median > 0:
            row.update(bytes=size, mb_per_s=round(size / median, 1))  # bytes per us = MB/s
        if key == 'validate' and median > 0:
            row['ns_per_load'] = round(median * 1000 / 32768, 1)
        segments.append(row)
    outside = sorted(sum(v for k, v in r.items() if k != 'dma_wait') for r in rows)
    result.update(mode=mode, mode_note='per-call mode differs' if len(set(modes)) > 1 else '',
                  complete_calls=len(rows), segments=segments,
                  total={'median_us': round(percentile(totals, 0.5), 1), 'mean_us': round(mean_total, 1),
                         'p10_us': round(percentile(totals, 0.1), 1), 'p90_us': round(percentile(totals, 0.9), 1)},
                  outside_dma_median_us=round(percentile(outside, 0.5), 1))
    return result


def render(result, header=()):
    out = ['# One FPGA product call, split', '', *header]
    out += [f'- Calls in the trace: {result["calls"]}; complete and used: {result["complete_calls"]}'
            f' (mode {result["mode"]}{"; " + result["mode_note"] if result.get("mode_note") else ""})',
            f'- Skipped: {result["skipped"] or "none"}; unfinished at the end: {result["unfinished_calls"]}']
    if not result['segments']:
        return '\n'.join(out + ['', 'No complete call: nothing to split.']) + '\n'
    total = result['total']
    out += [f'- Kernel time per call (edgefhe_ioctl): median {total["median_us"]:,.1f} us, '
            f'p10–p90 {total["p10_us"]:,.1f}–{total["p90_us"]:,.1f} us; outside the DMA window: '
            f'median {result["outside_dma_median_us"]:,.1f} us', '',
            '| Part | What happens | Median us | p10–p90 us | Share | Bytes | MB/s |', '|---|---|---|---|---|---|---|']
    for s in result['segments']:
        note = f' ({s["ns_per_load"]} ns per load)' if 'ns_per_load' in s else ''
        out.append(f'| {s["segment"]} | {s["meaning"]}{note} | {s["median_us"]:,.1f} | {s["p10_us"]:,.1f}–'
                   f'{s["p90_us"]:,.1f} | {100 * s["share_of_mean"]:.1f}% | {s.get("bytes", "")} | '
                   f'{s.get("mb_per_s", "")} |')
    out += ['', 'Share = mean of the part / mean of the whole call, so the column adds up to 100%. '
            'Times include the tracer\'s own cost; the untraced baseline above shows how much that is.']
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- self-test
def _fake_line(ts_us, cpu, body, dur_us=None, depth=0, comment=False, task='v5_e2e-4242'):
    """One line formatted like kernel/trace/trace_functions_graph.c (5.15)."""
    line = '%5d.%06d |  ' % divmod(ts_us, 1000000) + ' %d) ' % cpu
    pad = 14 - len(task)
    line += ' ' * (pad // 2) + task + ' ' * (pad - pad // 2) + ' | '
    if dur_us is None:
        line += '              |  '
    else:
        usecs, nsecs = divmod(round(dur_us * 1000), 1000)
        text = str(usecs) + ('.%03d' % nsecs if len(str(usecs)) < 7 else '')
        mark = '#' if dur_us > 1000 else '!' if dur_us > 100 else '+' if dur_us > 10 else ' '
        line += mark + ' ' + text + ' us ' + ' ' * max(0, 8 - len(text)) + '|  '
    return line + ' ' * (2 * (depth + 1 if comment else depth)) + body


def self_test():
    mod = ' [edgefhe_rns3_n4096_dma_proxy]'
    lines = ['# tracer: function_graph', '#']
    expect = []
    for n, (t, cpu2) in enumerate(((100_000_000, 1), (100_010_000, 3))):
        L = lambda dt, *a, **k: lines.append(_fake_line(t + dt, *a, **k))  # noqa: E731
        L(0, 1, f'edgefhe_ioctl{mod}() {{')
        L(2, 1, 'mutex_lock();', 1.0, 1)
        L(4, 1, f'edgefhe_submit_transfer{mod}() {{', None, 1)
        L(5, 1, '__check_object_size();', 2.0, 2)
        L(400, 1, f'/* copied_in: (edgefhe_submit_transfer+0xf0/0x5e8{mod}) */', None, 2, True)
        L(3400, 1, f'/* validated: (edgefhe_submit_transfer+0x1b8/0x5e8{mod}) */', None, 2, True)
        L(3500, 1, f'/* cleared: (edgefhe_submit_transfer+0x1cc/0x5e8{mod}) */', None, 2, True)
        L(3510, 1, '__init_swait_queue_head();', 0.5, 2)
        L(3530, 1, 'ktime_get();', 1.0, 2)
        if n == 0:
            L(3540, 1, 'wait_for_completion_timeout();', 539.0, 2)
        else:  # woken on another CPU: entry and exit print as separate lines
            L(3540, 1, 'wait_for_completion_timeout() {', None, 2)
            lines.append(' ------------------------------------------')
            L(4079, cpu2, '} /* wait_for_completion_timeout */', 539.0, 2)
        L(4080, cpu2, 'ktime_get();', 1.0, 2)
        L(4500, cpu2, f'/* copied_out: (edgefhe_submit_transfer+0x52c/0x5e8{mod}) */', None, 2, True)
        L(4501, cpu2, f'edgefhe_wipe_buffers{mod}();', 199.0, 2)
        L(4702, cpu2, f'}} /* edgefhe_submit_transfer{mod} */', 4698.0, 1)
        L(4703, cpu2, 'mutex_unlock();', 1.0, 1)
        L(4706, cpu2, f'}} /* edgefhe_ioctl{mod} */', 4706.0)
        expect.append({'enter': 3, 'checks': 4, 'copy_in': 393, 'validate': 3000, 'clear_out': 100,
                       'dma_setup': 31, 'dma_wait': 549, 'copy_out': 420, 'wipe': 200, 'leave': 6})
    lines.append(_fake_line(100_020_000, 2, 'edgefhe_ioctl() {'))  # cut off by the end of the buffer
    calls, _, unfinished = parse_calls('\n'.join(lines))
    assert len(calls) == 2 and unfinished == 1, (len(calls), unfinished)
    for call, want in zip(calls, expect):
        mode, got = split_call(call)
        assert mode == 'fine', got
        for key, value in want.items():
            assert abs(got[key] - value) < 1e-6, (key, got[key], value)
    result = analyse('\n'.join(lines))
    assert result['complete_calls'] == 2 and result['total']['median_us'] == 4706.0, result['total']
    by = {s['segment']: s for s in result['segments']}
    assert by['validate']['ns_per_load'] == round(3000e3 / 32768, 1) and by['copy_in']['mb_per_s'] == round(131072 / 393, 1)
    coarse = [line for line in lines if '/* ' not in line or '} /*' in line]
    result = analyse('\n'.join(coarse))
    assert result['mode'] == 'coarse' and result['complete_calls'] == 2, result
    by = {s['segment']: s['median_us'] for s in result['segments']}
    assert by['copy_validate_clear'] == 3503.0 and by['dma_setup'] == 21.0, by
    assert by['copy_out'] == 421.0 and by['wipe'] == 199.0 and by['leave'] == 6.0, by
    render(result)
    print('TRACE_PARSER_SELF_TEST_PASS')


# ---------------------------------------------------------------- board
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_descriptor(fd):
    """Read-only: the inherited descriptor must be the gated EdgeFHE character device."""
    target = os.readlink(f'/proc/self/fd/{fd}')
    if target != DEVICE or not stat.S_ISCHR(os.fstat(fd).st_mode):
        raise SystemExit(f'--fd {fd} points to {target}, not the {DEVICE} character device')
    return target


class Tracefs:
    def __init__(self):
        for path in (Path('/sys/kernel/tracing'), Path('/sys/kernel/debug/tracing')):
            if (path / 'trace').exists():
                self.path = path
                return
        raise SystemExit('tracefs not found (mount -t tracefs nodev /sys/kernel/tracing)')

    def read(self, name):
        return (self.path / name).read_text(encoding='utf-8', errors='replace')

    def write(self, name, value, append=False):
        # Like the shell's > and >>. Not open(..., 'a'): Python seeks to the end first, and
        # seq_file control files such as kprobe_events reject SEEK_END with EINVAL.
        fd = os.open(self.path / name, os.O_WRONLY | (os.O_APPEND if append else os.O_TRUNC))
        try:
            if value:
                os.write(fd, value.encode())
        finally:
            os.close(fd)

    def has(self, name):
        return (self.path / name).exists()


def filter_names(text):
    return {line.split()[0] for line in text.splitlines() if line and not line.startswith('#')}


def module_identity(ko):
    """Facts that decide whether the kprobe offsets apply to the loaded module."""
    sys_module = Path('/sys/module') / MODULE
    facts = {'kernel': platform.release(), 'loaded': sys_module.is_dir(),
             'srcversion': (sys_module / 'srcversion').read_text().strip() if (sys_module / 'srcversion').exists() else None,
             'ko': str(ko), 'ko_sha256': sha(ko) if Path(ko).is_file() else None, 'kallsyms': {}}
    for line in Path('/proc/kallsyms').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] == f'[{MODULE}]':
            facts['kallsyms'][parts[2]] = int(parts[0], 16)
    problems = []
    for key in ('kernel', 'srcversion', 'ko_sha256'):
        if facts[key] != BUILD[key]:
            problems.append(f'{key} {facts[key]} != {BUILD[key]}')
    for first, second, distance in BUILD['layout']:
        a, b = facts['kallsyms'].get(first), facts['kallsyms'].get(second)
        if not a or not b or b - a != distance:
            problems.append(f'kallsyms {second} - {first} is not {distance:#x}')
    facts['kallsyms'] = {k: hex(v) for k, v in facts['kallsyms'].items()}
    return facts, problems


def save_state(t):
    state = {name: t.read(name).strip() for name in ('tracing_on', 'current_tracer', 'max_graph_depth')}
    size = t.read('buffer_size_kb')
    expanded = re.search(r'expanded:\s*(\d+)', size)
    state['buffer_size_kb'] = expanded.group(1) if expanded else size.split()[0]
    state['trace_clock'] = re.search(r'\[(\S+)\]', t.read('trace_clock')).group(1)
    state['options'] = {opt: t.read('options/' + opt).strip() for opt in OPTIONS if t.has('options/' + opt)}
    return state


def restore(t, state):
    errors = []

    def attempt(name, value, append=False):
        try:
            t.write(name, value, append)
        except OSError as e:
            errors.append(f'{name} <- {value.strip()!r}: {e}')

    attempt('tracing_on', '0')
    attempt('current_tracer', 'nop')
    if t.has(f'events/{GROUP}'):
        attempt(f'events/{GROUP}/enable', '0')
        for name, *_ in MARKS:
            if t.has(f'events/{GROUP}/{name}'):
                attempt('kprobe_events', f'-:{GROUP}/{name}\n', append=True)
    attempt('set_ftrace_filter', '')
    attempt('set_graph_function', '')
    attempt('max_graph_depth', state['max_graph_depth'])
    for opt, value in state['options'].items():
        attempt('options/' + opt, value)
    attempt('trace_clock', state['trace_clock'])
    attempt('buffer_size_kb', state['buffer_size_kb'])
    attempt('trace', '')
    attempt('tracing_on', state['tracing_on'])
    return errors


def configure(t, available, marks):
    """Hook only FILTER, graph from edgefhe_ioctl to depth 3, kprobe marks if allowed."""
    applied = []
    t.write('tracing_on', '0')
    t.write('current_tracer', 'nop')
    t.write('buffer_size_kb', '4096')
    t.write('trace_clock', 'mono')  # one clock for every CPU: a call may wake up on another core
    t.write('set_ftrace_filter', '')
    for name in FILTER:
        if name in available:
            t.write('set_ftrace_filter', name + '\n', append=True)
            applied.append(name)
    for glob in OPTIONAL_GLOBS:
        try:
            t.write('set_ftrace_filter', glob + '\n', append=True)
            applied.append(glob)
        except OSError:
            pass
    t.write('set_graph_function', 'edgefhe_ioctl')
    t.write('max_graph_depth', '3')
    if marks:
        for name, offset, _, _ in MARKS:
            t.write('kprobe_events', f'p:{GROUP}/{name} {MODULE}:edgefhe_submit_transfer+{offset:#x}\n', append=True)
        t.write(f'events/{GROUP}/enable', '1')
    t.write('current_tracer', 'function_graph')
    options = {}
    for opt, value in OPTIONS.items():
        if t.has('options/' + opt):
            t.write('options/' + opt, value)
            options[opt] = value
    t.write('trace', '')
    return {'filter': applied, 'options': options, 'graph_function': 'edgefhe_ioctl', 'max_graph_depth': 3,
            'trace_clock': 'mono', 'buffer_size_kb': 4096,
            'kprobes': [f'p:{GROUP}/{n} {MODULE}:edgefhe_submit_transfer+{o:#x}  # {i}: {w}'
                        for n, o, i, w in MARKS] if marks else []}


def run_workload(name, a, out, env):
    result = out / f'workload_{name}.json'
    command = [a.bin / 'v5_e2e', '--data', a.data, '--model', a.model, '--preprocess', a.preprocess,
               '--output', result, '--backends', a.backend, '--modes', 'latency', '--rounds', a.rounds,
               '--warmup', a.warmup, '--power', 'off', '--fd', a.fd, '--label', f'dma trace {name}']
    if a.cpus:
        command += ['--cpus', a.cpus]
    started = time.monotonic()
    run = subprocess.run([str(c) for c in command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=env, pass_fds=(a.fd,))
    (out / f'workload_{name}.log').write_text(run.stdout, encoding='utf-8')
    if run.returncode != 0 or 'V5_E2E_PASS' not in run.stdout:
        print(run.stdout[-3000:])
        raise SystemExit(f'workload {name} failed (exit {run.returncode}); stopping, no retry')
    data = json.loads(result.read_text(encoding='utf-8'))
    stats = next(r['backend_stats'] for r in data['latency']['results'] if r['backend'] == a.backend)
    jobs = stats['jobs']
    return {'json': result.name, 'seconds': round(time.monotonic() - started, 1), 'calls_measured': jobs,
            'device_ms_per_call': round(stats['device_ms'] / jobs, 4),
            'driver_reported_ms_per_call': round(stats['driver_reported_ms'] / jobs, 4),
            'pack_ms_per_call': round(stats['pack_ms'] / jobs, 4),
            'unpack_ms_per_call': round(stats['unpack_ms'] / jobs, 4)}


def board(a):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise SystemExit('Run on the board as root (inside the operator\'s sudo bash -c)')
    for name in ('bin', 'data', 'model', 'preprocess', 'output'):
        if getattr(a, name) is None:
            raise SystemExit(f'--{name} is required on the board')
    if not (a.bin / 'v5_e2e').is_file():
        raise SystemExit(f'{a.bin}/v5_e2e not found: build first (run_comparison.py or v5_sources.py)')
    out = a.output.resolve()
    if out.exists():
        raise SystemExit('Use a new --output directory; earlier traces are evidence')
    descriptor = check_descriptor(a.fd)
    t = Tracefs()
    if 'function_graph' not in t.read('available_tracers').split():
        raise SystemExit('function_graph tracer not available in this kernel')
    if t.read('current_tracer').strip() != 'nop' or filter_names(t.read('set_ftrace_filter')) \
            or filter_names(t.read('set_graph_function')):
        raise SystemExit('ftrace is already in use (current_tracer / filters set); not touching it')
    available = filter_names(t.read('available_filter_functions'))
    missing = [name for name in REQUIRED if name not in available]
    if missing:
        raise SystemExit('not traceable: ' + ', '.join(missing))
    identity, problems = module_identity(a.ko)
    if not identity['loaded']:
        raise SystemExit(f'{MODULE} is not loaded (gate G)')
    marks = not a.no_marks and not problems
    out.mkdir(parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    report = {'schema': 'edgefhe-dma-trace-1', 'status': 'RUNNING', 'device': descriptor,
              'device_opened_by_tool': False, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'workload': {'backend': a.backend, 'rounds': a.rounds, 'warmup': a.warmup, 'cpus': a.cpus,
                           'v5_e2e_sha256': sha(a.bin / 'v5_e2e')},
              'module': identity, 'marks_used': marks,
              'marks_skipped_because': problems if not a.no_marks else ['--no-marks'],
              'dma_policy': {'retry': False, 'reset': False, 'stop_on_first_failure': True, 'requests_in_flight': 1},
              'tracefs': str(t.path)}
    write_report = lambda: (out / 'trace_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')  # noqa: E731
    try:
        print('baseline (untraced) ...', flush=True)
        report['baseline'] = run_workload('baseline', a, out, env)
        if t.has(f'events/{GROUP}'):  # left by an interrupted earlier run of this tool
            for name, *_ in MARKS:
                if t.has(f'events/{GROUP}/{name}'):
                    t.write(f'events/{GROUP}/{name}/enable', '0')
                    t.write('kprobe_events', f'-:{GROUP}/{name}\n', append=True)
        state = save_state(t)
        report['tracing_before'] = state
        try:
            report['tracing'] = configure(t, available, marks)
            print('traced run ...', flush=True)
            t.write('tracing_on', '1')
            try:
                report['traced'] = run_workload('traced', a, out, env)
            finally:
                t.write('tracing_on', '0')
                (out / 'trace.txt').write_text(t.read('trace'), encoding='utf-8')
                report['overrun_events'] = sum(
                    int(m) for cpu in sorted(t.path.glob('per_cpu/cpu*/stats'))
                    for m in re.findall(r'^overrun:\s*(\d+)', cpu.read_text(), re.M))
        finally:
            report['restore_errors'] = restore(t, state)
        result = analyse((out / 'trace.txt').read_text(encoding='utf-8'))
        base, traced = report['baseline'], report['traced']
        header = [f'- Workload: v5_e2e latency, {a.backend}, {a.warmup}+{a.rounds} rounds, CPUs {a.cpus or "all"}; '
                  f'module {identity["srcversion"]}, kprobe marks {"on" if marks else "off: " + "; ".join(report["marks_skipped_because"])}',
                  f'- Per call as the program sees it (ioctl time): untraced {base["device_ms_per_call"]:.3f} ms, '
                  f'traced {traced["device_ms_per_call"]:.3f} ms (tracer cost '
                  f'{1000 * (traced["device_ms_per_call"] - base["device_ms_per_call"]):,.0f} us per call)',
                  f'- Driver-reported elapsed_us per call: {base["driver_reported_ms_per_call"]:.3f} ms untraced, '
                  f'{traced["driver_reported_ms_per_call"]:.3f} ms traced']
        (out / 'breakdown.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        (out / 'breakdown.md').write_text(render(result, header), encoding='utf-8')
        report['breakdown'] = {k: result[k] for k in ('mode', 'calls', 'complete_calls', 'skipped')}
        ok = result['complete_calls'] > 0 and not report['restore_errors']
        report['status'] = f'DMA_TRACE_COMPLETE mode={result["mode"]} calls={result["complete_calls"]}' if ok else 'FAIL'
    except BaseException as e:
        report['status'] = 'FAIL'
        report['failure'] = repr(e)
        raise
    finally:
        report['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        write_report()
    print(render(result, header))
    if report.get('restore_errors'):
        print('RESTORE ERRORS:', report['restore_errors'])
    print(report['status'], 'output=' + str(out), flush=True)
    return 0 if report['status'].startswith('DMA_TRACE_COMPLETE') else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--self-test', action='store_true', help='check the parser on a synthetic trace')
    p.add_argument('--parse', type=Path, help='re-analyse a saved trace.txt (no root, no device)')
    p.add_argument('--fd', type=int, help='operator-opened descriptor of ' + DEVICE)
    p.add_argument('--bin', type=Path, help='directory with v5_e2e, e.g. build/comparison/stage2_03/bin')
    p.add_argument('--data', type=Path)
    p.add_argument('--model', type=Path)
    p.add_argument('--preprocess', type=Path)
    p.add_argument('--output', type=Path, help='new directory')
    p.add_argument('--cpus', default='', help='pin the workload, e.g. 0-3')
    p.add_argument('--backend', choices=('fpga-serial', 'fpga-overlap'), default='fpga-serial')
    p.add_argument('--rounds', type=int, default=10)
    p.add_argument('--warmup', type=int, default=2)
    p.add_argument('--no-marks', action='store_true', help='function-level split only, no kprobe events')
    p.add_argument('--ko', type=Path, default=PREBUILT_KO, help='the .ko file the operator loaded in gate G')
    a = p.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    if a.self_test:
        self_test()
        return 0
    if a.parse:
        result = analyse(a.parse.read_text(encoding='utf-8'))
        print(render(result, [f'- Trace: {a.parse}']))
        return 0 if result['complete_calls'] else 1
    if a.fd is None:
        raise SystemExit('Board run needs --fd N (operator-opened); or use --parse / --self-test')
    return board(a)


if __name__ == '__main__':
    sys.exit(main())
