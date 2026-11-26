"""System 1's Received Documents table and the run's document map (stdlib only, no browser, no network).

Two halves:
  table_records(raw)          Part 1: the table as read from the page (headers + cell texts, see DOCUMENTS_TABLE_JS in
                              1_system1_documents/system1_documents.py) -> one record per document row.  Columns are
                              found by their header TEXT (never by position), Metadata is split on its first two commas
                              only, Date Created (dd/mm/yyyy hh:mm AM/PM, UK) is also given as ISO text.
  build_map(paths, plan, control) / write_map(...)
                              Coordinator, once every case of the run is settled (just before the feed closes): which
                              documents (by Document ID - always unique) belong to which instructions, and which
                              instructions are related because they share documents.  Written to the run folder and to
                              CASES_ROOT\\_Document maps\\<run id>.json; Mission Control receives it FIRST, before any case.

A record (one per row of the table, also in RUNTIME_ROOT\\pipeline\\downloads\\<id>.json and the case folder's
'_Documents Table.json'):
  row, type, document_name, metadata_raw, account_ref, client_number, client_name, document_id, date_created (ISO or
  ''), date_created_raw, other_columns, saved_file, obtained ('downloaded' / 'copied from instruction <id>' /
  'not downloaded'), method, note
"""
from __future__ import annotations

import datetime

from shared import pipeline as P

MAP_VERSION = 1
MAP_FILE = "document_map.json"                      # in the run folder
MAP_STATUS = "document-map.json"                    # in the run's status folder: {ok, path, copy, error, written}
TABLE_VERSION = 1

# header text (compared loosely) -> record key.  Anything else is kept under other_columns; View is an icon only.
COLUMNS = {"type": "type", "document name": "document_name", "metadata": "metadata_raw", "document id": "document_id",
           "date created": "date_created_raw"}
IGNORED_COLUMNS = {"view"}
DATE_FORMATS = ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d/%m/%Y")

HOW_TO_READ_TABLE = [
    "One record per row of this instruction's System 1 'Received Documents' table, in the table's order.",
    "document_id is System 1's unique key for a document; the same document_id in another instruction is the SAME "
    "document. document_name is not unique.",
    "saved_file is the file's name in this case folder (it starts with the Document ID). obtained says whether it was "
    "downloaded from System 1 or copied from the folder of an earlier instruction of the same run that has the same "
    "document (identical file).",
    "account_ref, client_number and client_name come from the row's Metadata (account,client number,client name). "
    "date_created is the row's Date Created as ISO text (UK date, 12-hour clock in date_created_raw).",
    "origin compares date_created with the instruction's Created Date (instruction_created_date, from the BAM "
    "Report): 'probable customer document' = added within 2 minutes of the instruction's creation (most likely "
    "came with the customer's request); 'probable processor upload' = added later; 'origin unknown' = a date is "
    "missing. It is a likelihood only: the Audit Trail confirms who added each document.",
]

HOW_TO_READ_MAP = [
    "This map covers every instruction of one daily-review run. Read it before the instructions themselves.",
    "documents: one entry per System 1 Document ID (always unique). Same ID = same document, even when it appears in "
    "several instructions. 'in_instructions' lists every instruction that has it and the file name it has there.",
    "instructions: one entry per instruction ID (RequestId), in the BAM Report's order. Each entry stands on its own: "
    "client account number, client number and name, Sub Worktype (System 1's when it differs from the report's), "
    "fund, dates, source, email subject, status in this run, its documents (with ID, name, type, date, file name and "
    "the other instructions that have the same document), the instructions it shares documents with and its group. "
    "bam_report.columns = every column of its BAM Report row; system1 = what System 1's Overview showed.",
    "related_instructions: two instructions are related when they share at least one Document ID; "
    "shared_document_ids says which. groups: instructions connected through shared documents (A shares with B, B with "
    "C -> A, B and C are one group; each group lists its accounts, client numbers and Sub Worktypes). An instruction "
    "with no shared document has group null.",
    "run_details: the BAM Report file, the run choice and which systems ran.",
    "documents_without_id: rows whose Document ID was blank in System 1. They cannot be matched across instructions.",
    "origin (per instruction and document): 'probable customer document' = added within 2 minutes of that "
    "instruction's Created Date (most likely came with the customer's request); 'probable processor upload' = added "
    "later; 'origin unknown' = a date is missing. The same document can differ between instructions (a customer "
    "document in one, added later to another). A likelihood only: each case's Audit Trail confirms who added it.",
    "table_recorded false = that instruction was downloaded before the documents table was captured: its documents are "
    "not in this map.",
    "status: 'ready' = complete and handed to System 3 in this run; 'held' / 'waiting' = not sent in this run (reason "
    "given). Files are in CASES_ROOT\\<case_folder> (ready) or CASES_ROOT\\.work\\<case_folder> (not handed over).",
]


