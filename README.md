# Copilot Case Sender

Sends instruction-ID case folders to Microsoft 365 Copilot through your installed Microsoft Edge: **one fresh
conversation per case**, the case's documents plus one shared **review guidance** file (`review_guidance.md`) attached, your `base_message`
typed into the composer, **sent once**, outcome saved before the next case starts.

Everything is in `copilot_case_sender.py` (read top to bottom, sections are numbered). `Run.cmd` launches it.
Tests live in `tests/`. All paths below are **symbolic labels** (`CASES_ROOT`, `RUNTIME_ROOT`, …) – the real
ones are typed once during setup and stay in your local, git-ignored `sender_config.json`.

## Quick start (Windows, no admin rights)

1. Put `Run.cmd` and `copilot_case_sender.py` together in a folder on `S:`.
2. Double-click `Run.cmd`. It asks once for `PYTHON_EXECUTABLE` (paste the path, quotes are fine; it also looks
   for a `python\python.exe` beside it) and then starts `setup`.
3. `setup` asks you to paste: `CASES_ROOT`, the **Review guidance** `.md` file, the `base_message` file, the **Result contract**
   `.md` (Enter = no answer collection), `PYTHON_EXECUTABLE`,
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

### Start of a run (once)
1. **Sign-in detection.** Edge opens; the tool keeps checking (every 0.25 s, no overall limit) until a Copilot tab shows
   both the chat composer and the model button, then continues **by itself**. If you are already signed in nothing is
   asked. After 1 s of waiting it tells you to sign in in the Edge window; pressing **Enter** in the terminal is only a
   manual fallback (ignored when the terminal is redirected).
2. **Model discovery** (~3 s): the model button's menu is read – modes (Auto, Quick response, Think deeper) and every
   provider submenu (Claude, GPT …); nothing is hard-coded, so added or removed models just appear.
3. **You choose** by number, **Enter = default = the last entry listed under GPT** (no version parsing). Setting
   `requested_model` skips the question. `model_prompt_seconds` (default 0 = wait) can make an unattended run continue
   with the default.
4. **Warm-up in parallel with step 3:** up to `warm_tabs` (default 8, never more than the cases to send) Copilot tabs are
   opened at once and then walked through one by one like a person would (brought forward, pointer over the composer,
   a short pause) so Copilot wakes each one. After your answer every tab applies the chosen model in the background.

### Per case, strictly one at a time
**Foreground rule.** Every tab is brought to the front (and confirmed visible) when a case starts working in it, again
before the send checks, and once more immediately before the Send click. A background tab is throttled by the browser,
which makes the page time out and can make a send silently not happen. This is tested (nothing sits between the
foreground step and the click).

**Console detail.** `console_detail` (asked in `setup`, or `Run.cmd run --detail minimal|full`): *minimal* prints the
number of instructions queued, each instruction ID being sent with its attachment count, its status once submitted
(green), and anything you must act on (warnings, retries, uncertain/blocked cases, pauses); *full* adds every step.
With `console_show_ids` (default on) the real instruction ID appears **on the console only** – the shareable log files
keep aliases. Set it to false to print aliases instead.

take a warm tab → check the model label (a cheap read) → attach all documents + the review guidance in **one bulk
assignment** → type `base_message` while the transfer runs → wait until the **exact** attachment set is present with no
transfer/error and Send enabled (held steady for `stable_seconds`) → record `send_intent` → click Send once → confirm
the user message in *that* tab → record `submitted`, then add the ID to the ledger. Inventory of the next case runs
meanwhile (**direct files** only unless `recursive_documents` is set). Cases beyond the warmed tabs open a new tab on
demand (V1 is meant for a handful of controlled cases; recycling answered tabs is a later version).

