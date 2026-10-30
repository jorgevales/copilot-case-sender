# CLAUDE.md — Automatic daily statics review 2 (Copilot Case Sender)

Handoff for new Claude Code sessions. Read this first; it should be enough to start working.
User is a medium beginner: plain language, minimal jargon, don't push or publish without asking.

## What this project is

A Windows tool that sends **case folders** to Microsoft 365 Copilot through a dedicated, manually signed-in Edge window
(Playwright over CDP), then reads Copilot's answer back as strict JSON. Per case: one fresh Copilot chat, the case documents
plus a shared `review_guidance.md` attached, `base_message` typed, **sent exactly once**, outcome saved before the next case.
It is the rewrite ("2") of the older "Automatic daily statics review" runner (old audit workflow, merged PDFs, permissive
Edge flags were dropped on purpose; see README last section).

Real runs happen on the FNZ VDI, with everything on the `S:` drive. Local copies are for development and tests only.

## Files

| File | Role |
|---|---|
| `copilot_case_sender.py` | The whole program (~3,300 lines, stdlib + pinned `playwright==1.64.1` installed at first launch). |
| `Run.cmd` | Launcher; asks once for embedded `python.exe`, saves it in `local_python.cmd`, runs `python -B -E -s copilot_case_sender.py %*`. |
| `result_contract.md` | Strict answer format attached to every case (template, editable). |
| `README.md` | Full user/design documentation. Authoritative for behaviour; read the relevant section before changing logic. |
| `tests/test_sender.py` | 72 tests using fake page / fake Playwright. Needs `SENDER_TEST_ROOT` env var (a folder on approved storage), never system Temp. |
| `.gitignore` | Excludes `sender_config.json`, `local_python.cmd`, `sender_runtime/`, logs, `.venv/`, `.env`. |

Commands: `Run.cmd setup | check | plan | run` (`run --detail minimal|full`).
Tests: `set SENDER_TEST_ROOT=<folder>` then `<python> -B -m pytest -p no:cacheprovider tests`.

## Code map (`copilot_case_sender.py`, numbered sections)

1. BASICS (l.47) · 2. PATH POLICY (195; `PathPolicy`: `S:` ≡ its UNC share = one approved storage) · 3. CONFIG (318) ·
4. STORAGE, REDACTION, LOGGING (539) · 5. STATE: append-only journal + `sent_instructions.txt` ledger (722) ·
6. CASES: discovery, inventory, attachment plan (843) · 7. DEPENDENCIES (1054) ·
8. BROWSER: `EdgeBrowser` + `CopilotTab` page adapter (1110) · 8b. VISUAL CURSOR: optional `VisualCursor`, one shared
instance at `CopilotTab.cursor`; every `CopilotTab` click/hover/fill and tab switch goes through it (off = the plain call) · 9. TRANSACTION, QUEUE, RETRIES: `Runner` (1768) ·
10. COMMANDS: `setup/check/plan/run` + `main()` (2464).