# ---------------------------------------------------------------------------------------------- the table

def clean(v) -> str:
    return " ".join(str(v or "").split())


def iso_date(text: str) -> str:
    """'01/01/2026 01:37 PM' -> '2026-01-01T13:37:00' ('' when it is not a date of that form)."""
    t = clean(text).upper()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).isoformat()
        except ValueError:
            continue
    return ""


# A document's likely origin, from its Date Created and the instruction's Created Date (BAM Report).  Documents can
# only be added after the instruction was created: within CUSTOMER_MINUTES of it = it most likely came with the
# customer's request (uploaded automatically when the case was made); later = most likely added by a processor.
# Only a likelihood (user, 2026-11-26): the case's Audit Trail is what confirms who added it.
CUSTOMER_MINUTES = 2
ORIGIN_CUSTOMER = "probable customer document"
ORIGIN_PROCESSOR = "probable processor upload"
ORIGIN_UNKNOWN = "origin unknown"


def when(text):
    """'dd/mm/yyyy hh:mm AM/PM' or ISO text -> datetime (None when it is neither)."""
    t = clean(text)
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(t)
    except ValueError:
        pass
    iso = iso_date(t)
    return datetime.datetime.fromisoformat(iso) if iso else None


def origin_of(document_date, instruction_created) -> dict:
    """{origin, minutes_after_instruction_created, origin_note} for one document of one instruction."""
    d, c = when(document_date), when(instruction_created)
    if not d or not c:
        return {"origin": ORIGIN_UNKNOWN, "minutes_after_instruction_created": None,
                "origin_note": ("no Created Date for the instruction" if not c else "no Date Created for the document")
                + " - check the Audit Trail"}
    minutes = round((d - c).total_seconds() / 60, 1)
    if minutes < 0:
        return {"origin": ORIGIN_UNKNOWN, "minutes_after_instruction_created": minutes,
                "origin_note": "dated before the instruction's Created Date (should not happen) - check the Audit Trail"}
    if minutes <= CUSTOMER_MINUTES:
        return {"origin": ORIGIN_CUSTOMER, "minutes_after_instruction_created": minutes,
                "origin_note": f"added within {CUSTOMER_MINUTES} min of the instruction's creation - most likely came "
                               "with the customer's request; the Audit Trail confirms it"}
    return {"origin": ORIGIN_PROCESSOR, "minutes_after_instruction_created": minutes,
            "origin_note": f"added {minutes:g} min after the instruction was created - most likely added by a "
                           "processor; the Audit Trail confirms it"}


def label_origins(records: list, instruction_created) -> list:
    """Every record gets its origin (in place); returns the records."""
    for r in records or []:
        r.update(origin_of(r.get("date_created") or r.get("date_created_raw"), instruction_created))
    return records


def split_metadata(raw: str) -> tuple:
    """'<ACCOUNT>,<CLIENT NO>,<CLIENT NAME, may have commas>' -> (account, client number, client name)."""
    parts = [p.strip() for p in str(raw or "").split(",", 2)]
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def empty_record(row: int) -> dict:
    return {"row": row, "type": "", "document_name": "", "metadata_raw": "", "account_ref": "", "client_number": "",
            "client_name": "", "document_id": "", "date_created": "", "date_created_raw": "", "other_columns": {},
            "saved_file": "", "obtained": "not downloaded", "method": "", "note": ""}


