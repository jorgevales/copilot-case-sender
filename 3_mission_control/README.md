# System 3 — Mission Control (first version)

`mission_control.py` sends each complete case to the company's **Mission Control** workflow, which runs its own GPT
agents and sub-agents to produce the review. It is the default System 3 (setting `reviewer = mission_control`).

**Status: first version.** The exact structure Mission Control's trigger expects is not known yet. The package below is
a starting point, built in one place (`case_package`) so it can be changed easily once the structure is known.

## When it runs

- Started by the coordinator (`orchestrator/run_pipeline.py`) in this console - never by hand.
- **Nothing is sent until System 1 and System 2 have finished every case of the run** (since 2026-11-26). The console
  says "nothing is sent until every case of the run is settled" while it waits. Then:
  1. the run's **document map** is sent first, as its own send (see below);
  2. the cases follow, one case per send, in the BAM Report's order. The order is decided in `send_order` only
     (the one place to change for sending related cases together). Up to `mission_control_parallel` (default 2)
     cases are sent at the same time.
- If the map cannot be made, or Mission Control does not accept it (refused, unreachable after 3 tries, or no
  answer), **no case is sent that run**. Each case is reported as "NOT sent to Mission Control - failed: the
  document map did not reach Mission Control first".
- It also runs as the console of runs **without** System 3 (System 1 only / System 2 only / both): it only shows the
  progress and the sign-in step, and sends nothing.
- No Sub Worktype instructions file is needed or sent: Mission Control's agents already have them.

## What is sent

First, the **document map** (one send): `POST <mission_control_url>` as `multipart/form-data` with one part `map`
(`document_map.json`), built by `shared/document_map.py`. It is written for an AI agent to read on its own:
`how_to_read`, `run_details`, `instructions` (every instruction of the run with all its details, documents, related
instructions and group), `documents` (by Document ID, with every instruction that has it), `groups`,
`documents_without_id`. The copy as sent is kept in `RUNTIME_ROOT\mission-control\<run>\map.json`.

Then **one case per send** (`package_version` 2):

| Part | Content |
|---|---|
| `case` | JSON: `request_id`, `client_account_number`, `client_name`, `client_number`, `fund`, `fund_type`, `process_status`, `sub_worktype` (System 1's), `sub_worktype_in_report`, `received_date`, `end_date`, `source`, `email_subject`, `warnings`, `documents_table` (the case's Received Documents records), `related` (`group`, `related_instructions` with the shared Document IDs, `document_ids`), and `files` (name, kind, size, sha256 of each) |
| `files` | every file of the case folder, in the order: account snapshot, Audit Trail, documents table (`_Documents Table.json`, kind `documents table`), documents |

With `mission_control_auth = key`, the key goes in the header named by `mission_control_key_header` (default
`api-key`). The key is never printed or written to a report.

## Not connected yet

Leave the endpoint address empty in setup. Nothing then leaves the computer: the map is saved as
`RUNTIME_ROOT\mission-control\<run>\map.json` (outcome `prepared`) and each case's `case.json` is saved in
`RUNTIME_ROOT\mission-control\<run>\<id>\` and reported as **prepared - Mission Control not connected yet**. The same
copy is kept for every real send (a record of exactly what was sent).

## Outcomes

| Outcome | Meaning |
|---|---|
| sent to Mission Control | Mission Control answered OK (its reference is recorded when it gives one) |
| UNCERTAIN send | no answer in time after sending: the case may have arrived. Check Mission Control before sending it again (it is never re-sent by itself) |
| NOT sent - failed | refused (4xx) or unreachable after 3 tries; the run continues with the next case |
| prepared | not connected yet: saved only |

A refused key (HTTP 401/403) stops all further sends of that run, because every following case would fail the same way.

Every send is written to the already-done log (`shared/done_log.py`). Choosing a case again in a later run gives a
warning and asks once (Enter = send them all again).

`Run.cmd check` (menu option 7) also checks whether the endpoint answers at all. Nothing is sent.