Graphify highlights (497 nodes, 955 edges, 29 communities, no import cycles): god nodes are `CopilotTab` (40 edges),
`run()` (34), `Runner` (32), `guarded()` (23), `EdgeBrowser` (20), `SenderError` (17; `UiError`, `AttemptFailed` inherit it),
`PathPolicy` (15), `check_command()`, `run_command()`. Tests' `FakeTab` mirrors `CopilotTab`, so changing the page adapter
usually means updating `FakeTab` too. Full graph: `graphify-out/` in this folder (`graph.json`, `GRAPH_REPORT.md`, `graph.html`; git-ignored by hand if you don't want it committed). The older wrapper run (`Refresh-CopilotHandoff.py`, for Copilot Chat) also left an untracked `Copilot handoff/` folder; ignore it.

## Behaviour that must not break

- **At-most-once sending.** `send_intent` is journaled before the Send click; `submitted` confirmed in the *same tab* by exact browser target ID, then the ID goes in the ledger. Uncertain sends are never auto-resent. The only legitimate resend is "no valid answer exists" (same tab refreshed to new chat, within the 5-try budget).
- **Model list is mandatory.** `discover_models_anywhere` retries every signed-in Copilot tab until one shows the picker; never fall back to Copilot's current model. Only a stop request ends the wait.
- **Foreground each tab** (select it in its Edge window) before checks and immediately before Send click, always via `front(page)`, never a bare `bring_to_front`. **Edge never keeps the keyboard:** `FOCUS` (`FocusGuard`, started in `run_async`) remembers your last non-Edge window and hands the foreground back within ~25 ms when Edge grabs it during a marked moment (`front`, `/json/activate`, new tabs, Edge launch, which also starts with SW_SHOWNOACTIVATE). All Copilot tabs stay in ONE Edge window (user rule; no extra windows). Edge work (sign-in, tour, readiness) never waits for the cursor question: `apply_cursor` switches the cursor when it is answered, `DeferredStdin` starts the terminal reader only after it, and only the model question waits for it. *Locally verified* on real Copilot: focus lost once for 10 ms at Edge launch, never while joined or per case. Safety net: if Windows refuses to hand focus back (`FOCUS.failed`), quiet mode (`skip_front`) stops selecting tabs while you are in another app; `foreground()` then turns on focus emulation and works in the unselected tab (*Locally verified*: 3 cases on real Copilot, nothing sent).
- **Files attach directly first** (`CopilotTab._direct`: `set_input_files` on Copilot's own `#upload-file-button`, cursor pretends a composer click; success = a chip/busy/error within `DIRECT_SIGN_SECONDS`), else via **Playwright file-chooser interception** (no Windows dialog) for the rest of the run (`CopilotTab.direct_ok`). Simulated drag-and-drop does not work. Only the "Upload images and files" menu entry is clicked.
- **Startup console:** `Log.hold/release` keep every line (and `say()`) back while the cursor or model question is open. `TabTour` visits still-loading tabs (<= `VISIT_SECONDS`) until all are ready, pauses for model reading, never leaves a sign-in page. `EdgeBrowser.http_topup` opens missing tabs over loopback HTTP for a joined Edge, and `http_tour` visits them over HTTP (`/json/activate`) from that moment until Playwright's `TabTour` takes over (`tour_handoff`), because connecting waits on busy loading tabs; `http_tour` reads each tab's readiness over its own DevTools socket (`cdp_page_eval`, stdlib WebSocket, loopback/our port only) and stops as soon as every needed tab is ready, never running on while Playwright is still connecting; a launched Edge gets all URLs on its command line. Loopback HTTP uses a no-proxy opener (`LOOPBACK`). The model step shows a cursor pretend click even when no click is needed. `_open_model_menu` re-clicks the model button every `MENU_RETRY_SECONDS` (a just-ready tab ignores early clicks; *Locally verified* on real Copilot) instead of waiting `op_timeout`.
- **Max 20 attachments** (review guidance counts). Over limit: `send_partial` (default) or `block`; never merge/convert/drop silently.
- **Answer contract:** plain-text single-line JSON, exactly keys `COPILOT_RESULT` (`Correct|Incorrect|Inconclusive`), `JUSTIFICATION`, `A_B_COMPARISONS` (rows with `ITEM`, `Customer_expectation`, `System_data`, `MATCH`). Verdict must agree with rows. Invalid → up to 3 fix-up requests, then resubmit.
- **Storage:** every write inside `approved_root` (audit hook blocks others); no blind UNC conversion; lost storage stops the run, never falls back.
  **Never write to the system drive** (user rule 2026-10-30: per-user quota → `[Errno 28]` on the VDI; users have no admin rights). **One exception:** the Playwright driver (node.exe ~90 MB + package ~13 MB) may be copied to `<driver_cache_dir>\playwright-driver\<version>` (`DriverCache`, `WRITE_GUARD['extra']`), only when `driver_cache_dir` is on a local non-system disk (`drive_class == "fast"`; unset = no copy), because starting it from S: took 91–135 s on the VDI. Measured on the VDI: node --version 1.1 s, bundle reads 3.0 s, `cli --version` 83.9 s on every start; a node.exe copy with PE flag `IMAGE_FILE_NET_RUN_FROM_SWAP` did NOT help (85.8 s; removed, `RUNTIME_ROOT\driver-node` is deleted at bootstrap). Current suspect: ws's optional `bufferutil`/`utf-8-validate` lookup through Node's global folders; `bootstrap_storage` sets `WS_NO_BUFFER_UTIL`/`WS_NO_UTF_8_VALIDATE` (*Proposed*). `check` runs `cli --version` under `DRIVER_TRACE_JS` (`node --require`), printing calls over 0.3 s. `clean_legacy_driver_copy` removes the old `%LOCALAPPDATA%\CopilotCaseSender` copy. Logs are `events-*.txt` and the journal `state\journal.txt` (old `journal.jsonl` still replayed first) so they can be attached to Copilot. It is used only if it matches the S: original (file list, sizes, dates) and node.exe's sha256 matches; otherwise the driver starts from S: and a fresh copy is made in the background. `start_driver` bounds the local copy by `LOCAL_DRIVER_SECONDS` (30 s) and falls back to S: if it is blocked or hangs (*Proposed* for the VDI). Never case data, logs or state. *Locally verified*: driver start 1.2 s from the copy after a one-off security scan.
- **Browser safety:** loopback only, dedicated profile under `RUNTIME_ROOT\profile`, an Edge already open on `cdp_port` (any process) is joined and its Copilot tabs reused; in both scenarios `EdgeBrowser.prepare_tabs` tops up to `min(warm_tabs, cases)` (existing Copilot tabs, then blank tabs, then new; other sites never touched) at startup, and `ready_tabs` holds the run until every tab is ready (30 s reloads, dropped after 3); the model is set per tab inside `attempt` (no background model tasks); Edge start, the cursor question and the queue scan (`scan_queue`) all begin at launch in parallel; only if the port is silent do we launch our own; other Edge debug sessions on other ports (e.g. 9223, folder `.edge-debug-9223` is excluded from the queue) must never be touched; Edge is never killed.
- **Logs/console/summaries are redacted** (aliases only). Real paths, IDs, URLs live only in `RUNTIME_ROOT\state\`. Screenshots are off by default and local-only.
- **Visual cursor is observation only.** It wraps the original Playwright call (called exactly once, same args, original
  error re-raised), swallows its own failures, is capped at `cap_ms` per action, and draws only on the front tab. New
  page actions in `CopilotTab` should go through `self.cursor.click/hover/fill`. It is built with DOM calls + a
  constructed stylesheet in a closed shadow root (no HTML strings) so Copilot's CSP/Trusted Types are not tripped.
- Python launches with `-B -E -s`; no venv, no pip global, no registry/PATH changes.
- Sign-in and MFA are always manual. Never read or copy `.env`/credentials.

## Status

- Git repo on `main`, in sync with `origin/main`, last commit `9e8e0f2` "V2: result contract, plain-text JSON answers, background collection, fix-ups". No remote pushes without the user's say-so.
- Live-tested on a personal Microsoft 365 Copilot profile with synthetic files (about 7–15 s per case, V2 verdicts read back). Still **needs confirmation on the real VDI**: real `WNetGetUniversalNameW` on `S:`, embedded `._pth` + wheels, replace/lock on the share, unresponsive-renderer path, `.crdownload` location.
- Carry-over from the old project (see memory `automatic-daily-review-project`): the Edge debug endpoint not answering in 60 s on the VDI, and the plan to later run on any environment, not only `S:`. Whether this rewrite fixes it is unconfirmed; `EdgeBrowser.connect` is now a racing handshake (no restarts; attempts join at `RACE_GAPS`, alternating WebSocket/HTTP, first to connect wins, winner route remembered in `state\connect-route.txt` and printed as `WINNER: ...`); the old 3/6/12/20/30 s ladder is gone (`handshake_timeouts` is no longer read).

## Working rules for this project

- Minimal, targeted changes; match the existing numbered-section style and comment density.
- Label evidence as *VDI confirmed* / *Locally verified* / *Proposed*.
- Run the tests after any change in sections 8–9; add a fake-page test for new Copilot-UI behaviour.
- Do not hand-edit `Copilot handoff/` output.

## graphify

`graphify` is not on PATH. Use `"..\..\.tools\graphify\.venv\Scripts\graphify.exe"` (relative to this folder) wherever the rules below say `graphify`.

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Keeping the graph fresh

- `graphify-out/` and `.claude/` are git-ignored on purpose (local only). Commit them only if the user asks.
- Refresh the graph every time new code or docs land (after edits, a commit, or pulling a push): `graphify update .` for code (no cost); after doc changes (README, result_contract, this file) also run `graphify extract . --backend claude-cli --max-concurrency 1`, then `graphify cluster-only . --backend=claude-cli`. Use the executable path given above.
- Git hooks (post-commit, post-checkout) are installed locally via `graphify hook install` and rebuild the code graph automatically after each commit/checkout. Doc changes still need the manual `extract` step above. Hooks live in `.git/hooks` and are not pushed; reinstall them after a fresh clone.