def table_records(raw) -> list:
    """The page's table (`{found, headers, rows: [{index, cells}]}`) -> records.  A row whose cells are all blank (a
    'no documents' placeholder) is left out; a row without Document ID is kept with document_id ''."""
    if not raw or not raw.get("found"):
        return []
    keys = []
    for h in raw.get("headers") or []:
        k = clean(h).casefold()
        keys.append(COLUMNS.get(k) or ("" if k in IGNORED_COLUMNS else clean(h) or ""))
    out = []
    for r in raw.get("rows") or []:
        cells = [clean(c) for c in r.get("cells") or []]
        if not any(cells):
            continue
        rec = empty_record(len(out) + 1)
        rec["table_row_index"] = r.get("index", len(out))
        for i, value in enumerate(cells):
            key = keys[i] if i < len(keys) else f"column {i + 1}"
            if not key:
                continue
            if key in COLUMNS.values():
                rec[key] = value
            else:
                rec["other_columns"][key] = value
        rec["account_ref"], rec["client_number"], rec["client_name"] = split_metadata(rec["metadata_raw"])
        rec["date_created"] = iso_date(rec["date_created_raw"])
        out.append(rec)
    return out


def table_file(iid: str, account: str, records: list, table_found: bool, audit: str = "", client_number: str = "",
               client_name: str = "", sub_worktype: str = "", created: str = "") -> dict:
    """What '_Documents Table.json' in the case folder holds (the client details as System 1's Overview shows them;
    `created` = the instruction's Created Date from the BAM Report)."""
    return {"table_version": TABLE_VERSION, "how_to_read": HOW_TO_READ_TABLE, "instruction_id": iid,
            "client_account_number": account, "client_number": client_number, "client_name": client_name,
            "sub_worktype": sub_worktype, "instruction_created_date": created, "table_found": table_found,
            "documents": [{k: v for k, v in r.items() if k != "table_row_index"} for r in records],
            "audit_trail": audit, "written": P.now()}


# ---------------------------------------------------------------------------------------------- the run's map

def _groups(order: list, links: dict) -> dict:
    """iid -> group name ('G1', ...) for every instruction linked to another (connected through shared documents)."""
    parent = {i: i for i in order}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for a, others in links.items():
        for b in others:
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[rb] = ra
    members = {}
    for i in order:
        members.setdefault(root(i), []).append(i)
    out, n = {}, 0
    for i in order:                                         # numbered in report order of each group's first member
        group = members[root(i)]
        if len(group) > 1 and group[0] == i:
            n += 1
            for m in group:
                out[m] = f"G{n}"
    return out


