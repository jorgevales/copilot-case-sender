# 3_azure_reviewer - Part 3 without Copilot

Azure Document Intelligence (Layout) reads every page; an Azure language model fills each recognised form and then
reviews the case with the same daily instructions, Sub Worktype rules and result contract as the Copilot sender.
Chosen with the setting `reviewer: azure` (Run.cmd setup). `3_copilot_sender` stays as the fallback.

| Command | What it does |
|---|---|
| `azure_case_reviewer.py check` | settings + a live call to Document Intelligence and to each model deployment |
| `azure_case_reviewer.py review --csv --cases-root --guidance --out [--case-model] [--limit]` | standalone review (used by `Run.cmd azure-test`): one `<instruction id>.json` per case |
| `azure_case_reviewer.py run --pipeline <run dir>` | started by the coordinator instead of the sender |

How one case is reviewed (`reviewer.py`):
1. Files in order: snapshot, Audit Trail, documents (System 1's order). `.msg .doc .xls .ppt .rtf .gif` = case held.
2. PDF / images / Office -> Document Intelligence; on the free tier in 2-page pieces. Every result is cached in
   `RUNTIME_ROOT\cache\azure-di` by the file's fingerprint: a file seen before costs no pages. Text files are read here.
3. Each document matching a form of `form_maps.json` is filled by the form model, which cites the numbered lines
   it read. The code (not the model) sets HANDWRITTEN / CONFIDENCE / LOW_CONFIDENCE; below `azure_low_confidence`
   (0.80), a citation that does not exist, or a value not in the cited text = flagged.
4. One case-review call: the base message (case timing), the case source line, the three instruction files, the
   document list, EXTRACTED_FIELDS and every document's text. Answer = the contract's keys with `REVIEW_RESULT` and a
   `FIELD_IDS` list per comparison; checked by the sender's own `validate_result` (fix-up requests as for Copilot).
5. Program rules: a comparison resting on a flagged field becomes MATCH null ("Could not compare"); a case cannot be
   Correct while a key field is unreadable or a form is short of pages.

Errors: temporary (throttling, 5xx, network) = 3 retries then held; credentials / quota / wrong endpoint or deployment
= safe stop of the whole run; content refusal = held; program error = held with its trace in the run report.
Files: `RUNTIME_ROOT\results-azure\<id>.json`, `RUNTIME_ROOT\azure_reviewed_instructions.txt` (valid results only),
`logs\azure-responses-<time>.md` and `logs\azure-run-report-<time>.md`. Keys, tokens and endpoints are never written.
