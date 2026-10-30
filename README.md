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
0. **Everything starts at launch, in parallel:** the moving-cursor question, the queue scan (ends with the `queue`
   line) and Edge (joined if already open on `cdp_port`, otherwise launched) plus the connection. As soon as the case
   count is known, **all needed tabs are prepared at once** – `min(warm_tabs, cases to send)`, default up to 8, in
   every scenario (Edge launched or joined): Copilot tabs already open are used first, blank new-tab pages are turned
   into Copilot tabs, then new tabs are opened. Tabs showing any other site, and Copilot tabs beyond the needed number,
   are never touched. The connection to Edge is a race: one patient attempt starts at once, more join every few
   seconds on the other route, the first to connect wins and is printed (`WINNER: attempt n of m, websocket|http route`);
   that route is remembered (`state\connect-route.txt`) and tried first next time. A slow attempt is never restarted.
   **Fast driver start:** started from S:, Playwright's driver (node.exe + package, ~100 MB) takes 90-135 s on the VDI.
   Measured (2026-10-30): node.exe alone 1.1 s, reading the driver files 3.0 s, but loading the driver program 83.9 s
   on *every* start (a run-from-swap node.exe copy made no difference and was removed). Suspected cause: the driver's
   WebSocket library searching every folder Node knows for two optional add-ons that are not installed; the tool now
   sets `WS_NO_BUFFER_UTIL` / `WS_NO_UTF_8_VALIDATE` (process only) so that search never happens. `Run.cmd check` runs
   the driver program once traced and lists every lookup, file or system call that took over 0.3 s.
   **Nothing is ever written to the system drive** (a per-user quota there gave "No space left on device"). Only if
   `driver_cache_dir` is set to a folder on a *local disk that is not the system drive* is the driver copied there
   once (space available to you checked first, a failed copy removed at once) and used only while it matches the
   original on S: (checked every run, including a checksum); later runs then start the driver in seconds. Not set =
   no copy, the driver starts from S:. A copy left on the system drive by earlier versions is removed at startup.
   If the copy is refused (e.g. a policy blocking programs there) or does not start within 30 s, the log says
   `playwright_driver_start: local copy ...` and the driver starts from S: instead. `Run.cmd check` prints how long
   the driver took and where from, how long `node.exe` alone takes, and which local non-system disks exist.
   `Run.cmd` prints `Starting...` before Python even loads. While the cursor
   question (and later the model question) is on screen **nothing else is printed**: lines made meanwhile appear right
   after your answer. To make tabs load sooner: a launched Edge gets every needed tab on its command line, a joined Edge
   gets the missing ones over its plain loopback HTTP endpoint (`PUT /json/new`) before Playwright is connected, the
   Playwright driver boots while Edge starts, and loopback calls never go through a system proxy.
   **Tab tour:** it starts the moment the tabs are opened (over the loopback HTTP endpoint, before Playwright has
   connected, since connecting waits on busy loading tabs), then Playwright takes over. Until every tab shows the model button, the tool visits each still-loading tab in turn (brought
   forward, pointer over the page, at most 1 s per tab) because a Copilot tab in front gets ready much sooner; it
   pauses while the model list is read and never leaves a tab showing the sign-in page. It stops by itself once all
   tabs are ready.
   **Edge stays out of your way:** you can keep working (typing in the terminal or any other app) while the run works
   in Edge. Every Copilot tab stays in ONE Edge window; whenever the tool selects a tab (or Edge starts), a small focus
   guard gives the keyboard straight back to the app you were in (about 25 ms). Clicking Edge yourself is never undone.
   Sign-in, the tab tour and readiness do not wait for the moving-cursor answer; the tour stops as soon as every tab
   shows the model button. Only the model question waits until you have answered the cursor question.
1. **Sign-in detection.** Edge opens; the tool keeps checking (every 0.25 s, no overall limit) until a Copilot tab shows
   both the chat composer and the model button, then continues **by itself**. If you are already signed in nothing is
   asked. After 1 s of waiting it tells you to sign in in the Edge window; pressing **Enter** in the terminal is only a
   manual fallback (ignored when the terminal is redirected).
