"""Tests for System 1's Received Documents table, the per-case '_Documents Table.json', the duplicates copied within a
run, the run's document map and Mission Control's map-first send.  Synthetic data only; files only under
SENDER_TEST_ROOT.  The table script itself is RUN (Node + a tiny DOM built from the documented page structure).

    set SENDER_TEST_ROOT=<a folder on approved storage>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests
"""
import asyncio
import datetime
import html.parser
import json
import shutil
import subprocess
import types

import pytest

import test_pipeline as TP                           # its helpers (base fixture, fake System 1 page, Part 1 fakes)
from test_pipeline import base                       # noqa: F401  (pytest fixture)
import test_daily_update as DU                       # the Mission Control helpers

P, H, P1, T, MC = TP.P, TP.H, TP.P1, TP.T, DU.MC
hexid = TP.hexid
from shared import document_map as DM                 # noqa: E402
from shared import relationship_map as RM             # noqa: E402

GUID_A = "0a1b2c3d-0000-4000-8000-00000000000a"
GUID_B = "0a1b2c3d-0000-4000-8000-00000000000b"


# ---------------------------------------------------------------------------------------------- the page script

def cell(tag, label, value_html):
    inner = f'<span class="awLabel xTextx">{label}</span>' if tag == "td" else ""
    return (f'<{tag} class="awInfoTableCell{" awInfoTableCell--header" if tag == "th" else ""}">'
            f'<div class="awInfoTableCell_innerHeader">{inner}</div>'
            f'<div class="awInfoTableCell_content">{value_html}</div></{tag}>')


def info_table(headers, rows):
    """An Appway info table exactly as Studio_Documents_Table_Structure describes it."""
    head = "".join(cell("th", "", f'<span class="awLabel xTextx">{h}</span>') for h in headers)
    body = ""
    for r in rows:
        tds = "".join(cell("td", h, v if v.startswith("<") else f'<span class="awLabel xTextx">{v}</span>')
                      for h, v in zip(headers, r))
        body += f'<tr class="awInfoTableRow xInfoTableRowx">{tds}</tr>'
    return ('<div class="awInfoTable xInfoTablex"><table class="awInfoTable_table">'
            '<thead class="awInfoTableGroup--header"><tr class="awInfoTableRow awInfoTableRow--header xInfoTableRow-headerx">'
            f'{head}</tr></thead><tbody class="awInfoTableGroup--body">{body}</tbody></table></div>')


VIEW = ('<span class="awIcon xIconx"><svg class="awIcon_svg" focusable="false">'
        '<use xlink:href="#ext.defaulticons.viewdetailslinear-32"></use></svg></span>')
HEADERS = ["Type", "Document Name", "Metadata", "Document ID", "Date Created", "View"]


class Tree(html.parser.HTMLParser):
    """HTML -> nested {tag, attrs, children} (text as strings); entities like &amp; decoded as a browser does."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "html", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


HARNESS = r"""
const fs = require('fs');
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function build(n) {
  if (typeof n === 'string') return {nodeType: 3, text: n};
  const el = {nodeType: 1, tagName: n.tag.toUpperCase(), childNodes: n.children.map(build),
              classList: {contains: c => (n.attrs['class'] || '').split(/\s+/).includes(c)}};
  Object.defineProperty(el, 'children', {get() { return el.childNodes.filter(c => c.nodeType === 1); }});
  Object.defineProperty(el, 'textContent', {get() { return el.childNodes.map(c => c.nodeType === 3 ? c.text : c.textContent).join(''); }});
  return el;
}
const all = (e, out = []) => { for (const k of e.children) { out.push(k); all(k, out); } return out; };
const root = build(job.tree);
global.document = {documentElement: root, body: all(root).find(e => e.tagName === 'BODY')};
if (job.native)   // the browser's own index (querySelectorAll of one tag name), as the real page has it
  global.document.querySelectorAll = sel => all(root).filter(e => e.tagName === sel.toUpperCase());
const fn = eval('(' + job.script + ')');
process.stdout.write(JSON.stringify(fn()));
"""


def run_page_script(base, page_html):
    """The page script, run in Node twice: with the browser's own table index and with the plain tree walk.  Both
    must give the same answer, which is returned."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed - the page script cannot be run here")
    t = Tree()
    t.feed(page_html)
    (base / "harness.js").write_text(HARNESS, "utf-8")
    answers = []
    for native in (True, False):
        (base / "job.json").write_text(json.dumps({"tree": t.root, "script": P1.DOCUMENTS_TABLE_JS, "native": native}),
                                       "utf-8")
        out = subprocess.run([node, str(base / "harness.js"), str(base / "job.json")], capture_output=True, text=True,
                             timeout=60)
        assert out.returncode == 0, out.stderr
        answers.append(json.loads(out.stdout))
    assert answers[0] == answers[1]
    return answers[0]


