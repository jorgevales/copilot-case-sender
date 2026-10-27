# Copilot Case Sender

Sends instruction-ID case folders to Microsoft 365 Copilot through your installed Microsoft Edge: **one fresh
conversation per case**, the case's documents plus one shared `instructions.md` attached, your `base_message`
typed into the composer, **sent once**, outcome saved before the next case starts.

Everything is in `copilot_case_sender.py` (read top to bottom, sections are numbered). `Run.cmd` launches it.
Tests live in `tests/`. All paths below are **symbolic labels** (`CASES_ROOT`, `RUNTIME_ROOT`, …) – the real
ones are typed once during setup and stay in your local, git-ignored `sender_config.json`.

## Quick start (Windows, no admin rights)

1. Put `Run.cmd` and `copilot_case_sender.py` together in a folder on `S:`.
2. Double-click `Run.cmd`. It asks once for `PYTHON_EXECUTABLE` (paste the path, quotes are fine; it also looks
   for a `python\python.exe` beside it) and then starts `setup`.
3. `setup` asks you to paste: `CASES_ROOT`, the `instructions.md`, the `base_message` file, `PYTHON_EXECUTABLE`,
   `EDGE_EXECUTABLE`, `RUNTIME_ROOT` (and optionally a Node.js `node.exe`). Pressing Enter keeps a shown default;
   a folder pasted where a file is expected is searched for the expected name. Re-run `Run.cmd setup` any time to edit.
4. `Run.cmd check` validates everything (nothing is sent). `Run.cmd plan` shows the queue and any blocked cases.
   `Run.cmd run` (or just double-clicking) sends. The first run opens a dedicated Edge window: **sign in to
   Copilot there manually, once** (the tool never touches sign-in or MFA).

First launch installs the pinned `playwright` into `RUNTIME_ROOT\deps` (see *Dependencies*). It happens before
any case is processed, never during the case loop.

## What it does, per case

Queue = immediate child folders of `CASES_ROOT` named `<32 hex instruction ID>[ - <reference>]`, sorted.
`.edge-debug-9223` (any letter case) is excluded before anything else touches it. Other folders are reported
by a safe alias (`other-xxxxxx`); two folders with the same ID are both blocked and reported, never merged.

For each case, strictly one at a time:
fresh tab → inventory of **direct files** (set `recursive_documents` to include subfolders) → attach all documents
+ instructions in **one** `DOM.setFileInputFiles` call → type `base_message` → wait until the **exact** attachment
set is present with no transfer/error and Send enabled (held steady for `stable_seconds`) → record `send_intent`
→ click Send once → confirm the user message in *that* tab → record `submitted`, then add the ID to the ledger.

Speed comes from removing dead time, never from skipping a check: DOM-event polling (no fixed sleeps), one browser
round-trip per poll, no copying of files unless a name clash needs an alias, and **pipelining** – while case N
uploads, case N+1's folder is inventoried and its fresh tab is opened. Sends themselves never overlap.

### Re-runs and manual resend
`RUNTIME_ROOT\sent_instructions.txt` lists every instruction ID that was submitted (or is `uncertain`). Those IDs are
**never sent again**. To resend one yourself (new documents, a failed process…) **delete its line** and save; the next
run releases it (the append-only journal keeps the history). Unsent failures are retried within budget; uncertain
ones are never retried – check them in Copilot first.

### Outcomes
`queued · preflight_blocked · preparing · uploading · upload_ready · send_intent · submitted · retry_pending ·
submission_uncertain · failed_exhausted · cancelled` (+ run-level `recovery_required`). These are *transport*
outcomes. **Response collection and result validation are not delivered in this version** (`completion_mode` only
accepts `submission_only`); “submitted” never means “the analysis was correct”.

Blocked (never silently skipped, other cases continue): unsupported type, empty file, over `max_file_mb`,
unreadable/locked file, likely password-protected Office/PDF file, link not followed.
`ignore_names` (default `~$*`, `desktop.ini`, `thumbs.db`) are skipped but listed in the manifest.