**How files are attached (no Windows file window).** The `+` button's "Upload images and files" entry normally opens
Windows' own file dialog, which broke sends on the VDI. The tool registers a Playwright *file-chooser listener* on every
tab, which intercepts the chooser: the dialog never appears and the full file list is handed to the page in one call
(`set_files`). Only that one menu entry is ever clicked; the quick-action buttons under the composer are never touched.
A simulated drag-and-drop was tried and **does not work** in this Copilot (the page receives the drop, ignores it).

**Failures:** if Copilot shows a real upload error with its own *Try again* button, that button is clicked (max 2
times, only inside an upload-error message – never the response *Regenerate*). Still failing → **the same tab is
refreshed to the original Copilot chat link** (new chat) and the same case is tried again, **up to 5 tries in total**
(first 3 back-to-back, then the case moves to the end of the queue for the rest). A connection loss before sending
reconnects and continues; after sending it never resends (see *Uncertain sends*).

Speed comes from removing dead time, never from skipping a check: DOM-event polling (no fixed sleeps), one browser
round-trip per poll, warm tabs ready before the first case, model selection and tab preparation in parallel with your
prompt, file bulk-assignment, and **pipelining** of the next case's inventory. Sends themselves never overlap.

### V2: the answer contract and result collection

`review_guidance.md` tells Copilot *what to analyse and how*; **`result_contract.md`** (shipped in this repo as a strict
template – edit it freely) tells it *exactly what to answer*. Both are attached to every case (2 fixed files, so up to 18
case documents under the 20 limit). Your `base_message` should tell Copilot to answer only as the contract defines.

**The answer is plain text, not a code box** (code boxes sometimes fail to render): one JSON object, on one line:

    {"COPILOT_RESULT":"Correct|Incorrect|Inconclusive","JUSTIFICATION":"text","A_B_COMPARISONS":[{"ITEM":"what","Customer_expectation":"value","System_data":"live value","MATCH":true}]}

The tool reads only the **newest assistant reply** of that case's chat once Copilot has stopped generating and the text
has been unchanged for `answer_stable_seconds`. It finds the JSON object anywhere in the text (words around it or a code
block are tolerated) and checks it strictly: exactly the 3 keys, the verdict exactly `Correct`/`Incorrect`/`Inconclusive`,
each row exactly `ITEM`, `Customer_expectation`, `System_data`, `MATCH` (true/false), and the verdict must agree with the
rows (`Correct` ⇒ every `MATCH` true; `Incorrect` ⇒ at least one false; both need at least one row).

* **Not valid?** The tool immediately asks, in the same chat, for “your exact same answer, unchanged in content, in the
  correct structure” and names what was wrong – up to `fixup_requests` (3) times.
* **Still not valid?** The same instruction ID is resubmitted in the **same tab refreshed to a new chat**, inside the same
  5-tries budget (the only case where a submitted ID is sent again: no valid answer exists). After the last try the case
  ends as `result_invalid`.
* **Slow answers:** after `answer_overdue_minutes` (20) the console shows “answer overdue”; the tool keeps waiting and
  never resubmits a case Copilot may still be answering. When nothing else is left to send it is reported as
  `response_timeout`.
* **Collection runs in the background**: cases are still sent one by one; while Copilot answers, the next case is
  sent. A collector visits each answering tab (bringing it to the front – the sender and the collector never use the
  front tab at the same time), judges finished answers and sends fix-ups. A tab whose answer is collected is reset to a
  new chat and reused when a later case needs a tab.
* **Results** (local, in `RUNTIME_ROOT\results`): `<instruction ID>.json` (the validated answer + raw text) and
  `results.csv` (ID, status, verdict, rows, differences, tries, fixups, left-out files). The console prints each verdict:
  Correct white on green, Incorrect white on red, Inconclusive black on yellow. A valid verdict, even *Incorrect*, is a
  finished case; the ledger still means “submitted” and a case with a valid answer is skipped on re-runs.