def test_the_page_script_reads_only_the_table_with_a_document_id_column(base):
    decoy = info_table(["Name", "Value"], [["Owner", "Someone"]])
    docs = info_table(HEADERS, [
        ["Customer Identification", "MrJohnSmith-SS.pdf", "XX0000000,00000,EXAMPLE CLIENT &amp; PARTNERS - TRUSTS",
         GUID_A, "01/01/2026 10:28 PM", VIEW],
        ["Other", "Email.pdf", "XX0000000,00000,EXAMPLE CLIENT &amp; PARTNERS - TRUSTS", GUID_B, "01/01/2026 01:37 PM",
         VIEW]])
    page = (f'<body>{decoy}<div class="xDesignSystemx_xHubMainContent_Wrapperx"><h3>Received Documents</h3>{docs}</div>'
            '</body>')
    raw = run_page_script(base, page)
    assert raw["found"] and raw["headers"] == HEADERS
    assert [r["cells"][3] for r in raw["rows"]] == [GUID_A, GUID_B]
    assert raw["rows"][0]["cells"][0] == "Customer Identification"          # the repeated label "Type" is not read
    assert raw["rows"][0]["cells"][2] == "XX0000000,00000,EXAMPLE CLIENT & PARTNERS - TRUSTS"
    assert raw["rows"][0]["cells"][5] == "" and [r["index"] for r in raw["rows"]] == [0, 1]


def test_the_page_script_finds_the_table_without_its_wrapper_in_any_column_order(base):
    headers = ["Document ID", "Date Created", "Type", "Document Name", "Notes", "Metadata", "View"]
    page = "<body>" + info_table(headers, [
        [GUID_A, "02/03/2026 09:05 AM", "Other", "COA.pdf", "first", "AB1234567,123,A, B AND C", VIEW],
        ["", "02/03/2026 09:06 AM", "Other", "Email.pdf", "", "AB1234567,123,A, B AND C", VIEW]]) + "</body>"
    raw = run_page_script(base, page)
    recs = DM.table_records(raw)
    assert [r["document_id"] for r in recs] == [GUID_A, ""] and recs[0]["document_name"] == "COA.pdf"
    assert recs[0]["client_name"] == "A, B AND C" and recs[0]["client_number"] == "123"
    assert recs[0]["date_created"] == "2026-03-02T09:05:00" and recs[0]["other_columns"] == {"Notes": "first"}
    none = run_page_script(base, "<body>" + info_table(["Name", "Value"], [["a", "b"]]) + "</body>")
    assert none["found"] is False and none["heading"] is False
    loading = run_page_script(base, "<body><h3>Received Documents</h3><p>loading</p></body>")
    assert loading["found"] is False and loading["heading"] is True          # the table is still to come: wait for it