### The 20-attachment limit
`max_attachments` (default 20, **instructions.md counts**) is the *reference program's* number; live testing showed
Copilot accepting exactly 20 files on a personal profile, but check your tenant. A case with exactly the limit is
sent and the console says `at-limit`. When a case has **more** files, `over_limit_mode` decides:
* `send_partial` (default): attach instructions.md + the first documents in sorted order up to the limit, print a
  `WARNING` *before* sending (count of files left out), and finish as **`submitted_partial`** (yellow). The ledger
  records it as `partial`, the run exits with code 1, and the left-out file list stays in the local journal.
  The case is *not* resent automatically – delete its ledger line if you want to handle the rest manually.
* `block`: nothing is sent for that case (`preflight_blocked`, exact counts); other cases continue.
Nothing is ever merged, converted, split across chats or dropped silently.

### Console colours
A successfully submitted case prints **white text on green**; `submitted_partial` and `WARNING` print black on
yellow. Colour is used only on a real console (not in redirected output or log files; `NO_COLOR` disables it).

### Uncertain sends
A send is *uncertain* when `send_intent` was saved but no user message with your text could be seen in the same tab
(timeout, tab gone after reconnect, crash). The tool reconnects, rebinds by **exact browser target ID** (never tab
index or a shared URL), and looks at that conversation. If it still can't prove it, the case is
`submission_uncertain`, its ID goes in the ledger, and it is **never auto-resent**. Exactly-once cannot be proved in
every failure; this design guarantees *at-most-once automatically*, and tells you about the rest.

## Storage, S: and UNC

* `approved_root` (default: the drive/share root of `RUNTIME_ROOT`) defines **approved storage**. Cases, instructions,
  base message, dependency folder, runtime, config and every tool write must be inside it. Executables
  (python/edge/node) are read/execute only and may live anywhere (e.g. `C:`).
* Identity is a tuple of *path components*: a mapped drive is resolved with `WNetGetUniversalNameW` (buffer resized,
  errors raised not swallowed); `S:\x` and `\\server\share\x` get the same identity. If discovery is unavailable you
  can set `approved_unc_root`; if both exist and disagree the tool refuses to start. Containment compares components
  (`S:\ab` is not inside `S:\a`), resolves `..`, and a junction/link whose **real** target leaves approved storage is
  rejected. Unrelated UNC shares are rejected.
* Paths are never blindly converted to UNC. Edge is launched with the path as you entered it; if no debugging endpoint
  appears it retries once with the UNC spelling and remembers the winner.
* If storage disappears the run stops starting cases, a failed listing is an error (never an empty queue), nothing is
  written elsewhere, and Edge is left alone.

### What is and is not guaranteed about `C:`
Controlled: process-local `TEMP/TMP/TMPDIR`, `tempfile`, bytecode (`-B`, `sys.pycache_prefix`), pip/npm/Playwright
caches, logs, state, staging, Edge user-data/cache/crash/download locations, and an **audit hook that blocks any
Python-level write outside approved storage**. `USERPROFILE/APPDATA/LOCALAPPDATA` are *not* rewritten.
**Not** guaranteed: Windows, Edge itself (e.g. component/policy/GPU caches), security software, OneDrive/AV or other
system services may still write on `C:`; child processes (Edge, Node, pip) are redirected by environment/flags but are
not sandboxed. Where the `.crdownload` temp of an Edge download lands must be verified in your environment.

## Embedded Python and dependencies

No `py` launcher, no `venv`, no global pip/npm, no PATH/registry changes, no `--user`. The launcher runs
`python -B -E -s` (no bytecode, ignore `PYTHON*` variables, no user site). `check` reports version/bitness and
inspects `pythonXY._pth` **without changing it** (stdlib entries preserved; a warning appears if `import site` is on).
The dependency folder is added to `sys.path` explicitly.

First launch (not during cases) installs `playwright==1.64.1` (pinned in `REQUIREMENTS`) with the **configured**
interpreter into `RUNTIME_ROOT\deps` (or `dependency_directory`), pip cache and temp under `RUNTIME_ROOT`. Embedded
Python has no pip, so the official pip wheel is fetched (checksum-verified) and run from the wheel. If wheels for your
Python version are unavailable it stops with a clear message. **Offline alternative:** on any machine run
`python -m pip install --target <DEPENDENCY_DIR> playwright==1.64.1` for the same Python version/architecture and set
`dependency_directory`. Node: set `node_executable` (exported as `PLAYWRIGHT_NODEJS_PATH` before Playwright starts)
or the copy bundled in the package is used – `check` shows which.

## Browser connection

