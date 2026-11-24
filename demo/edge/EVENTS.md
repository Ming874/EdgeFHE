# Gateway events, records and HTTP API

This document fixes the interface between the board gateway core (`efgw/config.py`, `model.py`, `permit.py`,
`records.py`, `runner.py` and `edgefhe_gateway.py`) and the components built on it: the loopback HTTP API and
progress sampler (`efgw/httpapi.py`, `efgw/progress.py`), the owner screen (`demo/ui`), the owner-laptop serial relay
(`demo/owner`), the evidence verifier (`demo/evidence`) and the PC rehearsal driver (`demo/sim`). Field names, codes
and markers below are exact; zh-TW texts come from [form_cdc.json](../model/form_cdc.json) and `runner.MESSAGES`.

## 1. Data classes

| Class | Content | Where it may go |
|---|---|---|
| `owner` | answers, normalized values, contributions, logits, `p_balanced`, level, boundary, refusal code | the owner screen on 127.0.0.1 (and the serial relay to the owner laptop); the root-only private store on tmpfs; a 0600 disclosure of a fictional form written only by `disclose` |
| `public` | commitment, hashes, timings, counters, PIDs, argv, STOP codes, gateway codes | additionally the public log `/var/log/edgefhe-demo/public.jsonl` (whitelisted fields only, section 7) |

Owner-class events never reach journald, the public log, the cloud PC or any socket other than the loopback owner
screen. The command line prints public events only (`EVT` lines), or none with `--quiet`; `submit --owner-events`
also prints the owner-class events, for local debugging on the owner console only. `submit` always shows the risk
level of its answers on its own terminal (stderr and `GATEWAY_SUBMIT_RESULT`), so it must never run on a console
attached to the cloud PC, such as the board's serial console during setup. Under systemd (`serve`) stdout goes to the
journal, so the HTTP module prints public events only, or none with `--events none` or `--quiet`.

## 2. Envelope

```json
{"v": 1, "seq": 17, "run": "r3f9a2c1b0d4e", "type": "client.exited", "class": "public", "t_mono_ns": 815263000123, "data": {}}
```

| Field | Meaning |
|---|---|
| `v` | 1 |
| `seq` | 1, 2, 3, … per gateway process, over all runs; no gaps unless the in-memory log (20,000 events) dropped old ones (`EventLog.first_seq()`) |
| `run` | `r` + 12 lowercase hex digits, fresh per submission (`^r[0-9a-f]{12}$`) |
| `type`, `class` | section 3; `class` is `owner` or `public` |
| `t_mono_ns` | board `CLOCK_MONOTONIC` (`time.monotonic_ns()`) when the gateway emitted the event |
| `data` | JSON object; numbers are finite |

## 3. Event order and types

* Accepted form: `form.accepted` (public), `form.accepted` (owner), `client.spawned`, any number of `progress.irq`,
  `progress.io` and non-fatal `error`, `client.exited`, `irq.delta`, then either a fatal `error` or `result`
  (followed by a fatal `error` `VERIFICATION_FAILED` when the check failed), any non-fatal `WIPE_FAILED` or
  `RECORD_WRITE` `error`, and last `wiped` (emitted after the public record and the optional private record were
  written, so that `retained_private` is exact). `wiped` is emitted even when writing a record failed: the failure
  is a non-fatal `RECORD_WRITE` `error`, and a private file whose write or fsync failed (for example `ENOSPC` on
  tmpfs) is overwritten with zeros and unlinked at once, so `retained_private` is then false and no partial private
  file stays behind.
* Refused form: `form.refused` only. No file, no process, no network traffic; nothing is written to the public log.
* Public gate or throughput window: `client.spawned` … `irq.delta`, an optional fatal `error`, `wiped`.
* A run that fails before its client starts: the events up to that point, a fatal `error`, `wiped`.

