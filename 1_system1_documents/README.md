# Part 1 — System 1 documents

`system1_documents.py` downloads, case by case, every **Received Document** and the **Audit Trail** from System 1.
It uses the one System 1 tab in the **systems Edge** (port `systems_cdp_port`, default 9223).

- **Started by** the coordinator (`orchestrator/run_pipeline.py`), never by hand. It runs in the background with no
  console window. Its output goes to `RUNTIME_ROOT\pipeline\runs\<run>\logs\part1.log`, and its progress lines show
  on the sender's console as `system1`.
- **Waits** until you have pressed Enter and every part has confirmed its sign-in. Its own check: exactly ONE System 1
  tab is open and it shows My Inbox.
- **Per case** (CSV order): search the ID → read the result's Sub Worktype (no guidance file for it = the case is
  **held before View is clicked**, nothing downloaded) → open it → verify the exact ID **and** the CSV's account on
  Overview (a different account = the case is held, never downloaded) → download into
  `CASES_ROOT\.work\<id> - <ACCOUNT>` → back to My Inbox.
- **The search** keeps its fixed 1.5 s wait; when it does not show exactly one View, the reason says so in plain words
  (with the count) and the case is retried once, as before.
- **Each document**, first the **direct route** (no click): its viewer address, paired with its row by the document
  ID it names (by page order only when no address names an ID and the counts match; otherwise no address), fetched with
  the System 1 sign-in. When that address answers with a viewer page instead of the file, the file that page shows
  (iframe / embed / download link) is fetched. A file is saved under the row's name, with the server's extension when
  the row has none (Word, Excel, images are kept as they are).
- **Only if that fails, the View fallback, strict:** View is clicked ONCE. A file that downloads straight away is
  saved. A document window is read in this order: the file at its own address (Edge's PDF viewer shows the PDF's
  address), the file its page shows, a download it starts by itself, then ONE click on its own toolbar Download button
  if it has one. **Nothing inside the document is ever clicked** (no links). Every window this step opened is closed
  again, and if the file was not obtained a screenshot + page copy of the window is kept.
- **For the report**: every request (status, type, size, what the body looked like), the route that worked or why
  not, go to `RUNTIME_ROOT\pipeline\runs\<run>\diagnostics\part1\<id>.json`; a failed or held case also gets a
  screenshot, the visible text and the page file of the System 1 tab in `RUNTIME_ROOT\logs\captures\<run>\`. The
  coordinator turns them into `RUNTIME_ROOT\logs\pipeline-report-<run>.md`. When it is done it writes `RUNTIME_ROOT\pipeline\downloads\<id>.json`. That file lists the
  documents in Received Documents order, the Audit Trail and the Sub Worktype; System 1's Sub Worktype wins over the
  CSV's, and a difference is reported.
- **Retries** a failed case once (the folder is started clean). A hold or a stop is never retried.
- **Never touches** your System 2 tab: it only works in tabs on System 1's host.
- **Stops** when the shared stop signal is set: before the next case, or between the documents and the Audit Trail.
  An unfinished case stays in `.work`.

Kept unchanged from the original script: every UI step, selector and wait, the Audit Trail route, plus
`studio_visual_cursor.py` (the moving cursor; `STUDIO_VISUAL_CURSOR=0` turns it off). Rewritten (VDI run 2026-11-02:
the old popup fallback clicked every link that mentioned "download" or "save" - including links inside the document -
and one-by-position pairing could name a file after the wrong row): the document routes described above.
Changed for the pipeline:
- every System 1 page comes from the config, pasted once in setup (`system1_url`, `system1_viewer_marker`,
  `system1_brand_text`, `system1_brand_html`). None is written in this code (the repository is public). The inbox
  and case pages are recognised as "under the inbox page's address";
- Edge is joined, never launched here;
- Playwright starts the fast way, with Part 3's driver start: the drive letter, the driver-only settings and nothing
  written to the system drive;
- Pillow is only imported by the unused screenshot helpers.