2. **Model discovery** (~3 s): the model button's menu is read – modes (Auto, Quick response, Think deeper) and every
   provider submenu (Claude, GPT …); nothing is hard-coded, so added or removed models just appear. A submenu opens
   when the cursor hovers its entry; if the hover does not take (busy or background tab), the keyboard opens it
   (Right arrow) within ~2.5 s instead of failing the attempt after 20 s.
   Copilot tabs sign in and load at different speeds, so if the first tab's picker is not ready the script keeps trying
   it and every other signed-in Copilot tab (re-scanned each second) until one shows the list. There is no fallback
   such as sending with Copilot's current model: you always get the list and always choose. Only Ctrl+C ends the wait.
3. **You choose** by number, **Enter = default = the last entry listed under GPT** (no version parsing). Setting
   `requested_model` skips the question. `model_prompt_seconds` (default 0 = wait) can make an unattended run continue
   with the default.
4. **Every tab ready before the first send (in parallel with steps 2-3):** the model is asked as soon as ONE tab shows
   the list, but nothing is sent until every prepared tab shows the composer and the model button. A tab left on a
   sign-in page is reloaded once as soon as sign-in is detected; a tab still not ready is reloaded silently every 30 s,
   and after 3 reloads it is left out of the run with a warning (it is never sent into).
5. **The model is set per case, not in advance:** each tab selects the chosen model as the first step of its own send
   (model -> files -> message -> Send), then the next tab does the same. A tab that has only just become ready may ignore
   the first click on the model button: it is clicked again every 2.5 s instead of waiting 20 s for a menu. If the tab already shows that model, nothing is
   clicked, but the moving cursor still shows a pretend click on the model button.

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

**Attachment names** are matched by identity but tolerantly: status or size words, a hidden extension or an ellipsis around a name are
fine, and a file called e.g. `Pending_payments.pdf` is not mistaken for a running upload. While a big file loads Copilot may show it under a
temporary hash-style name: that counts as *still loading* (stall window 3x `upload_no_progress`), never as an error. Only **more chips than
files** fails at once ("unexpected attachment present"); the chip names are then saved to `RUNTIME_ROOT/state/upload-mismatch-<alias>.json`
(local only).
demand (V1 is meant for a handful of controlled cases; recycling answered tabs is a later version).

**How files are attached (no Windows file window, normally no menu).** First choice: the whole file list is handed in
one call straight to Copilot's own hidden multi-file input (`#upload-file-button`, the input behind "Upload images and
files"), so the `+` menu never opens; the moving cursor only shows a pretend click in the composer meanwhile. If
Copilot shows no chip, busy chip or upload error within 6 s, that page does not take files this way and the run uses the
menu route below for the rest of the run (*Locally verified*: the input exists on a personal Microsoft 365 Copilot
profile; the VDI tenant is to re-confirm). Menu route: the `+` button's "Upload images and files" entry normally opens
Windows' own file dialog, which broke sends on the VDI. The tool registers a Playwright *file-chooser listener* on every
tab, which intercepts the chooser: the dialog never appears and the full file list is handed to the page in one call
(`set_files`). Only that one menu entry is ever clicked; the quick-action buttons under the composer are never touched.
After a real upload failure the sequential route (one file at a time) is the fallback, as before.
A simulated drag-and-drop was tried and **does not work** in this Copilot (the page receives the drop, ignores it).

**Failures:** if Copilot shows a real upload error with its own *Try again* button, that button is clicked (max 2
times, only inside an upload-error message – never the response *Regenerate*). No such button (or still failing) →
the tool waits `upload_error_grace_seconds` (3 s) and looks again, because the message sometimes clears by itself.
Still there → inside the same try, a **fresh chat in the same tab and the same files attached again**, up to
`upload_recoveries` (2) times. Nothing has been sent at any of these points. Still failing → **the same tab is
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

### Moving cursor (optional)
At the start of `run` the tool asks: *Show a moving cursor in Edge so you can follow what the tool does?*
**Enter = Yes, N = No** (no time limit). It makes each case about 2-3 seconds slower. To stop being asked, set
`visual_cursor` in `setup` (or `sender_config.json`): `ask` (default), `on` or `off`.

What you see: one dark-blue pointer in the Edge page that is in front. It glides to each thing the tool works on and
stays where it last acted.
- **Solid ring**: the tool really clicked there. It means the click was sent, not that it worked.
- **Dashed ring**: a pretend click, shown for things that are not a click by the tool: closing a menu (the tool
  presses Escape), the upload "Try again" button (clicked by a page script), and a change of browser tab.
