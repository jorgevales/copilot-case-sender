# Daily statics review — System 1 documents + System 2 snapshots + Copilot

Three parts work **at the same time**, so Copilot can review case 1 while System 1 is still downloading case 2:

| Folder | Part | What it does | Where it works |
|---|---|---|---|
| `1_system1_documents/` | **Part 1** | Per case: every Received Document + the Audit Trail from System 1 | the ONE System 1 tab of the **systems Edge** (port 9223) |
| `2_system2_snapshots/` | **Part 2** | All account snapshots from System 2, in bulk (usually done before case 1) | signed-in requests with the systems Edge's cookies (no tab) |
| `3_copilot_sender/` | **Part 3** | Sends each complete case to Copilot, one fresh chat, sent once, answer read back | the **Copilot Edge** (port 9333) |
| `orchestrator/` | Coordinator | Asks for the CSV, starts everything, hands complete cases over, writes the daily report | no browser |
| `shared/pipeline.py` | Contract | The small files the parts use to work together (read its header) | — |
| `shared/timing.py` | Case timing | The four case-timing values typed into each case's message (End Date, snapshot time, business days, age) | — |
| `shared/review_workbook.py` | Results workbook | `Copilot review results.xlsx` for the human reviewers (no extra package) | — |
| `guidance/` | Instructions | Only a README: your real result contract, daily instructions, base message and `<Sub Worktype>.md` files live on S: (never in the repository) | — |

All paths below are **symbolic labels** (`CASES_ROOT`, `RUNTIME_ROOT`, …): the real ones are typed once in setup and
stay in your local, git-ignored `sender_config.json`.