Dedicated Edge profile under `RUNTIME_ROOT\profile` + `chromium.connect_over_cdp` (no fresh Chromium). One
controller lock per profile+port; loopback only (`--remote-debugging-address=127.0.0.1`, no wildcard origins); the
port must belong to our profile (`DevToolsActivePort` when Edge writes it, otherwise Edge must be holding the profile `lockfile` – a heuristic) so your other debug sessions are never touched.
Handshake escalation `3, 6, 12, 20, 30 s`, browser WebSocket first then HTTP, **sequentially**, discovery refreshed
each round, every attempt owned and cancelled/awaited. A bounded `reconnect_budget_seconds` ends in a durable
`recovery_required` run state (exit code 4) instead of looping. Ctrl+C stops at the next safe point (second press
aborts); Edge is never killed, no cookies or profile data are touched.

## Diagnostics and sharing

Console + `RUNTIME_ROOT\logs\events-*.jsonl` + `summary-*.json` use aliases and labels only; paths, IDs, URLs and
exception text are redacted. Real paths, case names, target IDs and conversation URLs exist **only** in
`RUNTIME_ROOT\state\` (local). Screenshots/HTML (`diagnostics_capture`, off by default) are sensitive, local-only and
never part of shareable output.

## Tests

```
set SENDER_TEST_ROOT=<a folder on approved storage>
<PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests
```
Needs `pytest`; refuses to run without `SENDER_TEST_ROOT` (never uses system Temp).

| Check | Status |
|---|---|
| 5 folders → 5 queue entries; `.edge-debug-9223` excluded (case variants) | **Tested here** |
| Spaces, Unicode, pasted quotes | **Tested here** |
| `S:` ≡ UNC identity (injected mapping), unrelated share, prefix-vs-component, mismatched mapping | **Tested here** (synthetic) |
| Escaping junction rejected | **Tested here** (local junction) |
| Real `WNetGetUniversalNameW` against your S: | **Needs your Windows/VDI** |
| Stub dependency import under `-B -E -s`; configured Node exported | **Tested here** |
| Embedded `._pth` behaviour, real embedded wheels | **Needs your Windows/VDI** |
| TEMP redirect, write guard, no write outside approved root | **Tested here** |
| S: loss → stop, no fallback; locked journal retries; torn journal line | **Tested here** (simulated) |
| Replace/lock behaviour on the real share | **Needs your Windows/VDI** (`check` probes it) |
| Delayed / hidden-overflow / partial uploads, extras, multiplicity | **Tested here** (fake page) |
| Duplicate basenames, instructions collision, unsupported, limit overflow | **Tested here** |
| Handshake fail after good endpoint, hang → cancel, budget exhaustion | **Tested here** (fake Playwright) |
| CDP loss during upload / right after send; target missing → uncertain; no resend | **Tested here** (fake page) |
| Crash between send and persistence; ledger repair/release; retry exhaustion; cancel | **Tested here** |
| No cross-case contamination; redaction of console/summary | **Tested here** |
| Real Copilot: fresh chat, 3-file and exact-20-file upload, send, confirmation in the same tab, no cross-case mixing, re-run skips sent IDs, manual release by deleting a ledger line, over-limit partial send | **Live-tested** on a personal Microsoft 365 profile with synthetic files (about 5–13 s per case); re-check on your tenant |
| Green/yellow status colours | **Tested here** (escape codes; look on your console) |
| Unresponsive renderer on a real Edge | **Needs your Windows/VDI** (error path tested with a fake) |

## What was reused from the earlier runner, and what was dropped

Reused ideas (re-written, not imported): dedicated-profile Edge launch with loopback debugging; `/json/version`
polling and endpoint validation; browser-WebSocket-then-HTTP `connect_over_cdp` with the 3-6-12-20-30 s ladder;
exact target-ID rebinding; the composer/attachment/Send/Stop selectors; `DOM.setFileInputFiles` bulk assignment (the
quarantined `playwright_bulk` route is **not** restored); Send-versus-Stop guarding; first-launch private install.
Dropped: the old audit workflow (CSV/allow-lists, merge-status JSON, merged PDFs, case-size categories, multi-case
packing, audit prompt/response contract/result parser), indefinite reconnect loops, permissive Edge flags, Temp
deletion routines, and the thousands of lines of diagnostics/override layers.