| Type | Class | Data |
|---|---|---|
| `form.accepted` | public | `kind` `"form"`, `backend`, `model`, `model_sha256`, `commitment` |
| `form.accepted` | owner | `features` (model order), `values` (int answers, BMI float), `x`, `z`, `contributions` (`weights[f] * z[f]`), `bias`, `commitment` |
| `form.refused` | owner | `code` (e.g. `BMI_ABOVE_MAX`, `AGE_UNDER_18`, `FORM_FIELDS`), `message` (zh-TW), `field` (or null), `note` |
| `client.spawned` | public | `kind` (`form`, `gate`, `throughput`), `backend`, `pid`, `argv`, `permit` (bool), `contract`, `client_sha256`, `overdue_s`, `rows`, `requests`, `window_ms`, `flags`, `irq_before` (`{irq: count}`), `dma_lines` (`{irq: "GICv2 121 Level xilinx-dma-controller"}`) |
| `progress.irq` | public | emitted by the progress sampler: `irq`, `count`, `delta` (increase since the spawn on that line), `index` (1 = public-key LOAD, 2 and later = ENC), `label` (`LOAD` or `ENC`), `t_ns` (since `client.spawned`) |
| `progress.io` | public | emitted by the progress sampler: `wchar`, `rchar` (bytes since its first sample of `/proc/<pid>/io`), `t_ns` |
| `client.exited` | public | `returncode`, `wall_ns` (spawn to exit), `overdue` (bool), `status` (client status or null), `stop` (STOP code or null), `stop_detail`, `report` (the client JSON line; a trace longer than 32 entries is cut, with `trace_truncated` and `trace_count`) |
| `irq.delta` | public | `backend`, `delta` (`{irq: after - before}`), `before`, `after`, `expected` (device calls on the PL backend, else null), `passed` (bool, or null when not checked), `lines` |
| `result` | owner | `logit_ckks`, `logit_plain`, `abs_error`, `max_abs_error` (0.002), `passed`, `p_balanced`, `level` (1–4 or null), `level_code`, `level_name`, `level_label`, `observed_rate`, `observed_caption`, `boundary` (`[2, 3]` or null), `boundary_text` (`介於 L2／L3，不判定`), `plain_level`, `plain_boundary`, `message` (the zh-TW failure text, or null), `evidence` |
| `error` | public | `code`, `message` (one complete zh-TW sentence, without the no-retry rule; section 9), `fatal` (false only for `CLIENT_OVERDUE`, `PROGRESS_SAMPLER`, `WIPE_FAILED`, `RECORD_WRITE`), `stop` (true when `code` is the client's STOP code) |
| `wiped` | public | `files` (of `in.csv`, `score.txt`, `permit`, each overwritten with zeros where it held data, then unlinked), `run_dir_removed`, `problems`, `retained_private` (bool: true only when a private record of this form now exists on the board's tmpfs until `disclose` or a reboot, section 8; false for gates, throughput windows and whenever `retain_private` is false) |

`result.evidence` repeats what the evidence strip shows: `commitment`, `request_sha256`, `response_payload_sha256`,
`device_calls`, `PL_hz`, `irq_delta`, `request_frame_bytes` (393,472 = 256 + 4 × 98,304) and `response_frame_bytes`
(98,560). The owner screen shows the level and the observed rate of the test data, never `p_balanced` as a
probability; within 0.002 of a level edge it shows `boundary_text` and no level; when `passed` is false it shows
`message` and no level.

Timings for the 13 stages come from `client.exited.report`: `input_read_ns` (CSV), `cold_context_and_TLS_ns`
(stages 4–5: key generation, PL LOAD, TLS handshake), and `trace[0]` with `prepare_ns` (stages 6–8: normalize,
encode, PL ENC), `TLS_roundtrip_and_validate_ns` (stages 9–11) and `decrypt_ns` (stage 12); `wall_ns` is the
gateway's own measurement. The live markers are `progress.irq` (LOAD, then ENC) and `progress.io`.

## 4. Gateway object (for the HTTP module)

`edgefhe_gateway.py serve [--config CFG] [--rehearsal-listen ADDR] [--progress-ms N] [more]` loads the configuration,
creates `gateway = runner.Gateway(cfg)` and returns
`httpapi.serve(gateway, cfg, rehearsal_listen=ADDR or None, progress_ms=N or None, argv=[more])` as its exit status.

| Member | Behaviour |
|---|---|
| `gateway.submit(form, backend=None)` | `(status, body)`. Validates synchronously; 202 `{"run"}` starts the run on a worker thread. 400 `{"code", "message", "run"}` for a refused form (a `form.refused` event exists), 400 `BACKEND_NOT_ALLOWED`, 409 `BUSY`, `PL_LOCK_BUSY` or `PL_DEVICE_BUSY`, 503 `GATEWAY_STOPPING`, `ROOT_REQUIRED`, `GATEWAY_PID1` or `WORKDIR_UNSAFE` (`{"code", "message"}`) |
| `gateway.status()` | `v`, `busy`, `run`, `stopping`, `default_backend`, `backends`, `retain_private` (bool, the configuration's `retain_private`, default false), `model` (`model_id`, `sha256`, `features`, `profile_hex`), `cloud` (`host`, `port`, `peer_name`, `protocol`, `mutual_authentication`, `certificate_pinning`), `sizes`, `form` (the whole form schema: fields, presets, levels, notice), `last_seq` |
| `gateway.rehearsal` | false; `httpapi.serve` sets it to true for `--rehearsal-listen 0.0.0.0`, and then every `pl` admission is refused with `REHEARSAL_PL_ON_BOARD` while `runner.board_markers(cfg)` finds `/dev/edgefhe_v8b_dma`, the configured `dma_node` or a `xlnx,zynqmp` device tree |
| `gateway.events` | `records.EventLog`: `since(seq, run=None, classes=…)`, `wait(seq, timeout, run=None)` (blocks until an event newer than `seq` exists), `last_seq()`, `first_seq()`, `add_listener(fn)` |
| `gateway.progress` | `None`, or a factory called as `factory(pid, emit, cfg)` right after `client.spawned`; `emit(type, data)` adds a public event to the run; the factory returns `stop()`, called after the client exited and before `client.exited`. A sampler only reads `cfg['proc_interrupts']` (`runner.parse_interrupts`, `runner.DMA_ACTION`) and `/proc/<pid>/io` every `progress_ms` (`cfg['progress_ms']`, 25 by default, 0 disables), and never signals |
| `gateway.request_stop(signum=None)`, `gateway.stopping`, `gateway.wait_idle(timeout)` | stop accepting work; the run in flight finishes normally |
| `runner.install_stop_signals(fn)` | SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and SIGUSR2 (`runner.STOP_SIGNALS`) call `fn(signum)` (main thread). The HTTP module stops its server from there and returns only after `gateway.wait_idle()`: with systemd `KillMode=mixed`, every process left when the main process exits is killed, and a signal during an issued PL ENC quarantines the driver. SIGKILL cannot be deferred and must never be sent while a client runs |

Configuration keys for the HTTP module: `listen` (`config.parse_listen`, default `127.0.0.1:8080`), `ui_dir`,
`progress_ms`. The gateway itself never opens a socket and never signals a client.

## 5. HTTP API (loopback only)

| Request | Response |
|---|---|
| `GET /`, `GET /ui/<file>` | the owner screen from `ui_dir`; extensions `.html .js .mjs .css .svg .json`; no traversal (404) |
| `GET /api/v1/status` | `gateway.status()` (including `retain_private`) plus `progress_ms` and `rehearsal` (bool) |
| `GET /api/v1/runs/<run>` | `{"run", "events"}` from the in-memory log; 404 when unknown |
| `POST /api/v1/submit` | body `{"form": {"GenHlth": 3, "HighBP": 1, "BMI": 31.2, "Age": 9}, "backend": "pl"}` (`backend` optional); the status and body of `gateway.submit` |
| `GET /api/v1/events?since=N` | Server-Sent Events: each envelope as `id: <seq>` and `data: <envelope JSON>` with no `event:` field; resume with `since` or `Last-Event-ID`; a comment line `: heartbeat` every 2 s |

Every request needs `Host` equal to `127.0.0.1:PORT` or `localhost:PORT` (else 403). A POST also needs
`X-EdgeFHE-Owner: 1`, `Content-Type: application/json`, a body of at most 4 KiB and an `Origin` that is absent,
`http://127.0.0.1:PORT` or `http://localhost:PORT` (else 403). Responses carry `Content-Security-Policy: default-src
'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'`,
`X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. The server binds 127.0.0.1 unless
`--rehearsal-listen 0.0.0.0` is given, the environment has `EDGEFHE_REHEARSAL=1` and `runner.board_markers(cfg)` is
empty (PC rehearsal only; else `GATEWAY_LISTEN_REFUSED code=REHEARSAL_ON_BOARD`).

## 6. Command line

`python3 -I -B edgefhe_gateway.py <command> [--config /etc/edgefhe-demo/gateway.json]`, as root. stdout carries one
`EVT <envelope>` line per public event (suppressed by `--quiet`, which `serve` honours too; owner-class events only
with `submit --owner-events`) and a final marker; stderr carries zh-TW messages: the one-sentence message of a failed
run is followed by the fixed line `本系統不會自動重試，也不會中斷加密程式。`, and `submit` writes the CKKS error as
`a×10⁻ⁿ` (for example `2.4×10⁻⁶`, as on the owner screen) and states whether a private record was retained (or which
private files it could not wipe).

| Command | Success marker | Failure markers |
|---|---|---|
| `preflight [--backend B]` | `GATEWAY_PREFLIGHT_PASS checks=N backend=B` after `PREFLIGHT_OK <name> …` lines | `PREFLIGHT_FAIL <name> …`, `GATEWAY_PREFLIGHT_FAIL failed=a,b` |
| `submit --genhlth N --highbp N --bmi X --age N [--backend B] [--owner-events]` | `GATEWAY_SUBMIT_RESULT run= backend= level= boundary= abs_error= passed=1 request_sha256= response_payload_sha256= commitment=` (`level=-` and `boundary=2/3` at an edge) | `GATEWAY_SUBMIT_REFUSED run= code=`, `GATEWAY_SUBMIT_STOP run= code=`, `GATEWAY_SUBMIT_FAILED run= code=` |
| `submit … --backend pl --expect OWNER_CREATE`, `gates --backends pl --expect OWNER_CREATE` | `GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE run= backend=pl` | as above; `EXPECT_REQUIRES_NO_DEVICE` while the DMA node exists |
| `gates --backends cpu,spec,pl` | `GATEWAY_GATE backend= …` per backend, then `GATEWAY_GATES_PASS backends= max_public_error= spec.device_calls=21 pl.device_calls=21 pl.irq_delta=+21/+21 pl.PL_hz=` | `GATEWAY_GATES_FAIL backend= code=` |
| `throughput [--backend pl] [--seconds 30]` | `GATEWAY_THROUGHPUT_PASS backend= records_per_s= completed_requests= device_calls= irq_delta= PL_hz= rows=2048 window_ms= mode=pipeline slots=8 inflight=8` | `GATEWAY_THROUGHPUT_FAIL backend= code=` |
| `disclose REQUEST_SHA256\|RUN` | `GATEWAY_DISCLOSURE_WRITTEN request_sha256= run= path= existed=0\|1` | `GATEWAY_DISCLOSURE_NOT_FOUND key=`, `GATEWAY_DISCLOSURE_FAILED key=` |
| `serve [--rehearsal-listen ADDR] [--progress-ms N] [--quiet] [--events public\|none]` | `GATEWAY_SERVING 127.0.0.1:8080` (HTTP module) | `GATEWAY_SERVE_UNAVAILABLE` |
| any | | `GATEWAY_RUN_NOT_STARTED code=`, `GATEWAY_CONFIG_INVALID code=`, `GATEWAY_MODEL_INVALID`, `GATEWAY_STOP_REQUESTED signal=` (after a deferred stop) |

Exit status: 0 pass; 1 a run or check failed; 2 the form was refused locally; 3 the gateway did not start a run
(not root, busy, PID 1, DMA node held, backend not allowed); 4 usage, configuration or model error. A gate synthesizes
`--rows 2048 --requests 20` in serial mode with the base contract and requires `max_public_error` ≤ 0.002,
`requests_over_5e4` 0, device calls 0, 21 and 21, and +21 on both DMA interrupt lines for the PL backend. The
throughput window uses `--mode pipeline --slots 8 --duplex 1 --hash-io 1 --decrypt-thread 1 --inflight 8
--io-cpu auto --io-isolate 0 --read-every 1` (`v8b_rev5/evidence/CLAIMS_LEDGER.json`, `current_best/pl/config`) and the
layered contract.

## 7. Public record

One JSON line per accepted form, gate and throughput window, sorted keys, ASCII, `O_APPEND`, file mode 0644, at
`public_log` (default `/var/log/edgefhe-demo/public.jsonl`). Only these keys exist (`records.PUBLIC_FIELDS`):

`v`, `kind`, `run`, `t_board_epoch` (board clock, seconds), `backend`, `model`, `model_sha256`, `profile`, `host`,
`port`, `client_sha256`, `commitment` (form), `status`, `stop`, `error` (gateway code), `verified` (form: the
0.002 check), `returncode`, `overdue`, `wall_ns`, `request_sha256` and `response_payload_sha256` (form, as in the
client trace and the cloud `server.log`), `device_calls`, `PL_hz`, `irq_delta`, `irq_expected`,
`completed_requests`, `rows`, `requests`, `window_ms`, `mode`, `flags` (throughput), `input_read_ns`,
`cold_context_and_TLS_ns`, `prepare_ns`, `TLS_roundtrip_and_validate_ns`, `decrypt_ns`, `elapsed_ns`,
`records_per_s`, `max_public_error`, `requests_over_5e4`, `trace` (gate: hashes and timings of the 20 requests),
`trace_count` and `trace_sha256` (throughput: SHA-256 of the lines `request_sha256,response_payload_sha256\n`).

No answer, normalized value, logit, `p_balanced`, level or boundary is ever written there.

## 8. Commitment, private record and disclosure

* Commitment: `sha256('EDGEFHE_FORM_COMMIT_V1\n' + salt_hex + '\n' + canonical_json + '\n')` with a fresh 32-byte
  salt (`salt_hex`, 64 lowercase hex digits). `canonical_json` is
  `json.dumps(values, sort_keys=True, separators=(',', ':'), ensure_ascii=True)` of the answers in canonical types,
  for example `{"Age":9,"BMI":31.2,"GenHlth":3,"HighBP":1}` (BMI always a float).
* Private record: only when the configuration sets `retain_private: true` (the default is false), the gateway keeps
  `<workdir>/private/<run>.json` for each form whose request reached the wire (0600 in a 0700 directory on tmpfs,
  schema `edgefhe.demo.private-record.v1`) with the salt, the answers, the CSV text, `x`, `z`, the logits and the
  level. A reboot clears it and `disclose` removes it. `status.retain_private` reports the setting and the `wiped`
  event's `retained_private` whether this run left a record. Only an evidence session with fictional answers that
  needs the disclosure (verification gates G9 and G10) turns the setting on; the PC rehearsal (`demo/sim`) does.
* Disclosure (`disclose`, fictional forms only): `<disclosure_dir>/<request_sha256>.json`, 0600, schema
  `edgefhe.demo.disclosure.v1`, with `fictional` (true), `notice`, `commitment_rule`, `run`, `kind`, `backend`,
  `model`, `model_sha256`, `request_sha256`, `response_payload_sha256`, `commitment`, `salt_hex`, `canonical_json`,
  `form`, `csv_text`, `x`, `z`, `contributions`, `logit_ckks`, `logit_plain`, `abs_error`, `max_abs_error`, `passed`,
  `level`, `boundary`, `p_balanced`, `status`, `stop`, `error`, `t_board_epoch`. The verifier opens the commitment of
  the public record with `salt_hex` and `canonical_json`, and checks `|logit_ckks - logit_plain| <= max_abs_error`
  and the level against the form schema.

## 9. Runs, files and the permit

Per run, `<workdir>/<run>/` (0700) holds `in.csv` (0600, LF, header `GenHlth,HighBP,BMI,Age`, one row, BMI with one
decimal), `score.txt` (0600, created by the client, one `%.17g` line) and `permit` (0600). The permit is
`EDGEFHE_CPP_MTLS_PL_V1\n<boot_id>\n<gateway pid>\n<st_dev>\n<st_ino>\n<contract>\n` with the contract of
[invoke_permit.hpp](../../v8b_rev5/software/client/invoke_permit.hpp) and the layers of
[client.cpp](../../v8b_rev5/software/client/client.cpp) (144-150), bound to the literal CSV and score paths. The
gateway creates it with `O_CREAT|O_EXCL|O_NOFOLLOW`, fchmods it to 0600, fsyncs it, passes a read-only descriptor
(`--permit-fd N`) and unlinks it only after the client exited. The client is the gateway's direct child
(`start_new_session=True`, `env {PATH: /usr/bin:/bin, LANG: C}`, stdin `/dev/null`, cwd `/`); the gateway refuses
to issue a permit as PID 1 (`GATEWAY_PID1`: use `docker run --init` or a shell wrapper). After the exit the gateway
overwrites `in.csv` and `score.txt` with zeros, fsyncs and unlinks them, and removes the directory.
[contract_vector.cpp](tests/contract_vector.cpp) prints the reference contracts from the real header into
`tests/contract_vectors.json`.

One run at a time per machine: `flock` on the fixed `/run/edgefhe-demo.pl.lock` (`runner.PL_LOCK_PATH`; a regular
root-owned file with one link, which no `gateway.json` can move), and for the `pl` backend a scan of `/proc/*/fd`
that refuses to start while any process holds the configured `dma_node` or `/dev/edgefhe_v8b_dma`. The scan reads
each descriptor's link first: the node's own path matches at once, and pipes, sockets and other non-path targets are
skipped. Any other absolute path is compared by `os.stat` identity (a device node by `st_rdev`, so a second node of
the same major:minor made elsewhere is found too; a regular test node by inode), except a path on a network or FUSE
file system (`runner.REMOTE_FS_PREFIXES`, from `/proc/self/mounts`), where `stat` could block the admission while
it holds the run lock. The scan and the client's own open are separate steps; the lock, not the scan, is what
excludes a second gateway.

A STOP code (`STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY <CODE>` on the client's stderr) becomes a fatal `error` whose
`code` is that STOP code and whose `message` is the zh-TW text of `runner.describe` (for example `TLS_PEER_PIN`,
`TLS_PEER_CHAIN`, `SOCKET_CONNECT_ERROR`, `TLS_DEADLINE`, `TLS_FATAL_OR_UNEXPECTED_EOF`, `NORMALIZATION_REFUSED`,
`OWNER_CREATE`, `PL_CLOCK_RANGE`, `CPU_SPEC_PL_FRAME_ACCOUNTING` and every `PL_PERMIT_*`). Nothing is retried. The
gateway's own codes are `BUSY`, `PL_LOCK_BUSY`, `PL_LOCK_UNSAFE`, `PL_DEVICE_BUSY`, `REHEARSAL_PL_ON_BOARD`,
`GATEWAY_STOPPING`, `GATEWAY_PID1`, `ROOT_REQUIRED`, `BACKEND_NOT_ALLOWED`, `EXPECT_INVALID`,
`EXPECT_REQUIRES_NO_DEVICE`, `EXPECTATION_UNMET`, `WORKDIR_UNSAFE`, `RUN_SETUP`, `CLIENT_SPAWN`, `CLIENT_OVERDUE`,
`CLIENT_SIGNALLED`, `CLIENT_EXIT`, `CLIENT_OUTPUT`, `CLIENT_REPORT_MISMATCH`, `IRQ_LINES`, `IRQ_DELTA_MISMATCH`,
`SCORE_FILE`, `VERIFICATION_FAILED`, `PROGRESS_SAMPLER`, `RECORD_WRITE`, `WIPE_FAILED` and `INTERNAL`.

Every `message` of `runner.describe` (the `error` events and the gateway's own `{code, message}` refusals) is one
complete, formal zh-TW sentence with an explicit subject that ends with the only `。` of the text and never states the no-retry
rule itself; a client STOP that ends the inference says `加密程式已自行停止此次推論`, because the gateway never
interrupts the client. A request for the operator names the operator as the subject (`操作人員應…`,
`操作人員不得…`), and a timeout is written with thousands separators (`TLS_DEADLINE`: `30,000 毫秒`). The owner
screen and the command line add exactly one fixed line under it: `本系統不會自動重試，也不會中斷加密程式。`
(`runner.NO_RETRY_LINE`).

## 10. Testing without a board

* [gateway.example.json](gateway.example.json) documents every configuration key. Two keys exist for tests only:
  `proc_interrupts` (a file in `/proc/interrupts` format; Docker Desktop containers show an empty
  `/proc/interrupts`) and `dma_node` (any path; the gateway only checks it for holders and existence).
* [fake_efnet_client.py](tests/fake_efnet_client.py) accepts the client's argv, re-checks the permit and emits the
  client's outputs; its behaviour comes from the JSON file passed as `--plugin` (interrupt bumps, upload and download
  byte counts for `/proc/<pid>/io`, STOP codes, delays). `/dev/shm` is mounted `noexec` in containers, so the fake
  executable lives under `/tmp` while the work directory may live on `/dev/shm`.
* Without `--init`, a test runner in a container is PID 1; a permit test therefore needs a child process as the
  client's parent (the gateway command line, for example).