- **Blue glow around the message box**: the message is being typed in (one normal fill, no extra click).
- **Tab change**: the pointer goes to the right or left edge of the page (the side of the next tab), gives a pretend
  click, then the tab really changes and the pointer stays at that spot. A web page cannot draw on Edge's own tab bar.

It is only a drawing: it cannot be clicked, never types, clicks or focuses anything, and if it fails the run carries
on exactly as without it. Work on hidden background tabs is not drawn. It is hidden in diagnostic screenshots and
removed from every tab when the run ends. Size, colours and timings can be changed with `visual_cursor_options`
(see `VISUAL_DEFAULTS` in section 8b of the script); for example `{"move_ms": 150}` makes it faster.

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
an Edge that is **already open on the configured `cdp_port`** (opened by this tool earlier or by any other process) is joined as it is: its signed-in Copilot tabs are used directly (each reset to a fresh chat if it was not one), the model list is read from them right away, and the pool is **topped up** to `min(warm_tabs, cases)` (blank tabs reused first, then new tabs; other sites never touched). Only when nothing is listening does the tool start its own Edge on the dedicated profile and prepares the same number of tabs. Other debug sessions on other ports are never touched, so keep `cdp_port` different from them.
Handshake escalation `3, 6, 12, 20, 30 s`, browser WebSocket first then HTTP, **sequentially**, discovery refreshed
each round, every attempt owned and cancelled/awaited. A bounded `reconnect_budget_seconds` ends in a durable
`recovery_required` run state (exit code 4) instead of looping. Ctrl+C stops at the next safe point (second press
aborts); Edge is never killed, no cookies or profile data are touched.

## Diagnostics and sharing

Console + `RUNTIME_ROOT\logs\events-*.txt` (one JSON event per line, `.txt` so it can be attached to Copilot) + `summary-*.json` use aliases and labels only; paths, IDs, URLs and
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
| Upload-error → Try again ×2 → 3 s second look → fresh chat + attach again ×2 → same-tab refresh → 5 tries; model picker with a different model list | **Tested here** with a fake page. Upload errors were seen live on an account that refused uploads; the extra recovery steps are not yet confirmed live |
| Green/yellow status colours | **Tested here** (escape codes; look on your console) |
| Unresponsive renderer on a real Edge | **Needs your Windows/VDI** (error path tested with a fake) |

## What was reused from the earlier runner, and what was dropped

Reused ideas (re-written, not imported): dedicated-profile Edge launch with loopback debugging; `/json/version`
polling and endpoint validation; browser-WebSocket-then-HTTP `connect_over_cdp` with the 3-6-12-20-30 s ladder;
exact target-ID rebinding; the composer/attachment/Send/Stop selectors; bulk file assignment (originally `DOM.setFileInputFiles`; now first straight onto Copilot's own `#upload-file-button`
input, verified by a chip appearing, else through an intercepted file chooser); Send-versus-Stop guarding; first-launch private install.
Dropped: the old audit workflow (CSV/allow-lists, merge-status JSON, merged PDFs, case-size categories, multi-case
packing, audit prompt/response contract/result parser), indefinite reconnect loops, permissive Edge flags, Temp
deletion routines, and the thousands of lines of diagnostics/override layers.

## Run report (your eyes inside the VDI)
At the end of every `run` (also after a crash, a stop, or a failure before the queue starts) the tool writes
`RUNTIME_ROOT\logs\run-report-<time>.md` and prints its path. It is meant to be pasted to another LLM: it starts with a guide (how the
tool works, case states, invariants, what to answer), then the outcome and exit code, OS / Python / Playwright / Edge versions, CPU,
memory, disk, screen, time zone, remote-session hints, the script hash, **every setting**, storage facts, browser facts (launched or
joined, reconnects, tabs with ready/visible state, model chosen), one block per case (attempts, routes, reasons, gaps between steps,
document counts / sizes / name *shapes*), how the attachment chips looked when names did not match, errors with call frames, the full
event timeline and the last console lines. It is deliberately **not redacted**: it contains the real paths, instruction IDs and folders, file names, URLs, chip labels, environment variables and full error tracebacks, so it stays on the VDI and the user hands it over. The only things never included are credentials: environment variables whose names look like secrets are hidden, and no cookie, token or `.env` content is read. The console and `events-*.txt` logs stay redacted.