* **Re-runs:** a case submitted earlier but never answered gets its answer collected from its saved chat (same tab by exact
  target ID, else the saved conversation link in a new tab) – never resent for that. Cases submitted before V2 are not
  touched.
* Model picker: models that Copilot greys out (for example after a usage limit) are shown as unavailable, are never the
  default, and choosing one stops the run with a clear message instead of burning retries.

### Re-runs and manual resend
`RUNTIME_ROOT\sent_instructions.txt` lists every instruction ID that was submitted (or is `uncertain`). Those IDs are
**never sent again**. To resend one yourself (new documents, a failed process…) **delete its line** and save; the next
run releases it (the append-only journal keeps the history). Unsent failures are retried within budget; uncertain
ones are never retried – check them in Copilot first.

### Outcomes
`queued · preflight_blocked · preparing · uploading · upload_ready · send_intent · submitted · retry_pending ·
submission_uncertain · failed_exhausted · cancelled` (+ run-level `recovery_required`). These are *transport*
outcomes: “submitted” never means “the analysis was correct”. With `completion_mode: json_result` (V2, below) each
submitted case continues with `fixup_sent · result_valid · result_invalid · response_timeout`;
`completion_mode: submission_only` is the V1 behaviour (send only).

Blocked (never silently skipped, other cases continue): unsupported type, empty file, over `max_file_mb`,
unreadable/locked file, likely password-protected Office/PDF file, link not followed.
`ignore_names` (default `~$*`, `desktop.ini`, `thumbs.db`) are skipped but listed in the manifest.

### The 20-attachment limit
`max_attachments` (default 20, **the review guidance file counts**) is the *reference program's* number; live testing showed
Copilot accepting exactly 20 files on a personal profile, but check your tenant. A case with exactly the limit is
sent and the console says `at-limit`. When a case has **more** files, `over_limit_mode` decides:
* `send_partial` (default): attach the review guidance + the first documents in sorted order up to the limit, print a
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

* `approved_root` (default: the drive/share root of `RUNTIME_ROOT`) defines **approved storage**. Cases, review guidance,
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
| V2 on real Copilot: contract + guidance attached, plain-text JSON answers read back, Correct / Incorrect / Inconclusive verdicts from 3 synthetic cases, results.csv and per-case JSON | **Live-tested** (about 15 s from send to verdict); the fix-up and resubmission loops are **tested here** with a fake page |
| Real Copilot: sign-in auto-detection, model discovery + selection, 5 warm tabs, intercepted file chooser (no Windows dialog), exact-20-file upload, send, confirmation in the same tab, no cross-case mixing, re-run skips sent IDs, manual release, over-limit partial send | **Live-tested** on a Microsoft 365 Copilot profile with synthetic files (about 7–11 s per case); the VDI is yours to re-confirm |
| Upload-error → Try again ×2 → same-tab refresh → 5 tries; model picker with a different model list | **Tested here** with a fake page (could not be provoked on the real page) |
| Green/yellow status colours | **Tested here** (escape codes; look on your console) |
| Unresponsive renderer on a real Edge | **Needs your Windows/VDI** (error path tested with a fake) |

## What was reused from the earlier runner, and what was dropped

Reused ideas (re-written, not imported): dedicated-profile Edge launch with loopback debugging; `/json/version`
polling and endpoint validation; browser-WebSocket-then-HTTP `connect_over_cdp` with the 3-6-12-20-30 s ladder;
exact target-ID rebinding; the composer/attachment/Send/Stop selectors; bulk file assignment (originally `DOM.setFileInputFiles`, now through an intercepted file chooser; the
quarantined `playwright_bulk` route is **not** restored); Send-versus-Stop guarding; first-launch private install.
Dropped: the old audit workflow (CSV/allow-lists, merge-status JSON, merged PDFs, case-size categories, multi-case
packing, audit prompt/response contract/result parser), indefinite reconnect loops, permissive Edge flags, Temp
deletion routines, and the thousands of lines of diagnostics/override layers.
