# Part 1 — System 1 documents

`system1_documents.py` downloads, case by case, every **Received Document** and the **Audit Trail** from System 1.
It uses the one System 1 tab in the **systems Edge** (port `systems_cdp_port`, default 9223).

- **Started by** the coordinator (`orchestrator/run_pipeline.py`), never by hand. It runs in the background with no
  console window. Its output goes to `RUNTIME_ROOT\pipeline\runs\<run>\logs\part1.log`, and its progress lines show
  on the sender's console as `system1`.
- **Waits** until you have pressed Enter and every part has confirmed its sign-in. Its own check: exactly ONE System 1
  tab is open and it shows My Inbox.
- **Per case** (CSV order): search the ID → open it → verify the exact ID **and** the CSV's account on Overview
  (a different account = the case is held, never downloaded) → download into `CASES_ROOT\.work\<id> - <ACCOUNT>` →
  back to My Inbox. When it is done it writes `RUNTIME_ROOT\pipeline\downloads\<id>.json`. That file lists the
  documents in Received Documents order, the Audit Trail and the Sub Worktype; System 1's Sub Worktype wins over the
  CSV's, and a difference is reported.
- **Retries** a failed case once (the folder is started clean). A hold or a stop is never retried.
- **Never touches** your System 2 tab: it only works in tabs on System 1's host.
- **Stops** when the shared stop signal is set: before the next case, or between the documents and the Audit Trail.
  An unfinished case stays in `.work`.

Kept unchanged from the original script: every UI step, selector, wait and download route (direct, popup, nested
popup), plus `studio_visual_cursor.py` (the moving cursor; `STUDIO_VISUAL_CURSOR=0` turns it off).
Changed for the pipeline:
- every System 1 page comes from the config, pasted once in setup (`system1_url`, `system1_viewer_marker`,
  `system1_brand_text`, `system1_brand_html`). None is written in this code (the repository is public). The inbox
  and case pages are recognised as "under the inbox page's address";
- Edge is joined, never launched here;
- Playwright starts the fast way, with Part 3's driver start: the drive letter, the driver-only settings and nothing
  written to the system drive;
- Pillow is only imported by the unused screenshot helpers.