def test_the_row_and_view_scripts_that_now_share_the_table_finder_still_parse(base):
    """document_rows / mark_document_view use the real browser's selectors, so they are only parsed here."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    scripts = []

    class Grab:
        async def evaluate(self, script, arg=None):
            scripts.append(script)
            return [] if "marker" in script else True
    T.run(P1.document_rows(Grab()))
    T.run(P1.mark_document_view(Grab(), {"documentId": "x", "index": 0}))
    assert len(scripts) == 2 and all("s4DocsTable(document)" in s for s in scripts)
    VC = TP.VC
    scripts += [VC.INSTALL, VC.TAP]                      # the pointer's in-page scripts (tap = pretend click) too
    for n, s in enumerate(scripts):
        (base / f"s{n}.js").write_text("const f = (" + s + ");\n", "utf-8")
        out = subprocess.run([node, "--check", str(base / f"s{n}.js")], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------------------------- the records

def test_records_split_the_metadata_on_two_commas_and_read_the_uk_date():
    raw = {"found": True, "headers": HEADERS, "rows": [
        {"index": 0, "cells": ["Other", "x.pdf", "XX0000000,00000,SMITH, JONES & CO", GUID_A, "01/01/2026 12:05 AM", ""]},
        {"index": 1, "cells": ["", "", "", "", "", ""]},                                  # a placeholder row
        {"index": 2, "cells": ["Other", "y.pdf", "", "", "not a date", ""]}]}
    recs = DM.table_records(raw)
    assert len(recs) == 2 and recs[0]["account_ref"] == "XX0000000" and recs[0]["client_name"] == "SMITH, JONES & CO"
    assert recs[0]["date_created"] == "2026-01-01T00:05:00" and recs[0]["table_row_index"] == 0
    assert recs[1]["document_id"] == "" and recs[1]["date_created"] == "" and recs[1]["row"] == 2
    assert DM.table_records({"found": False}) == []
    assert DM.name_with_id(GUID_A, "Email.pdf") == f"{GUID_A}_Email.pdf" and DM.name_with_id("", "Email.pdf") == "Email.pdf"


@pytest.mark.parametrize("document, created, origin, minutes", [
    ("2026-11-20T09:00:00", "20/11/2026 09:00 AM", DM.ORIGIN_CUSTOMER, 0),           # uploaded with the case
    ("20/11/2026 09:02 AM", "20/11/2026 09:00 AM", DM.ORIGIN_CUSTOMER, 2),           # the 2-minute edge
    ("2026-11-20T09:03:00", "20/11/2026 09:00 AM", DM.ORIGIN_PROCESSOR, 3),
    ("2026-11-21T14:00:00", "20/11/2026 09:00 AM", DM.ORIGIN_PROCESSOR, 1740),
    ("2026-11-20T08:59:00", "20/11/2026 09:00 AM", DM.ORIGIN_UNKNOWN, -1),            # cannot happen: clock mismatch
    ("2026-11-20T09:00:00", "", DM.ORIGIN_UNKNOWN, None),                             # no Created Date in the report
    ("", "20/11/2026 09:00 AM", DM.ORIGIN_UNKNOWN, None),
])
def test_a_documents_origin_comes_from_the_instructions_created_date(document, created, origin, minutes):
    o = DM.origin_of(document, created)
    assert o["origin"] == origin and o["minutes_after_instruction_created"] == minutes
    assert "Audit Trail" in o["origin_note"]


def test_the_bam_report_gives_the_created_date_as_text(base):
    report = DU.BH.write_bam(base / "r.xlsx", [DU.BH.case(hexid(1), created=datetime.datetime(2026, 11, 20, 13, 5))])
    cases, _, _ = P.read_report(report)
    assert cases[0]["created_date"] == "20/11/2026 01:05 PM" and cases[0]["row"]["Created Date"] == "20/11/2026 01:05 PM"
    assert cases[0]["row"]["Received Date"] == "20/11/2026 09:15 AM"            # every date column as text in the row


# ---------------------------------------------------------------------------------------------- Part 1

def test_the_document_step_names_files_by_id_copies_duplicates_and_records_every_row(base, monkeypatch):
    paths, _ = TP.p1_setup(base)
    earlier = paths.work / f"{hexid(9)} - AB9"                       # an earlier instruction of this run
    earlier.mkdir(parents=True)
    (earlier / f"{GUID_B}_Email.pdf").write_bytes(b"%PDF-shared")
    monkeypatch.setattr(P1, "RUN_DOCS", {GUID_B: {"iid": hexid(9), "folder": earlier.name, "file": f"{GUID_B}_Email.pdf"}})
    folder = paths.work / f"{hexid(1)} - AB1"
    folder.mkdir()
    o = "https://system1.example.invalid"
    ctx = TP.Ctx({o + "/view/a": TP.Resp(b"%PDF-a"), o + "/view/c": TP.Resp(b"%PDF-c")})
    meta = "AB1,100,SYNTHETIC CLIENT"
    table = {"found": True, "headers": HEADERS, "rows": [
        {"index": 0, "cells": ["Other", "COA.pdf", meta, GUID_A, "01/01/2026 01:37 PM", ""]},
        {"index": 1, "cells": ["Other", "Email.pdf", meta, GUID_B, "01/01/2026 01:38 PM", ""]},
        {"index": 2, "cells": ["Other", "Note.pdf", meta, "", "01/01/2026 01:39 PM", ""]},
        {"index": 3, "cells": ["Other", "", meta, "", "01/01/2026 01:40 PM", ""]}]}      # nothing to click or name
    rows = [{"index": 0, "rowIndex": 0, "documentName": "COA.pdf", "documentId": GUID_A, "viewerUrl": o + "/view/a",
             "pairing": "id", "viewerUrlsOnPage": 3},
            {"index": 1, "rowIndex": 1, "documentName": "Email.pdf", "documentId": GUID_B, "viewerUrl": o + "/view/b",
             "pairing": "id", "viewerUrlsOnPage": 3},
            {"index": 2, "rowIndex": 2, "documentName": "Note.pdf", "documentId": "", "viewerUrl": o + "/view/c",
             "pairing": "position", "viewerUrlsOnPage": 3}]
    page = TP.S1Page(ctx, rows, lambda *a: pytest.fail("no View click is needed"), table)
    events = []
    out = T.run(P1.download_documents(ctx, page, folder, events))
    assert [r["saved_file"] for r in out[:3]] == [f"{GUID_A}_COA.pdf", f"{GUID_B}_Email.pdf", "Note.pdf"]
    assert (folder / f"{GUID_B}_Email.pdf").read_bytes() == b"%PDF-shared" and o + "/view/b" not in ctx.asked
    recs = [r["record"] for r in out]
    assert [r["obtained"] for r in recs] == ["downloaded", f"copied from instruction {hexid(9)}", "downloaded",
                                             "not downloaded"]
    assert recs[0]["client_name"] == "SYNTHETIC CLIENT" and recs[0]["date_created"] == "2026-01-01T13:37:00"
    assert "no Document ID" in recs[2]["note"] and out[3]["source_text"] == "Table only"
    table_events = [e for e in events if e["event"] == "documents-table-read"]
    assert table_events[0]["found"] and table_events[0]["withoutId"] == 2

    records, found = P1.case_table(out, events)
    assert found and [r["row"] for r in records] == [1, 2, 3, 4] and "table_row_index" not in records[0]


def test_a_complete_case_gets_its_documents_table_file_and_record(base, monkeypatch):
    paths, control = TP.p1_setup(base)
    TP.P1Fakes(P1, monkeypatch)

    async def documents(context, page, folder, events):
        (folder / f"{GUID_A}_COA.pdf").write_bytes(b"%PDF")
        events.append({"event": "documents-table-read", "found": True})
        rec = dict(DM.empty_record(1), document_id=GUID_A, document_name="COA.pdf", saved_file=f"{GUID_A}_COA.pdf",
                   obtained="downloaded", table_row_index=0, date_created="2026-11-20T09:01:00")
        return [{"source_text": "View", "saved_file": f"{GUID_A}_COA.pdf", "status": "direct", "record": rec}]
    monkeypatch.setattr(P1, "download_documents", documents)
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "x", "created_date": "20/11/2026 09:00 AM"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "SUCCESS" and result["documents"] == [f"{GUID_A}_COA.pdf"]
    saved = P.read_json(paths.work / f"{hexid(1)} - AB100000" / P.DOCUMENTS_TABLE_FILE)
    assert saved["documents"][0]["document_id"] == GUID_A and saved["audit_trail"] == "Audit Trail.pdf"
    assert saved["table_found"] and saved["how_to_read"] and result["table_file"] == P.DOCUMENTS_TABLE_FILE
    assert saved["instruction_created_date"] == "20/11/2026 09:00 AM"
    assert saved["documents"][0]["origin"] == DM.ORIGIN_CUSTOMER and result["documents_table"][0]["origin"] == DM.ORIGIN_CUSTOMER


def test_the_downloads_record_keeps_the_table_and_later_cases_can_copy_from_it(base, monkeypatch):
    paths, control = TP.p1_setup(base)
    P1.RUN_DOCS["stale"] = {"iid": "old run"}                    # never carried into a new run

    async def fake(context, page, case, i, n, control):
        rec = dict(DM.empty_record(1), document_id=GUID_A, saved_file=f"{GUID_A}_COA.pdf", obtained="downloaded")
        return page, {"status": "SUCCESS", "account_number": "AB1", "folder": f"{case['iid']} - AB1",
                      "sub_worktype": "x", "documents": [f"{GUID_A}_COA.pdf"], "audit": "Audit Trail.pdf",
                      "documents_table": [rec], "table_found": True, "table_file": P.DOCUMENTS_TABLE_FILE,
                      "elapsed_seconds": 1, "tries": []}
    monkeypatch.setattr(P1, "process_instruction", fake)
    T.run(P1.run_cases(None, "page", [{"iid": hexid(1), "sub_worktype_csv": "x"}], paths, control,
                       lambda *a, **k: None, {"done": 0, "held": 0}))
    m = P.read_json(paths.downloads / f"{hexid(1)}.json")
    assert m["documents_table"][0]["document_id"] == GUID_A and m["table_file"] == P.DOCUMENTS_TABLE_FILE
    assert P1.RUN_DOCS == {GUID_A: {"iid": hexid(1), "folder": f"{hexid(1)} - AB1", "file": f"{GUID_A}_COA.pdf"}}
    P1.RUN_DOCS.clear()


# ---------------------------------------------------------------------------------------------- the run's map

def rec(did, name, saved=None, date="2026-11-20T09:01:00"):
    return dict(DM.empty_record(1), document_id=did, document_name=name, saved_file=saved or f"{did}_{name}",
                obtained="downloaded", type="Other", date_created=date)


def test_the_map_links_instructions_through_shared_documents_and_groups_them(base):
    paths, control = TP.make_paths(base)
    ids = [hexid(n) for n in range(1, 7)]
    tables = {ids[0]: [rec("D1", "a.pdf")], ids[1]: [rec("D1", "a.pdf"), rec("D2", "b.pdf")],
              ids[2]: [rec("D2", "b.pdf"), rec("", "loose.pdf", "loose.pdf")], ids[3]: [rec("D4", "d.pdf")],
              ids[4]: None, ids[5]: [rec("D1", "a.pdf")]}
    for iid, table in tables.items():
        folder = paths.cases_root / f"{iid} - AB1"
        folder.mkdir(parents=True)
        m = {"iid": iid, "folder": folder.name, "sub_worktype": "x", "audit": "Audit Trail.pdf"}
        if table is not None:
            m.update(documents_table=table, table_found=True)
        P.write_json(paths.downloads / f"{iid}.json", m)
        P.write_json(paths.cases / f"{iid}.json", {"folder": folder.name})
    control.hold(ids[5], "System 2: no snapshot", "coordinator")
    plan = {"cases": [{"iid": i, "account": "AB1", "needs": "download", "client_number": "C1", "client_name": "SYNTH",
                       "sub_worktype_csv": "Change of Address", "fund": "F1", "status": "Ended", "row_number": 6 + n,
                       "row": {"RequestId": i, "Fund": "F1"},
                       "created_date": {0: "20/11/2026 09:00 AM", 1: "20/11/2026 08:00 AM"}.get(n, "")}
                      for n, i in enumerate(ids)] +
                     [{"iid": hexid(7), "needs": "skipped"}], "bam": "BAM Report.xlsx", "scope": "Every case"}
    P.write_json(paths.downloads / f"{ids[0]}.json", dict(P.read_json(paths.downloads / f"{ids[0]}.json"),
                 system1_identity={"customerNumber": "C1", "customerName": "SYNTH", "accountNumber": "AB1"},
                 sub_worktype="Bank Details", sub_worktype_system1="Bank Details"))
    m = DM.build_map(paths, plan, control)
    inst = m["instructions"]
    assert list(inst) == ids and m["groups"] == [{"group": "G1", "instructions": [ids[0], ids[1], ids[2], ids[5]],
                                                  "shared_document_ids": ["D1", "D2"], "client_account_numbers": ["AB1"],
                                                  "client_numbers": ["C1"], "sub_worktypes": ["Bank Details", "x"]}]
    assert [(r["instruction_id"], r["shared_document_ids"]) for r in inst[ids[1]]["related_instructions"]] == \
        [(ids[0], ["D1"]), (ids[2], ["D2"]), (ids[5], ["D1"])]
    assert inst[ids[1]]["related_instructions"][0]["sub_worktype"] == "Bank Details"
    first = inst[ids[0]]                                       # an entry an agent can read on its own
    assert (first["client_number"], first["client_name"], first["fund"], first["sub_worktype"]) == \
        ("C1", "SYNTH", "F1", "Bank Details")
    assert first["bam_report"]["sub_worktype"] == "Change of Address" and first["bam_report"]["columns"]["Fund"] == "F1"
    assert first["system1"]["client_number"] == "C1" and first["system1"]["sub_worktype"] == "Bank Details"
    assert first["documents"][0]["also_in_instructions"] == [ids[1], ids[5]] and first["documents"][0]["file_name"] == "D1_a.pdf"
    assert m["run_details"]["bam_report"] == "BAM Report.xlsx" and m["run_details"]["run_choice"] == "Every case"
    # D1 (added 09:01) came with the customer's request of ids[0] (created 09:00), but was added later to ids[1] (08:00)
    assert first["documents"][0]["origin"] == DM.ORIGIN_CUSTOMER and first["created_date"] == "20/11/2026 09:00 AM"
    assert inst[ids[1]]["documents"][0]["origin"] == DM.ORIGIN_PROCESSOR
    assert inst[ids[1]]["documents"][0]["minutes_after_instruction_created"] == 61
    assert [x["origin"] for x in m["documents"]["D1"]["in_instructions"]] == \
        [DM.ORIGIN_CUSTOMER, DM.ORIGIN_PROCESSOR, DM.ORIGIN_UNKNOWN]
    assert inst[ids[2]]["documents_by_origin"] == {DM.ORIGIN_CUSTOMER: 0, DM.ORIGIN_PROCESSOR: 0, DM.ORIGIN_UNKNOWN: 2}
    assert inst[ids[3]]["group"] is None and inst[ids[3]]["related_instructions"] == []
    assert inst[ids[4]]["table_recorded"] is False and inst[ids[5]]["status"] == "held"
    assert inst[ids[0]]["status"] == "ready" and inst[ids[0]]["folder_location"] == "CASES_ROOT"
    assert [x["instruction_id"] for x in m["documents"]["D1"]["in_instructions"]] == [ids[0], ids[1], ids[5]]
    assert m["documents_without_id"][0]["file_name"] == "loose.pdf" and inst[ids[2]]["documents_without_id"] == 1
    assert m["summary"] == {"instructions": 6, "documents": 3, "documents_in_several_instructions": 2,
                            "documents_without_id": 1, "groups": 1, "instructions_without_table": 1}
    assert m["how_to_read"] and DM.related_of(m, ids[3]) == {"group": None, "related_instructions": [],
                                                             "document_ids": ["D4"]}


def test_the_coordinator_writes_the_map_before_it_closes_the_feed(base):
    paths, control = TP.make_paths(base)
    plan = {"cases": [{"iid": hexid(1), "account": "AB1", "needs": "send"}], "systems": "3", "reviewer": "mission_control"}
    seen = {}
    real = DM.write_map

    def spy(*a):
        seen["closed_before"] = bool(control.feed_closed())
        return real(*a)
    hand = H.Handover(paths, control, plan, "", [], alive=lambda part: False)
    H.DM.write_map = spy
    try:
        assert hand.step() is True
    finally:
        H.DM.write_map = real
    assert seen == {"closed_before": False} and control.feed_closed()
    status = DM.read_status(paths)
    assert status["ok"] and DM.read_map(paths)["instructions"][hexid(1)]["table_recorded"] is False
    assert (paths.cases_root / P.DOCUMENT_MAPS_DIR / f"{paths.run.name}.json").is_file()
    assert status["relationships_ok"] and RM.read(paths)["instructions"][hexid(1)]["related"] == []
    assert (paths.cases_root / P.DOCUMENT_MAPS_DIR / f"{paths.run.name}{RM.COPY_SUFFIX}").is_file()


def test_the_hand_over_lists_the_documents_table_after_the_audit_trail(base):
    paths, control = TP.make_paths(base)
    g, contract = TP.guidance(base, "Change of Address")
    TP.fake_download(paths, hexid(1), "AB1")
    m = P.read_json(paths.downloads / f"{hexid(1)}.json")
    (paths.work / m["folder"] / P.DOCUMENTS_TABLE_FILE).write_text("{}", "utf-8")
    P.write_json(paths.downloads / f"{hexid(1)}.json", dict(m, table_file=P.DOCUMENTS_TABLE_FILE))
    plan = {"cases": [{"iid": hexid(1), "account": "AB1", "needs": "download", "sub_worktype_csv": "Change of Address"}],
            "systems": "123", "day": P.today(), "reviewer": "copilot"}
    P.write_json(paths.snapshot("AB1"), {"account": "AB1"})
    hand = H.Handover(paths, control, plan, g, [contract])
    hand.step()
    order = P.read_json(paths.cases / f"{hexid(1)}.json")["order"]
    assert order[:3] == ["AB1_Complete_Account_Profile.json", "Audit Trail.pdf", P.DOCUMENTS_TABLE_FILE]


# ---------------------------------------------------------------------------------------------- the relationship map

def entry(iid, client, sub, source, received, subject="", docs=(), status="ready"):
    return {"instruction_id": iid, "client_number": client, "client_name": "SYNTH" if client else "",
            "client_account_number": f"AC{iid[-2:]}", "sub_worktype": sub, "source": source, "received_date": received,
            "email_subject": subject, "document_ids": list(docs), "status": status, "group": None}


def test_the_relationship_map_groups_one_customer_and_ranks_every_pair():
    i = [hexid(n) for n in range(1, 8)]
    doc_map = {"run": "r", "instructions": {e["instruction_id"]: e for e in [
        entry(i[0], "C1", "Change of Address", "Email", "20/11/2026 09:00 AM", "Change of address", ["D1"]),
        entry(i[1], "C1", "Change of Address", "Email", "20/11/2026 09:30 AM", "RE: FW:  change  of Address", ["D9"]),
        entry(i[2], "C1", "Bank Details", "Post", "20/11/2026 03:00 PM", "re: OTHER", ["D1"]),
        entry(i[3], "C1", "Change of Address", "Email", "21/11/2026 10:00 AM", "Other"),
        entry(i[4], "C1", "Bank Details", "Email", "21/11/2026 11:00 AM", "", status="held"),
        entry(i[5], "C2", "Change of Address", "Email", "20/11/2026 09:10 AM", "Change of address", ["D1"]),
        entry(i[6], "", "Change of Address", "Email", "")]}}
    rel = RM.build_relationships(doc_map, 2)
    r = rel["instructions"]
    assert [(x["instruction_id"], x["strength"]) for x in r[i[0]]["related"]] == \
        [(i[2], "strong"), (i[1], "likely"), (i[3], "likely"), (i[4], "possible")]
    assert any("share 1 document(s): D1" in x for x in r[i[0]]["related"][0]["reasons"])
    assert "same email subject (supporting only)" in r[i[0]]["related"][1]["reasons"]    # never 'strong' by itself
    assert any("30 min apart (same time cluster" in x for x in r[i[0]]["related"][1]["reasons"])
    only_subject = next(x for x in r[i[2]]["related"] if x["instruction_id"] == i[3])     # nothing else in common
    assert only_subject["strength"] == "likely" and "same email subject (supporting only)" in only_subject["reasons"]
    assert [(x["instruction_id"], x["strength"]) for x in r[i[3]]["related"]][:2] == [(i[0], "likely"), (i[1], "likely")]
    assert next(x for x in r[i[4]]["related"] if x["instruction_id"] == i[3])["strength"] == "likely"   # 1 h apart
    assert r[i[5]]["related"] == []                         # another customer: never linked, even sharing D1
    assert r[i[6]]["related"] == [] and r[i[6]]["time_cluster"] is None
    assert [r[x]["time_cluster"] for x in i[:5]] == ["C1/T1", "C1/T1", "C1/T2", "C1/T3", "C1/T3"]
    c1 = rel["customers"][0]
    assert c1["client_number"] == "C1" and c1["instructions"] == 5
    coa = c1["sub_worktypes"][0]
    assert coa["sub_worktype"] == "Change of Address" and \
        [(t["time_cluster"], t["instructions"]) for t in coa["time_clusters"]] == [("C1/T1", [i[0], i[1]]), ("C1/T3", [i[3]])]
    assert coa["time_clusters"][0]["first_received"] == "2026-11-20T09:00:00"
    assert rel["customers"][2]["client_number"] == RM.UNKNOWN_CUSTOMER
    assert rel["summary"] == {"instructions": 7, "customers": 2, "customers_with_several_instructions": 1,
                              "pairs": {"strong": 1, "likely": 5, "possible": 4}, "without_customer": 1}
    assert RM.slice_of(rel, i[0])["time_cluster"] == "C1/T1" and rel["how_to_read"] and rel["window_hours"] == 2
    assert RM.tidy_subject("Fwd: RE:  Hello  World") == "hello world"
    wider = RM.build_relationships(doc_map, 24)["instructions"]        # the window is the setting
    assert next(x for x in wider[i[2]]["related"] if x["instruction_id"] == i[4])["strength"] == "likely"


# ---------------------------------------------------------------------------------------------- Mission Control

def test_a_case_package_carries_its_table_and_related_instructions(base):
    row, handed, folder = DU.one_case(base)
    (folder / P.DOCUMENTS_TABLE_FILE).write_text("{}", "utf-8")
    related = {"group": "G1", "related_instructions": [{"instruction_id": hexid(2), "shared_document_ids": ["D1"]}],
               "document_ids": ["D1"]}
    meta, files = MC.case_package(row, dict(handed, order=handed["order"] + [P.DOCUMENTS_TABLE_FILE]), folder,
                                  [rec("D1", "a.pdf")], related)
    assert meta["package_version"] == 2 and meta["related"] == related and meta["documents_table"][0]["document_id"] == "D1"
    assert {f["name"]: f["kind"] for f in meta["files"]}[P.DOCUMENTS_TABLE_FILE] == "documents table"


def test_the_map_is_prepared_when_not_connected_and_refused_without_a_map(base):
    paths, control = TP.make_paths(base)
    cfg = DU.mc_cfg(mission_control_url="", runtime_root=str(paths.runtime_root))
    ok, detail, m, r = MC.send_map(cfg, paths)
    assert not ok and "could not be made" in detail and m is None and r is None
    DM.write_map(paths, {"cases": []}, control)
    ok, detail, m, r = MC.send_map(cfg, paths, opener=lambda *a, **k: pytest.fail("nothing may be sent"))
    assert ok and "prepared" in detail and m["map_version"] == 1 and r["window_hours"] == 2
    out = paths.runtime_root / MC.OUTBOX / paths.run.name
    assert P.read_json(out / MC.MAP_OUTBOX)["outcome"] == "prepared" and P.read_json(out / MC.REL_OUTBOX)["outcome"] == "prepared"
    status = DM.read_status(paths)                       # a relationship map that could not be made: no case is sent
    P.write_json(paths.sub("status") / DM.MAP_STATUS, dict(status, relationships_ok=False, relationships_error="boom"))
    ok, detail, *_ = MC.send_map(cfg, paths)
    assert not ok and "relationship map could not be made (boom)" in detail


def test_the_send_order_is_the_report_order():
    cases = [types.SimpleNamespace(id=i) for i in ("c", "a", "b")]
    assert [c.id for c in MC.send_order(cases, ["a", "b", "c"])] == ["a", "b", "c"]


def test_a_full_run_sends_the_map_first_then_each_case_with_its_related_instructions(base, monkeypatch, capsys,
                                                                                     trigger):
    cases = [DU.BH.case(hexid(1), "AB100000"), DU.BH.case(hexid(4), "AB400000")]
    extra = {"mission_control_url": trigger, "mission_control_auth": "key", "mission_control_key": "SECRET-MC"}
    code, out, rows, runtime, cases_root = DU.whole_run(base, monkeypatch, capsys, "full", cases, extra,
                                                        ["--scope", "all"])
    assert code == 0, out
    assert [kind for kind, *_ in DU.Trigger.sent] == ["map", "case", "case"]
    doc_map, parts = DU.Trigger.sent[0][1], DU.Trigger.sent[0][2]
    assert list(parts) == ["map", "relationship_map"]           # both maps in the one first send
    rel_map = json.loads(parts["relationship_map"].get_content())
    assert rel_map["instructions"][hexid(1)]["related"][0]["strength"] == "strong"      # they share DOC-SHARED
    assert doc_map["groups"][0]["instructions"] == [hexid(1), hexid(4)]
    assert doc_map["documents"]["DOC-SHARED"]["in_instructions"][1]["instruction_id"] == hexid(4)
    metas = {m["request_id"]: m for m, *_ in DU.Trigger.got}
    rel = metas[hexid(4)]["related"]["related_instructions"]
    assert [(r["instruction_id"], r["shared_document_ids"]) for r in rel] == [(hexid(1), ["DOC-SHARED"])]
    assert rel[0]["client_account_number"] == "AB100000" and metas[hexid(4)]["related"]["group"] == "G1"
    entry = doc_map["instructions"][hexid(1)]
    assert entry["client_account_number"] == "AB100000" and entry["bam_report"]["columns"]["RequestId"] == hexid(1)
    assert doc_map["run_details"]["mode"] == "full" and entry["status"] == "ready"
    assert metas[hexid(4)]["relationships"]["related"][0]["instruction_id"] == hexid(1)
    assert metas[hexid(1)]["documents_table"][0]["document_id"] == "DOC-SHARED"
    assert list((cases_root / P.DOCUMENT_MAPS_DIR).glob("*.json"))
    assert rows[hexid(1)]["outcome"] == "sent to Mission Control"


def test_a_map_mission_control_refuses_means_no_case_is_sent(base, monkeypatch, capsys, trigger):
    DU.Trigger.refuse_map = True
    cases = [DU.BH.case(hexid(1), "AB100000")]
    code, out, rows, *_ = DU.whole_run(base, monkeypatch, capsys, "full", cases, {"mission_control_url": trigger},
                                       ["--scope", "all"])
    assert code == 0, out
    assert [kind for kind, *_ in DU.Trigger.sent] == ["map"] and DU.Trigger.got == []
    assert rows[hexid(1)]["outcome"] == "NOT sent to Mission Control - failed"
    assert "document map did not reach Mission Control first" in rows[hexid(1)]["detail"]


# ---------------------------------------------------------------------------------------------- speed (2026-11-26)

class SlowCtx(TP.Ctx):
    """A signed-in GET that takes `delay` seconds; counts the answers given."""
    def __init__(self, table, delay):
        super().__init__(table)
        self.done, inner, ctx = 0, self.request, self

        class Request:
            async def get(self, url, timeout=0):
                await asyncio.sleep(delay)
                answer = await inner.get(url, timeout)
                ctx.done += 1
                return answer
        self.request = Request()


def test_documents_saved_side_by_side_never_take_the_same_name(base):
    paths, _ = TP.p1_setup(base)
    folder = paths.work / "f"
    folder.mkdir(parents=True)
    o = "https://system1.example.invalid"
    ctx = SlowCtx({o + "/1": TP.Resp(b"%PDF-1"), o + "/2": TP.Resp(b"%PDF-2")}, 0.05)
    rows = [{"index": i, "rowIndex": i, "documentName": "Email.pdf", "documentId": "", "viewerUrl": f"{o}/{i + 1}",
             "pairing": "position", "viewerUrlsOnPage": 2} for i in range(2)]
    out = T.run(P1.download_documents(ctx, TP.S1Page(ctx, rows, None), folder, []))
    assert sorted(r["saved_file"] for r in out) == ["Email (2).pdf", "Email.pdf"]
    assert sorted((folder / r["saved_file"]).read_bytes() for r in out) == [b"%PDF-1", b"%PDF-2"]


class FillingPage(TP.S1Page):
    """The Documents section while its table is still filling in: `empty` reads without the table first."""
    def __init__(self, ctx, rows, table, empty):
        super().__init__(ctx, rows, None, table)
        self.empty, self.reads = empty, 0

    async def evaluate(self, script, arg=None):
        if script == P1.DOCUMENTS_TABLE_JS:
            self.reads += 1
            if self.reads <= self.empty:
                return {"found": False, "headers": [], "rows": [], "heading": True}
        elif self.reads <= self.empty and "marker" in script:
            return []
        return await super().evaluate(script, arg)


def test_the_table_is_used_the_moment_it_has_rows_and_an_empty_one_after_half_a_second():
    table = {"found": True, "headers": HEADERS, "rows": [{"index": 0, "cells": ["Other", "a.pdf", "", "id-a", "", ""]}]}
    rows = [{"index": 0, "rowIndex": 0, "documentName": "a.pdf", "documentId": "id-a", "pairing": "none"}]
    page = FillingPage(None, rows, table, empty=2)
    import time as _t
    start = _t.monotonic()
    raw, found = T.run(P1.wait_documents_page(page))
    assert raw["found"] and found == rows and page.reads == 3 and _t.monotonic() - start < P1.TABLE_SETTLE
    start = _t.monotonic()
    raw, found = T.run(P1.wait_documents_page(FillingPage(None, [], dict(table, rows=[]), empty=0)))
    assert raw["found"] and found == [] and P1.TABLE_SETTLE <= _t.monotonic() - start < P1.TABLE_SETTLE + 0.5


# ---------------------------------------------------------------------------------------------- the pointer's taps

class TapFrame(TP.PointerFrame):
    def __init__(self):
        super().__init__()
        self.taps = []

    async def evaluate(self, script, arg=None):
        if script == TP.VC.TAP:
            self.taps.append(arg)
            return True
        return await super().evaluate(script, arg)


def test_the_pointer_taps_the_middle_after_documents_and_nudges_every_idle_two_seconds():
    VC = TP.VC
    s1_frame, s2_frame = TapFrame(), TapFrame()
    s1 = TP.PointerPage("https://system1.example.invalid/app/case", s1_frame)
    s2 = TP.PointerPage("https://system2.example.invalid/home", s2_frame)

    async def go():
        cur = await VC.VisualCursor.start(TP.PointerContext(s1, s2), VC.CursorConfig(), only=TP.system1_only)
        await cur.move_to(s1, 10, 10)
        VC.visual_center_tap("not a page")              # never raises, whatever it is given
        cur.center_tap(s1)
        await asyncio.sleep(0.05)
        await cur.tap(s2)                               # never on another system's tab
        cur.start_nudging(0.2)
        await asyncio.sleep(0.75)                       # idle: a tap about every 0.2 s
        busy = len(s1_frame.taps)
        for _ in range(4):                              # something visible every 0.1 s: no nudge
            await cur.move_to(s1, 20, 20)
            await asyncio.sleep(0.1)
        quiet = len(s1_frame.taps) - busy
        await cur.close()
        return busy, quiet
    busy, quiet = T.run(go())
    assert s1_frame.taps[0] == {"center": True} and s2_frame.taps == []
    assert busy >= 3 and all(t == {"center": False} for t in s1_frame.taps[1:]) and quiet == 0


# ---------------------------------------------------------------------------------------------- the console

def test_a_system1_line_shows_the_real_instruction_id_on_the_console_but_not_in_the_log_file(base, capsys):
    S = TP.S
    iid, other = hexid(7), hexid(8)
    log = S.Log(None, S.Redactor(), "full", show_ids=True)
    log.file = base / "events.txt"
    log("system1", show_id=iid, note=f"{iid} NOT downloaded: copied from {other}")
    shown = capsys.readouterr().out
    assert f"note={iid} NOT downloaded: copied from <id>" in shown          # only the line's own case is shown
    filed = (base / "events.txt").read_text("utf-8")
    assert iid not in filed and "<id> NOT downloaded" in filed
    quiet = S.Log(None, S.Redactor(), "full", show_ids=False)              # the setting off: <id> as before
    quiet("system1", show_id=iid, note=f"{iid} NOT downloaded")
    assert iid not in capsys.readouterr().out


def test_the_fakes_refuse_a_built_in_listener_like_playwright_does():
    with pytest.raises(AttributeError, match="_pw_impl_instance_"):
        TP.pw_listener([].append)


trigger = DU.trigger                                  # the local fake Mission Control endpoint (pytest fixture)