def build_map(paths: P.Paths, plan: dict, control: P.Control) -> dict:
    """Every instruction this run works on (needs download or send), from each case's saved record."""
    cases = [c for c in plan.get("cases") or [] if c.get("needs") in ("download", "send")]
    order = [c["iid"] for c in cases]
    held = control.held()
    waiting = P.read_json(paths.sub("status") / "waiting.json", {}) or {}
    documents, without_id, instructions = {}, [], {}
    for c in cases:
        iid = c["iid"]
        m = P.read_json(paths.downloads / f"{iid}.json") or {}
        handed = P.read_json(paths.cases / f"{iid}.json") or {}
        folder = m.get("folder") or handed.get("folder") or ""
        if iid in held:
            status, why = "held", held[iid].get("reason", "")
        elif iid in waiting:
            status, why = "waiting", waiting[iid]
        elif handed and folder and (paths.cases_root / folder).is_dir():
            status, why = "ready", ""
        else:
            status, why = "not complete", "not handed over in this run"
        where = ("CASES_ROOT" if folder and (paths.cases_root / folder).is_dir() else
                 "CASES_ROOT\\.work" if folder and (paths.work / folder).is_dir() else "")
        records = m.get("documents_table")
        created = c.get("created_date", "")
        origins = [origin_of(r.get("date_created") or r.get("date_created_raw"), created) for r in records or []]
        ids = []
        for r, o in zip(records or [], origins):
            ref = {"instruction_id": iid, "file_name": r.get("saved_file", ""), "obtained": r.get("obtained", ""),
                   "origin": o["origin"], "minutes_after_instruction_created": o["minutes_after_instruction_created"]}
            did = r.get("document_id", "")
            if not did:
                without_id.append({**ref, "document_name": r.get("document_name", ""), "type": r.get("type", ""),
                                   "date_created": r.get("date_created", "")})
                continue
            if did not in ids:
                ids.append(did)
            d = documents.setdefault(did, {
                "document_id": did, "type": r.get("type", ""), "document_name": r.get("document_name", ""),
                "account_ref": r.get("account_ref", ""), "client_number": r.get("client_number", ""),
                "client_name": r.get("client_name", ""), "metadata_raw": r.get("metadata_raw", ""),
                "date_created": r.get("date_created", ""), "date_created_raw": r.get("date_created_raw", ""),
                "in_instructions": []})
            if not any(x["instruction_id"] == iid for x in d["in_instructions"]):
                d["in_instructions"].append(ref)
        s1 = m.get("system1_identity") or {}
        instructions[iid] = {
            "instruction_id": iid,
            "client_account_number": c.get("account", "") or P.norm_account(s1.get("accountNumber", "")),
            "client_number": c.get("client_number", "") or s1.get("customerNumber", ""),
            "client_name": c.get("client_name", "") or s1.get("customerName", ""),
            "sub_worktype": m.get("sub_worktype") or handed.get("sub_worktype") or c.get("sub_worktype_csv", ""),
            "fund": c.get("fund", ""), "fund_type": c.get("fund_type", ""), "created_date": created,
            "received_date": c.get("received_date", ""), "end_date": c.get("end_date", ""),
            "source": c.get("case_source", ""), "email_subject": c.get("email_subject", ""),
            "status": status, "status_reason": why,
            "bam_report": {"row_number": c.get("row_number", ""), "process_status": c.get("status", ""),
                           "sub_worktype": c.get("sub_worktype_csv", ""), "columns": c.get("row") or {}},
            "system1": {"client_number": s1.get("customerNumber", ""), "client_name": s1.get("customerName", ""),
                        "client_account_number": s1.get("accountNumber", ""),
                        "sub_worktype": m.get("sub_worktype_system1", ""), "warnings": m.get("warnings") or []},
            "case_folder": folder, "folder_location": where,
            "files_in_case_folder": list(handed.get("order") or []),
            "table_recorded": records is not None, "table_found": m.get("table_found", records is not None),
            "documents_table_file": m.get("table_file", ""), "audit_trail": m.get("audit", ""),
            "documents": [{"document_id": r.get("document_id", ""), "document_name": r.get("document_name", ""),
                           "type": r.get("type", ""), "date_created": r.get("date_created", ""),
                           "file_name": r.get("saved_file", ""), "obtained": r.get("obtained", ""),
                           "account_ref": r.get("account_ref", ""), "client_number": r.get("client_number", ""),
                           "client_name": r.get("client_name", ""), **o, "also_in_instructions": []}
                          for r, o in zip(records or [], origins)],
            "documents_by_origin": {k: sum(1 for o in origins if o["origin"] == k)
                                    for k in (ORIGIN_CUSTOMER, ORIGIN_PROCESSOR, ORIGIN_UNKNOWN)},
            "document_ids": ids, "documents_without_id": sum(1 for x in without_id if x["instruction_id"] == iid),
            "related_instructions": [], "group": None}
    links = {i: {} for i in order}                          # iid -> {other iid: [shared document ids]}
    for did, d in documents.items():
        having = [x["instruction_id"] for x in d["in_instructions"]]
        for a in having:
            for b in having:
                if a != b:
                    links[a].setdefault(b, []).append(did)
    group_of = _groups(order, {a: list(b) for a, b in links.items()})
    for iid in order:
        inst = instructions[iid]
        inst["related_instructions"] = [
            {"instruction_id": b, "client_account_number": instructions[b]["client_account_number"],
             "client_number": instructions[b]["client_number"], "sub_worktype": instructions[b]["sub_worktype"],
             "shared_document_ids": links[iid][b]} for b in order if b in links[iid]]
        inst["group"] = group_of.get(iid)
        for d in inst["documents"]:
            if d["document_id"] in documents:
                d["also_in_instructions"] = [x["instruction_id"] for x in documents[d["document_id"]]["in_instructions"]
                                             if x["instruction_id"] != iid]
    groups = []
    for name in dict.fromkeys(group_of[i] for i in order if i in group_of):
        members = [i for i in order if group_of.get(i) == name]
        shared = [did for did, d in documents.items()
                  if sum(1 for x in d["in_instructions"] if x["instruction_id"] in members) > 1]
        groups.append({"group": name, "instructions": members, "shared_document_ids": shared,
                       "client_account_numbers": sorted({instructions[i]["client_account_number"] for i in members}),
                       "client_numbers": sorted({instructions[i]["client_number"] for i in members} - {""}),
                       "sub_worktypes": sorted({instructions[i]["sub_worktype"] for i in members} - {""})})
    shared_docs = sum(1 for d in documents.values() if len(d["in_instructions"]) > 1)
    return {
        "map_version": MAP_VERSION, "run": paths.run.name, "created": P.now(), "how_to_read": HOW_TO_READ_MAP,
        "run_details": {"run": paths.run.name, "day": plan.get("day", ""), "bam_report": plan.get("bam", ""),
                        "run_choice": plan.get("scope", ""), "mode": plan.get("mode", ""),
                        "systems": plan.get("systems", ""), "system3": plan.get("reviewer", "")},
        "summary": {"instructions": len(order), "documents": len(documents), "documents_in_several_instructions":
                    shared_docs, "documents_without_id": len(without_id), "groups": len(groups),
                    "instructions_without_table": sum(1 for i in instructions.values() if not i["table_recorded"])},
        "groups": groups, "instructions": instructions, "documents": documents,
        "documents_without_id": without_id,
    }