> **Do not do** (each one brings back an ~80 s startup delay or breaks a storage rule; see
> [Startup on the network drive](#startup-on-the-network-drive)):
> - Don't `.resolve()` / `os.path.realpath()` any path that goes to the Playwright driver (they turn `S:` into UNC).
> - Don't launch anything through `\\server\share`; start it through the mapped drive letter.
> - Don't fall back to the UNC path when the drive letter is missing: stop and say "map the drive".
> - Don't remove the `ws` add-on settings (`DRIVER_ENV`: `WS_NO_BUFFER_UTIL`, `WS_NO_UTF_8_VALIDATE`).
> - Don't change `HOME` / `USERPROFILE` / `NODE_PATH` to fix startup: they were never the cause.
> - Don't write anything to the system drive (per-user quota: "No space left on device").

## Quick start (Windows, no admin rights)

1. Keep this whole folder together on `S:`.
2. Double-click `Run.cmd`. It asks once for `PYTHON_EXECUTABLE` (paste the path; quotes are fine) and starts `setup`
   the first time.
3. `setup` asks for: `CASES_ROOT`, the **guidance folder** (one `<Sub Worktype>.md` per type), the `base_message` file
   (`daily_review_base_message.md`), **`daily_review_instructions.md`** (required: how Copilot analyses every case), the
   **result contract** (no template is offered any more), `PYTHON_EXECUTABLE`, `EDGE_EXECUTABLE` and `RUNTIME_ROOT`. Then, **once**, it asks for the
   pages the tool connects to (your original Part 1 / Part 2 scripts have each one):
   - **System 1, where the case documents are downloaded from:** its inbox page (My Inbox), the document viewer
     marker (optional; without it documents are opened one by one, which is slower) and the brand control
     pattern(s).
   - **System 2, where the account snapshots are retrieved from:** a page that shows "logout" when signed in, the
     account summary page (with `{account}`), the text that marks a party profile link, and the party links page
     (with `{party}`).

   These stay only in your local, git-ignored `sender_config.json`. **No System 1 / System 2 address is written in
   the code.** Enter keeps a shown value. Run `Run.cmd setup` again at any time to edit.
4. `Run.cmd check` validates everything (nothing is sent). The first run installs the pinned packages (`playwright`,
   `requests`, `beautifulsoup4`) into `RUNTIME_ROOT\deps`.

## A daily run (`Run.cmd` / `Run.cmd run`)

1. **The CSV.** Paste its path (the last one is offered). It needs the columns `instruction id` (the old
   `instrcution id` spelling also works), `Client Account Number`, `Client Number`, `Process Status` and
   `Sub Worktype`, and (optional, last) **`End Date`** - when the case ended, in UK time. Only **Ended** rows are
   reviewed. Every other row (Voided, blank, an invalid ID, a duplicate, no account) is listed under **SKIPPED** with
   its reason before anything starts. A CSV without any End Date is sent anyway (see [Case timing](#case-timing)).
   Before this, an older results workbook is renamed (see [Results workbook](#results-workbook-copilot-review-resultsxlsx)).
2. **The plan.** For each Ended row: *download and send*, *complete already* (send only), *already sent earlier*
   (sent-ledger), or *held* (for example, a folder for that ID already in CASES_ROOT that this tool did not make).
   Sub Worktypes without an instructions file are warned about.
3. **Two Edges open.** The **systems Edge** (System 1 + System 2, its own remembered profile) and the **Copilot
   Edge** (its own remembered profile). Two profiles are needed because Edge locks a profile to one running Edge.
   Both remember your sign-ins between days.
4. **Questions** (Part 3, in this console): moving cursor? Then the Copilot model.
5. **Sign in everywhere, then press Enter.** Part 1 checks there is exactly ONE System 1 tab showing My Inbox; Part 2
   checks the System 2 page shows "logout"; Copilot is already confirmed. If something is missing it is named, and
   you press Enter again. After this you can walk away.
6. **Everything runs together:**
   - **Part 2** fetches every account snapshot in bulk (same-day snapshots are reused).
   - **Part 1** downloads case after case into the hidden `CASES_ROOT\.work\<id> - <ACCOUNT>`.
   - **The coordinator** moves a case into CASES_ROOT the moment it is complete: documents + Audit Trail +
     snapshot + an instructions file for its Sub Worktype. It copies the snapshot in and renames the folder in one
     step, so Copilot never sees a half-downloaded case.
   - **Part 3** picks the case up within seconds. It attaches `daily_review_instructions.md`, that type's `.md` and the
     result contract, then the snapshot, the Audit Trail and the documents (Received Documents order), types the base
     message with this case's timing filled in, sends once and collects the answer while the next case is still
     downloading.
7. **The daily report**: on screen (a "Reviewed X of Y ... N need you" line, then everything that needs you) and as
   `RUNTIME_ROOT\reports\daily-report-<run>.csv`. One line per CSV row: sent + Copilot's verdict, held + why,
   skipped + why, left-out files and the Copilot chat's address. A valid answer's detail gives the discrepancy type and
   the counts (comparisons, differences, not compared, rule checks, not satisfied, not determinable). Case timing sent as
   'Not provided' is marked `CHECK:` there, so the row is listed as needing you. Under the report the console names
   this run's **results workbook**.

**Files for the reviewing Copilot** (all in `RUNTIME_ROOT\logs`, attach them after a run):
- `run-report-<time>.md`: the sender's run (settings, timings, every case, errors and abnormal states, the whole
  workload, this run's Copilot Edge messages);
- `copilot-responses-<time>.md`: every Copilot chat in full: what was sent, every reply, whether it followed the
  contract (and which keys it used instead), whether it reads like a refusal, the buttons around the reply and the
  items of its `...` menu, and every fix-up request with whether it arrived;
- `pipeline-report-<run>.md`: what System 1 and System 2 saw: per case the search, the Sub Worktype, every document
  with the way it was downloaded (or why not, with each request's status / type / size), per account the snapshot
  (reused or saved, when), and this run's systems Edge messages;
- `captures\<run>\`: a screenshot, the visible text and the page file wherever a case, a document window or a System 2
  page failed.
None of them lists environment variables or user / machine names, and every web address is cut before its `?`. Page
copies are otherwise kept whole (Copilot needs them), so each file opens with a note telling the reviewing Copilot to use
everything but never repeat company details (names, IDs, account numbers, hosts, addresses, access codes) in its report.

**Held, never sent** (each with a plain reason in the report):
- System 1 shows a different account than the CSV;
- Part 1 failed the case twice (a program error - a mistake in the code, not a page problem - is not retried; its full
  trace is in the pipeline report, and the same one on a second case stops Part 1: the rest are listed as not reached);
- no instructions file for its Sub Worktype - checked on the search result, **before anything is downloaded**;
- My Inbox says "Your inbox is empty" for its ID (not in My Inbox under the filters that are on - they are named), or
  shows several results;
- no System 2 snapshot (failed after one retry);
- a folder with the same name already in CASES_ROOT.

**Stops and pauses**
- **System 2 Access Denied** (after one retry) **stops everything safely**: no new downloads, no new sends. A send
  already clicked is confirmed, and answers already sent are still collected.
- **System 2 sign-in expired**: a pause. The console asks you to sign in again in the systems Edge and press Enter.
- **Ctrl+C**: stops the sender safely (twice = at once); the other parts stop through the same signal.
- **Running again the same day continues**: complete cases are not downloaded again, unfinished `.work` cases are
  redone, snapshots are reused, and nothing in the sent-ledger is sent again.

**Where things are** (all on S:, nothing on the system drive):
- `RUNTIME_ROOT\pipeline\runs\<run>\`: the plan, signals, `logs\part1.log` / `part2.log`, held cases;
- `pipeline\downloads\`, `pipeline\cases\`, `pipeline\snapshots\<day>\`: the hand-over files (see `shared/pipeline.py`);
- `profile-systems\` and `profile\`: the two Edge profiles (sign-in data: never share them).

**Evidence**: *Locally verified* with fake System 1 / System 2 / Copilot pages and real processes (`tests/`). Still
*Proposed* until confirmed on the VDI:
- the real System 1/2 pages through the new joins;
- the cookie read over DevTools;
- the Access-Denied page wording;
- the background-window switches on the systems Edge.

`Run.cmd send-only` is Part 3 alone, as before the pipeline. It sends the folders already in CASES_ROOT with one
general review guidance file (`instructions_path`).

---

# Part 3 — Copilot sender (details)

Everything below describes `3_copilot_sender/copilot_case_sender.py` (read top to bottom; its sections are numbered).
In the pipeline each case gets `daily_review_instructions.md` + its Sub Worktype's `.md`. With `send-only` it gets
`daily_review_instructions.md` + the one shared **review guidance** file (`review_guidance.md`). In both, the documents
are attached, your `base_message` is typed (with the case timing filled in),
the case is **sent once**, and the outcome is saved before the next case starts.

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
   **Driver start:** about 2-4 s on the VDI (was ~83 s); see [Startup on the network drive](#startup-on-the-network-drive).
   Every startup step prints a `startup` line with its time and `at Ns` since launch (driver import, driver start,
   Edge connect, each tab ready, all tabs ready), also in the minimal console.
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

`daily_review_instructions.md` tells Copilot *how to analyse any case*, the Sub Worktype `.md` (or the review guidance)
*which checks apply*, and **`result_contract.md`** *exactly what to answer*. All three are attached to every case, in that
order (3 fixed files, so up to 17 case documents under the 20 limit). Your own copies live on S: and are **never** kept in
the repository.

**The answer** is a short plain-text section headed `Working`, then **one JSON object written as plain text on the
reply's very last line** (not a code box). Eight keys:

    {"COPILOT_RESULT":"Correct|Incorrect|Inconclusive","DISCREPANCY_TYPE":"None|Potential processor error|Discrepancy from aged case","CASE_END_DATETIME":"...","REGISTER_SNAPSHOT_DATETIME":"...","CLIENT_REQUEST":"...","JUSTIFICATION":"...","A_B_COMPARISONS":[{"ITEM":"...","Customer_expectation":"...","CUSTOMER_SOURCE":"...","System_data":"...","SYSTEM_SOURCE":"...","MATCH":true}],"RULE_CHECKS":[{"RULE":"...","STATUS":"Satisfied|Not satisfied|Not determinable|Not applicable","EVIDENCE":"...","NOTE":"..."}]}

The tool reads only the **newest assistant reply** of that case's chat once Copilot has stopped generating and the text
has been unchanged for `answer_stable_seconds`, and of that reply **only its last non-empty line** (read with its line
breaks). A result JSON found anywhere else (in the Working text, in a code block, or with text after it) is never
accepted: it gets a fix-up request that says so. The line is checked strictly (`validate_result`):
* exactly the 8 keys (in **any order**: the tool puts them, and every row's keys, in the contract's order before saving -
  the report says "key order fixed by the script"); each comparison row exactly 6 keys, `MATCH` `true`, `false` or
  `null` (null = could not be compared, not a failure); each rule-check row exactly 4 keys and an allowed `STATUS`; no
  empty text anywhere;
* at least one comparison row, always; `RULE_CHECKS` empty only in `send-only` (no Sub Worktype file attached), and then
  the result must be Inconclusive;
* **the result follows the contract's order of priority**: any `MATCH` false or rule `Not satisfied` ⇒ Incorrect;
  otherwise any `MATCH` null or rule `Not determinable` ⇒ Inconclusive; Correct needs every `MATCH` true and every
  rule `Satisfied` / `Not applicable`;
* **the timing label**: `DISCREPANCY_TYPE` is `None` exactly when no `MATCH` is false; otherwise it must match the case
  age statement typed into this case's message (without one the business-day gap decides; without both: Potential
  processor error), `JUSTIFICATION` must begin with `<type>:`, and an aged case must include the sentence
  "This case was completed more than 3 business days before the Register snapshot was captured.";
* `CASE_END_DATETIME` and `REGISTER_SNAPSHOT_DATETIME` must be copied **exactly** from the message.

Any of these is a hard error: fix-up request, then resubmission (below).

* **The format is in the message too:** the visible message ends with a short reminder of the eight keys and the
  last-line rule, saying it overrides any other answer format in the attached files (on the VDI all three first answers
  used another key set).
* **Not valid?** The tool immediately asks, in the same chat, for “your exact same answer, unchanged in content, in the
  correct structure” and names what was wrong – up to `fixup_requests` (3) times. It checks the request really appeared
  in the chat and records it.
* **Refusals:** a reply that reads like a refusal is marked as such in the responses file and is resent the same way.
  If the refusal removed the message box (only "New chat" left), with or without a normal reply, the case ends as
  `refused_chat_blocked` (“Copilot refused and blocked the chat”): its text and every button are recorded, **nothing is
  clicked**, and the run goes on with the next case.
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
* **Results** (local, in `RUNTIME_ROOT\results`): `<instruction ID>.json` (the validated answer in the contract's key
  order + the raw reply) and `results.csv` (unchanged: ID, status, verdict, rows, differences, tries, fixups, left-out
  files). For people: the [results workbook](#results-workbook-copilot-review-resultsxlsx). The console prints each verdict:
  Correct white on green, Incorrect white on red, Inconclusive black on yellow. A valid verdict, even *Incorrect*, is a
  finished case; the ledger still means “submitted” and a case with a valid answer is skipped on re-runs.
* **Re-runs:** a case submitted earlier but never answered gets its answer collected from its saved chat (same tab by exact
  target ID, else the saved conversation link in a new tab) – never resent for that. Cases submitted before V2 are not
  touched.
* Model picker: models that Copilot greys out (for example after a usage limit) are shown as unavailable, are never the
  default, and choosing one stops the run with a clear message instead of burning retries.

### Case timing

The base message **must** contain the four placeholders `{{CASE_END_DATETIME}}`, `{{REGISTER_SNAPSHOT_DATETIME}}`,
`{{BUSINESS_DAYS_GAP}}` and `{{CASE_AGE_STATEMENT}}`; `check` and every run refuse to start and name the missing one.
Each case's message gets its own values (`shared/timing.py`):

| Placeholder | Value |
|---|---|
| `CASE_END_DATETIME` | the CSV's `End Date`, exactly as written (e.g. `12/10/2026 02:17 PM`, UK time). An Excel serial number such as `46308.48958` (a comma also works) is written as `13/10/2026 11:45 AM`; a whole number as a date only |
| `REGISTER_SNAPSHOT_DATETIME` | the snapshot's Account Summary `metadata.retrievedUtc`, to whole seconds (`2026-10-14T12:23:38+00:00`) |
| `BUSINESS_DAYS_GAP` | working days from the case end to the snapshot, both in UK time (summer time worked out by the tool): the count starts the day **after** the case ended and includes the snapshot's day (ended Monday, snapshot Thursday = 3). Weekends and England & Wales bank holidays do not count: 2025-2028 are built in; add later years (or extra days) to an optional `bank_holidays.txt` beside `daily_review_instructions.md`, one date per line (`2029-01-01` or `01/01/2029`, `#` comments) |
| `CASE_AGE_STATEMENT` | 3 or less: `Case completed within the last 3 business days`; more: `Case completed more than 3 business days` |

Nothing is guessed: a missing or unreadable End Date, a snapshot without a readable time, a snapshot older than the
case end, or a year with no bank-holiday list makes the affected values **`Not provided`** (the contract has a rule for
that). The case is still sent, and the daily report marks it `CHECK:`. The values are kept in the journal (the case's
`preparing` record), so an answer collected after a restart is checked against what was really sent. In `send-only`
there is no CSV: the case end, gap and age are `Not provided` without a flag. *Locally verified* (tests); real CSVs:
*Proposed* until the VDI run.

### Results workbook (`Copilot review results.xlsx`)

`RUNTIME_ROOT\reports\Copilot review results.xlsx` holds **this run's valid Copilot answers** and is rewritten after
every answer (`shared/review_workbook.py`, written with Python's standard library: nothing to install).
* **One row per comparison and per rule check**; the case's own values are repeated on each of its rows so Excel's filter
  and sort work. Columns: Instruction ID, Client Account Number, Sub Worktype, Copilot result, Discrepancy type, Needs
  attention (the case's red and yellow items), Client request, Justification, then the row's Type (Comparison / Rule
  check), What was checked, Customer expectation, Customer source, Register value, Register source, Outcome, Evidence,
  Note, then Case end date/time, Register snapshot date/time, Business days gap, Case age statement, Client Number,
  Working section (not wrapped: read it in the formula bar), and empty reviewer columns: Reviewer agrees (Y/N list),
  Reviewer correction, Reviewer notes, Reviewer corrected result (Correct / Incorrect / Inconclusive list).
* **Colours** (whole row): red = no match / Not satisfied, yellow = could not compare / Not determinable, green = match /
  Satisfied, grey = Not applicable. Header row and ID column frozen, a filter on every column.
* **Start of a run** (the coordinator, or the sender in `send-only`): an older workbook is renamed
  `Copilot review results - older <the time it was made>.xlsx`. If Excel has it open the run says so and waits for
  Enter, then tries again; nothing starts before that.
* **During the run** a workbook opened in Excel is never waited for: a warning, then it is written again after the next
  answer. Still open at the end: a time-stamped copy `Copilot review results - <time>.xlsx` is written with every answer,
  and its name is printed. The coordinator prints the workbook's path under the daily report.
* Cases without a valid answer are not in the workbook (the daily report lists them). *Locally verified* (opened with
  openpyxl in tests); opening in Excel on the VDI: *Proposed*.

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
`max_attachments` (default 20, **the instruction files count**: daily instructions, Sub Worktype / review guidance,
result contract) is the *reference program's* number; live testing showed
Copilot accepting exactly 20 files on a personal profile, but check your tenant. A case with exactly the limit is
sent and the console says `at-limit`. When a case has **more** files, `over_limit_mode` decides:
* `send_partial` (default): attach the instruction files + the first documents in sorted order up to the limit, print a
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

## Startup on the network drive

Placeholders: `<drive>:` = the mapped network drive the tool lives on, `\\<server>\<share>` = its UNC form.

**What was proven on the VDI (2026-10-30)**

| # | Fact | Evidence |
|---|---|---|
| F1 | `node.exe` started through its **UNC path**: Node's search for a missing module walks past the share root to `\\<server>\node_modules`; Windows treats that as a request for a *share* named `node_modules` and fails only after ~80 s (`UNKNOWN`). | check: that one folder 80.7 s `UNKNOWN`, every other folder 0.0 s `ENOENT` |
| F2 | Every missing-module lookup paid that ~80 s. | whole `bufferutil` lookup 80.7 s; probe step 163.4 s = 2 x 80.7 s |
| F3 | Started through the **mapped drive letter** the walk stops at `<drive>:\node_modules`; the server root is never touched. | check: 15 folders, all 0.0 s; whole lookup 0.0 s |
| F4 | The home folder is not involved (`USERPROFILE`, `HOMEDRIVE`+`HOMEPATH`, Node's homedir all local; `HOME`, `NODE_PATH`, `NODE_OPTIONS` unset). | home-folder entries 0.0 s |
| F5 | Telling the `ws` library not to look for its optional add-ons removed the lookups that triggered F1: driver start ~83 s -> ~2-4 s. | earlier runs vs later checks (2.1, 2.8, 3.8 s) |
| F6 | `node.exe` alone ~1.5-2 s, reading the two driver bundles (6.4 MB) ~2.5 s, Python importing Playwright ~0.7-2 s: normal network-drive costs. | check steps 1, 2, 4 |
| F7 | The VDI has no local non-system disk, so no fast local driver copy (`driver_cache_dir` unset). | check |
| - | Ruled out: a run-from-swap `node.exe` copy (85.8 s, removed) and a driver copy on the system drive (user quota). | check / run report |

**Rules kept in the code** (`driver_command()` in section 7 is the one place that decides the driver path, for
`check` and the run alike):
- The driver (`node.exe` + `cli.js`) starts through the mapped drive letter (`drive_letter_spelling`); Playwright's
  package is put on `sys.path` through the drive letter too; the working folder is moved to the drive letter.
- No `.resolve()` / `realpath()` on those paths (they turn a mapped drive back into UNC); `HERE` uses `abspath`.
- No mapped drive covering the driver in this session = **stop** ("Map the drive, then run again"), never a UNC start.
- `DRIVER_ENV` (`WS_NO_BUFFER_UTIL=1`, `WS_NO_UTF_8_VALIDATE=1`) goes to the driver process only, as a second guard.
- `HOME` / `USERPROFILE` / `NODE_PATH` / `NODE_OPTIONS` are left alone; nothing system-wide, no admin rights.

**`Run.cmd check` is the regression test.** Healthy values on the VDI:

| Step | Measures | Healthy | Investigate if |
|---|---|---|---|
| 1/5 | `node.exe` alone | ~1.5-2.5 s | > 5 s |
| 2/5 | Node reads the 2 driver bundles | ~2-3 s | > 6 s |
| 3/5 | Every folder Node searches for a missing module (real paths: sanitise before sharing) | every folder 0.0 s, 15 folders | any folder > 0.5 s, any `UNKNOWN`, any `\\` entry (17 folders = UNC is back) -> `[FAIL]` |
| 4/5 | Python imports Playwright | ~0.7-2 s | > 5 s |
| 5/5 | Playwright driver starts | ~2-4 s | > 5 s |

`check` also marks `[FAIL]` if `node.exe`, `cli.js`, the working folder or the Playwright package is on a UNC path,
and notes it if Python itself was started by UNC path.

**Change log:** 2026-10-30 - driver launched through the mapped drive letter; a UNC launch caused an ~80 s
server-root share lookup per missing module. Driver start ~83 s -> ~2-4 s. Logs and journal are `.txt` (attachable
to Copilot); nothing is written to the system drive.

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
Needs `pytest`; refuses to run without `SENDER_TEST_ROOT` (never uses system Temp). `tests/test_sender.py` = Part 3,
`tests/test_pipeline.py` = the pipeline. The Part 2 tests need `requests` + `beautifulsoup4`; without them they are
skipped, not failed.

| Check | Status |
|---|---|
| Daily CSV: only Ended reviewed, every other row listed with its reason, column spellings, `;` CSVs | **Tested here** |
| Hand-over only when documents + Audit Trail + snapshot + Sub Worktype instructions are all there; held reasons; stop leaves `.work` alone; a crashed part counts as finished | **Tested here** |
| Part 3 live queue: a case finished mid-run is sent; its own instructions + contract, then snapshot, Audit Trail, documents in order; over the limit the last documents are left out | **Tested here** (real Runner, fake page) |
| Stop after a send: nothing new sent, the sent answer still collected; sign-in check names what is missing; pause → Enter → resume | **Tested here** |
| Part 1 case logic: `.work` folder, account mismatch held without retry, one clean retry, stop between documents and Audit Trail, one-System-1-tab check, no guidance = held before download | **Tested here** (scripted System 1 steps) |
| Part 1 whole document step end to end (direct, document window, straight download; windows closed, listeners removed); program error not retried with its trace; the same program error on 2 cases stops Part 1, the rest listed as not reached | **Tested here** (scripted browser) |
| My Inbox search states: 'inbox is empty' held with its filters (never clicked, no retry), a late single View used, several results held, loading / sign-in after 10 s = failure | **Tested here** (scripted My Inbox); the empty-inbox wording is the VDI's (2026-11-02) |
| Code check: no call passes a keyword already given by position (the bug that failed every case on the VDI, 2026-11-02) | **Tested here** (`tests/test_code_checks.py`) |
| Part 1 downloads: viewer address paired by document ID (position only when no address names an ID and counts match); a viewer page's own file fetched; a document window read without clicking inside it (its address, the file it shows, a download it starts, its own toolbar button only) | **Tested here** (fake responses / window; pairing run in Node). Real System 1 viewer: **needs your VDI** |
| Part 2: Access Denied vs expired sign-in vs normal page; Access Denied twice stops the run; expired → pause → fresh cookies; only System 2 cookies read; the failing page kept with addresses cut | **Tested here** (synthetic pages) |
| Copilot refusals: blocked chat (with and without a reply) ends the case, nothing clicked; refusal resent and confirmed; responses file with replies, buttons and `...` menu | **Tested here** (fake page). Real refusal page: **needs your VDI** |
| One whole run with real processes (coordinator + stand-ins for the three parts): skipped row, held case, two cases sent, daily report | **Tested here** |
| Real System 1 / System 2 pages, cookie read over DevTools, two Edges side by side, background switches | **Needs your VDI** |
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
document counts / sizes / name *shapes*), how the attachment chips looked just before Send (every case), errors, holds and
abnormal states (every case without a clean verdict, every CSV row that needs you, every warning / retry / fix-up, tracebacks),
the full event timeline, the last console lines, the Copilot Edge's messages written during this run, and (pipeline runs) every
CSV row of the whole workload. It keeps the real paths, instruction IDs and folders, file names, chip labels and full error
tracebacks, so it stays on the VDI and the user hands it over. Left out on purpose: the environment-variable list, user /
computer / domain names, and everything after `?` in web addresses (sign-in codes live there; the parameter names stay).
No cookie, token or `.env` content is ever read. The console and `events-*.txt` logs stay redacted. Beside it:
`copilot-responses-<time>.md` and (pipeline runs) `pipeline-report-<run>.md` + `captures\<run>\` - see "Files for the
reviewing Copilot" above.