def write_map(paths: P.Paths, plan: dict, control: P.Control) -> dict:
    """Build + write the documents map (run folder, then the CASES_ROOT copy), then the relationship map built from it
    (shared/relationship_map.py, window = plan['relationship_hours']), and their status file.  Never raises: a map
    that could not be built is recorded (ok / relationships_ok False + error) so Mission Control sends no case."""
    from shared import relationship_map as RM
    status = {"ok": False, "path": "", "copy": "", "error": "", "written": "",
              "relationships_ok": False, "relationships_path": "", "relationships_copy": "", "relationships_error": ""}
    try:
        doc_map = build_map(paths, plan, control)
        target = paths.run / MAP_FILE
        P.write_json(target, doc_map)
        status.update(ok=True, path=str(target), summary=doc_map["summary"])
        try:
            copy = paths.cases_root / P.DOCUMENT_MAPS_DIR / f"{paths.run.name}.json"
            P.write_json(copy, doc_map)
            status["copy"] = str(copy)
        except OSError as exc:                              # the run's own copy is what Mission Control reads
            status["copy_error"] = f"{type(exc).__name__}"
        try:
            hours = float(plan.get("relationship_hours") or RM.DEFAULT_HOURS)
            path, copy, summary = RM.write(paths, doc_map, hours)
            status.update(relationships_ok=True, relationships_path=path, relationships_copy=copy,
                          relationships_summary=summary)
        except Exception as exc:                            # noqa: BLE001 - recorded: Mission Control sends no case
            status["relationships_error"] = f"{type(exc).__name__}: {exc}"[:300]
    except Exception as exc:                                # noqa: BLE001 - recorded, never crashes the hand-over
        status["error"] = f"{type(exc).__name__}: {exc}"[:300]
    status["written"] = P.now()
    try:
        P.write_json(paths.sub("status") / MAP_STATUS, status)
    except OSError:
        pass
    return status


def read_status(paths: P.Paths):
    """The map's status file (None until the coordinator has written it)."""
    return P.read_json(paths.sub("status") / MAP_STATUS)


def read_map(paths: P.Paths):
    return P.read_json(paths.run / MAP_FILE)


def related_of(doc_map, iid: str) -> dict:
    """What a case's send carries from the map: its group and the instructions it shares documents with."""
    entry = ((doc_map or {}).get("instructions") or {}).get(iid) or {}
    return {"group": entry.get("group"), "related_instructions": entry.get("related_instructions") or [],
            "document_ids": entry.get("document_ids") or []}


def name_with_id(document_id: str, name: str) -> str:
    """The saved file's name: '<Document ID>_<Document Name>' (only the name when the row has no Document ID)."""
    name = name or "document"
    return f"{document_id}_{name}" if document_id and not name.startswith(document_id) else name
