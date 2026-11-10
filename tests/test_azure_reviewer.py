"""Tests for the Azure reviewer engine (3_azure_reviewer) - Azure is never called: Document Intelligence and the
language model are replaced by fakes, so these prove the engine's own logic, not Azure's behaviour.

Run like the other tests (files are written ONLY under SENDER_TEST_ROOT):

    set SENDER_TEST_ROOT=<a folder on approved storage>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests
"""
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

ROOT = os.environ.get("SENDER_TEST_ROOT")
if not ROOT:
    pytest.exit("Set SENDER_TEST_ROOT to a folder on approved storage; tests never use the system Temp.", 2)

ENGINE = Path(__file__).resolve().parent.parent / "3_azure_reviewer"
sys.path.insert(0, str(ENGINE))
pytest.importorskip("requests")
import azure_case_reviewer as ACR                    # noqa: E402
import azure_client as AZ                            # noqa: E402
import evidence as EV                                # noqa: E402
import reviewer as RV                                # noqa: E402

S, P, TM = ACR.S, ACR.P, ACR.TM
IID, ACCT = "0123456789abcdef0123456789abcdef", "SYN00042"
FORMS = {"email-instruction-authority-form": {
    "title": "Email instructions authority form", "pages": 2,
    "match": ["Email instructions authority form", "Authority to accept email instructions"],
    "fields": [{"id": "client_name", "label": "Client name", "page": 1, "kind": "text", "key": True},
               {"id": "client_id_numbers", "label": "Client ID number(s)", "page": 1, "kind": "text", "key": True},
               {"id": "consent", "label": "I agree", "page": 1, "kind": "checkbox", "key": False}]}}


@pytest.fixture
def base():
    p = Path(ROOT) / f"azure-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True)
    return p


# ---------------------------------------------------------------------------------------------- fakes

def di_result(pages, hw_words=(), low=None):
    """A Layout analyzeResult: pages = {n: [line text, ...]}; words in `hw_words` handwritten; low = {word: conf}."""
    content, out_pages, hw_spans = "", [], []
    for n, lines in pages.items():
        words, lns = [], []
        for text in lines:
            start = len(content)
            spans = []
            for w in text.split():
                off = len(content)
                content += w + " "
                words.append({"content": w, "confidence": (low or {}).get(w, 0.99), "span": {"offset": off, "length": len(w)}})
                if w in hw_words:
                    hw_spans.append({"offset": off, "length": len(w)})
            lns.append({"content": text, "polygon": [0, len(lns), 1, len(lns), 1, len(lns) + 1, 0, len(lns) + 1],
                        "spans": [{"offset": start, "length": len(content) - start}]})
            content += "\n"
        out_pages.append({"pageNumber": n, "lines": lns, "words": words,
                          "selectionMarks": [{"state": "selected", "confidence": 0.95, "polygon": [2, 2, 3, 2, 3, 3, 2, 3]}]})
    return {"content": content, "pages": out_pages,
            "styles": [{"isHandwritten": True, "confidence": 0.9, "spans": hw_spans}] if hw_spans else []}


FORM_PAGES = {1: ["Email instructions authority form", "Authority to accept email instructions",
                  "Client name", "ST MARY CHARITY", "Client ID number(s)", "SYN00042", "I agree"],
              2: ["Page two notes"]}


class FakeDI:
    def __init__(self, by_name):
        self.by_name, self.pages_used, self.cached_pages = by_name, 0, 0

    def analyze(self, data, paged=True):
        return [self.by_name[data.decode("ascii").strip()]]


class FakeChat:
    def __init__(self, answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, model, messages, name, schema, what):
        self.calls.append((name, model, [m["content"] for m in messages]))
        self.usage["calls"] += 1
        obj = self.answers[name].pop(0)
        if isinstance(obj, Exception):
            raise obj
        return obj, json.dumps(obj), model


def form_answer(name_cite="D3.p1.l4", id_cite="D3.p1.l6", name="ST MARY CHARITY"):
    return {"FIELDS": [{"FIELD": "client_name", "VALUE": name, "CITATIONS": [name_cite]},
                       {"FIELD": "client_id_numbers", "VALUE": "SYN00042", "CITATIONS": [id_cite]},
                       {"FIELD": "consent", "VALUE": "ticked", "CITATIONS": ["D3.p1.s1"]}], "NOTES": ""}


def review_answer(verdict="Correct", match=True, field_ids=("D3.client_name",), status="Satisfied", rule_ids=()):
    return {"REVIEW_RESULT": verdict, "DISCREPANCY_TYPE": "None" if match is not False else TM.PROCESSOR_ERROR,
            "HEADLINE": "h", "CASE_END_DATETIME": "03/11/2026 10:06 AM",
            "REGISTER_SNAPSHOT_DATETIME": "2026-11-04T10:00:00+00:00", "CLIENT_REQUEST": "c",
            "JUSTIFICATION": ("" if match is not False else TM.PROCESSOR_ERROR + ": ") + "j",
            "A_B_COMPARISONS": [{"ITEM": "Client name", "Customer_expectation": "ST MARY CHARITY",
                                 "CUSTOMER_SOURCE": "D3", "System_data": "ST MARY CHARITY", "SYSTEM_SOURCE": "A > B",
                                 "MATCH": match, "EXPLANATION": "Same value.", "FIELD_IDS": list(field_ids)}],
            "RULE_CHECKS": [{"RULE": "r", "STATUS": status, "EVIDENCE": "e", "NOTE": "n", "FIELD_IDS": list(rule_ids)}]}


def make_case(base, files=None, form_pages=None):
    """Snapshot (D1), Audit Trail (D2), the form (D3); 'files' adds more.  Fake DI keys on the file content."""
    case = base / "cases" / f"{IID} - {ACCT}"
    case.mkdir(parents=True)
    (case / f"{ACCT}{P.SNAPSHOT_SUFFIX}").write_text(json.dumps(
        {"Account Summary": {"metadata": {"retrievedUtc": "2026-11-04T10:00:00+00:00"}}}), "utf-8")
    (case / "Audit Trail.pdf").write_bytes(b"audit")
    (case / "Email instruction authority.pdf").write_bytes(b"form")
    for name, data in (files or {}).items():
        (case / name).write_bytes(data)
    rules = base / "Email Instruction Authority.md"
    rules.write_text("1. Check the form.", "utf-8")
    di = FakeDI({"audit": di_result({1: ["Audit Trail", "03/11/2026 received by email"]}),
                 "form": form_pages or di_result(FORM_PAGES)})
    ci = RV.CaseInput(iid=IID, folder=case, account=ACCT, sub_worktype="Email Instruction Authority",
                      case_source="Email", rules=rules, end_date="03/11/2026 10:06 AM")
    return ci, di


def make_reviewer(di, chat, forms=FORMS, threshold=0.80):
    holidays = TM.Holidays(None)
    return RV.Reviewer(di=di, chat=chat, forms=forms, form_model="form-m", case_model="case-m", threshold=threshold,
                       daily="daily rules", contract="contract", base="Ended {{CASE_END_DATETIME}}",
                       validate=S.validate_result, discrepancy_words=S.DISCREPANCY_WORDS,
                       timing=lambda e, r, f: TM.case_timing(e, r, holidays, from_csv=f),
                       snapshot_suffix=P.SNAPSHOT_SUFFIX, fixups=2)


# ---------------------------------------------------------------------------------------------- evidence

def test_citation_ids_handwriting_and_tick_boxes_come_from_the_layout_result():
    ev = EV.DocEvidence.from_di("D3", "f.pdf", [di_result(FORM_PAGES, hw_words={"SYN00042"})])
    assert ev.pages == 2 and ev.index["D3.p1.l6"].text == "SYN00042" and ev.index["D3.p1.l6"].hw
    assert ev.index["D3.p1.l4.w2"].text == "MARY" and not ev.index["D3.p1.l4.w2"].hw
    assert ev.index["D3.p1.s1"].selected
    text = ev.render()
    assert "[D3.p1.l6] SYN00042  {handwritten}" in text and "[D3.p1.s1] [X] ticked" in text and "--- page 2 ---" in text


def test_a_field_is_flagged_below_the_mark_or_when_its_citation_does_not_hold_it():
    ev = EV.DocEvidence.from_di("D3", "f.pdf", [di_result(FORM_PAGES, hw_words={"SYN00042"}, low={"SYN00042": 0.62})])
    f = {"id": "client_id_numbers", "label": "Client ID", "kind": "text"}
    row = EV.derive_field(ev, f, "SYN 00042", ["D3.p1.l6"], 0.80)
    assert row["HANDWRITTEN"] and row["CONFIDENCE"] == 0.62 and row["LOW_CONFIDENCE"] and "0.62" in row["REASON"]
    good = EV.derive_field(ev, {**f, "id": "client_name"}, "St Mary Charity", ["D3.p1.l4"], 0.80)
    assert not good["LOW_CONFIDENCE"] and not good["HANDWRITTEN"] and good["CONFIDENCE"] == 0.99
    assert EV.derive_field(ev, f, "SYN00042", ["D3.p9.l1"], 0.80)["REASON"].startswith("cited text not found")
    assert "not in the cited text" in EV.derive_field(ev, f, "SYN99999", ["D3.p1.l6"], 0.80)["REASON"]
    assert EV.derive_field(ev, f, "SYN00042", [], 0.80)["LOW_CONFIDENCE"]
    blank = EV.derive_field(ev, f, "", [], 0.80)
    assert blank["VALUE"] == "" and not blank["LOW_CONFIDENCE"]
    box = {"id": "consent", "kind": "checkbox"}
    assert not EV.derive_field(ev, box, "ticked", ["D3.p1.s1"], 0.80)["LOW_CONFIDENCE"]
    assert "does not show" in EV.derive_field(ev, box, "not ticked", ["D3.p1.s1"], 0.80)["REASON"]
    sig = EV.derive_field(ev, {"id": "sig", "kind": "signature"}, "not signed", [], 0.80)
    assert sig["VALUE"] == "possibly not signed" and sig["LOW_CONFIDENCE"]       # never 'not signed' at 1.0


def test_text_files_are_read_here_with_full_confidence():
    ev = EV.DocEvidence.from_text("D4", "note.txt", "Hello there\n\nSecond line")
    assert ev.local and ev.index["D4.p1.l3"].text == "Second line"
    row = EV.derive_field(ev, {"id": "x", "kind": "text"}, "there", ["D4.p1.l1.w2"], 0.8)
    assert row["CONFIDENCE"] == 1.0 and not row["HANDWRITTEN"] and not row["LOW_CONFIDENCE"]


# ---------------------------------------------------------------------------------------------- Azure client

class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url, headers, kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def http_with(replies, mode="key"):
    cfg = types.SimpleNamespace(azure_auth=mode, azure_docintel_key="K1", azure_openai_key="K2", az_cli_path="az")
    sess = Session(replies)
    waits = []
    h = AZ.Http(AZ.Auth(cfg), sleep=waits.append, session_factory=lambda: sess)
    return h, sess, waits


def test_throttling_is_retried_with_retry_after_then_held_after_three_retries():
    h, sess, waits = http_with([Resp(429, headers={"Retry-After": "7"}), Resp(200, {"ok": 1})])
    assert h.call("GET", "https://x", "di", "t").status_code == 200 and waits == [7.0]
    assert sess.calls[0][2] == {"Ocp-Apim-Subscription-Key": "K1"}
    h, sess, waits = http_with([Resp(500)] * 4)
    with pytest.raises(AZ.TemporaryError, match="after 3 retries"):
        h.call("GET", "https://x", "openai", "t")
    assert waits == [2, 5, 15] and sess.calls[0][2] == {"api-key": "K2"}


@pytest.mark.parametrize("status,body,kind", [
    (401, {}, AZ.SafeStop), (404, {"error": {"code": "DeploymentNotFound", "message": "m"}}, AZ.SafeStop),
    (403, {"error": {"code": "403", "message": "Out of call volume quota for FormRecognizer F0"}}, AZ.SafeStop),
    (400, {"error": {"code": "content_filter", "message": "filtered"}}, AZ.Refused),
    (400, {"error": {"code": "BadArgument", "message": "bad"}}, AZ.BadRequest)])
def test_credentials_quota_and_endpoints_stop_safely_refusals_and_rejections_hold(status, body, kind):
    h, _sess, waits = http_with([Resp(status, body)])
    with pytest.raises(kind):
        h.call("POST", "https://x", "openai", "t")
    assert waits == []


def test_signin_uses_the_az_token_and_a_missing_sign_in_is_a_safe_stop():
    ok = types.SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "T", "expires_on": 9e12}))
    cfg = types.SimpleNamespace(azure_auth="cli", azure_docintel_key="", azure_openai_key="", az_cli_path="az")
    auth = AZ.Auth(cfg, run=lambda *a, **k: ok)
    assert auth.mode == "signin" and auth.headers("di") == {"Authorization": "Bearer T"}
    bad = AZ.Auth(cfg, run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(AZ.SafeStop, match="az login"):
        bad.headers("openai")


class PieceHttp:
    """Answers analyze POSTs for pages=a-b from a file of `total` pages; 400 past the end."""

    def __init__(self, total):
        self.total, self.posts = total, []
        self.sleep = lambda s: None

    def call(self, method, url, service, what, **kw):
        if method == "POST":
            pages = url.split("pages=")[1] if "pages=" in url else f"1-{self.total}"
            self.posts.append(pages)
            a, b = map(int, pages.split("-"))
            if a > self.total:
                raise AZ.BadRequest("Document Intelligence: Azure rejected the request (400) (InvalidArgument)")
            self.result = {"pages": [{"pageNumber": n} for n in range(a, min(b, self.total) + 1)]}
            return Resp(202, headers={"Operation-Location": "https://op"})
        return Resp(200, {"status": "succeeded", "analyzeResult": self.result})


@pytest.mark.parametrize("total,posts", [(3, ["1-2", "3-4"]), (4, ["1-2", "3-4", "5-6"]), (1, ["1-2"])])
def test_the_free_tier_reads_2_page_pieces_and_a_file_seen_before_costs_nothing(base, total, posts):
    http = PieceHttp(total)
    di = AZ.DocIntel("https://di", http, base / "cache", free=True)
    res = di.analyze(b"%PDF same bytes")
    assert http.posts == posts and sum(len(r["pages"]) for r in res) == total and di.pages_used == total
    again = AZ.DocIntel("https://di", http, base / "cache", free=True)
    again.analyze(b"%PDF same bytes")
    assert http.posts == posts and again.pages_used == 0 and again.cached_pages == total


# ---------------------------------------------------------------------------------------------- one case

def test_a_clean_case_is_correct_with_documents_fields_and_the_case_source_in_the_question(base):
    ci, di = make_case(base)
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [review_answer()]})
    rec = make_reviewer(di, chat).review(ci)
    assert rec["state"] == "result_correct", rec["reason"]
    assert [d["KIND"] for d in rec["DOCUMENTS"]] == ["snapshot", "audit_trail", "form"]
    assert rec["DOCUMENTS"][2]["FORM"] == "email-instruction-authority-form"
    assert {f["FIELD_ID"] for f in rec["EXTRACTED_FIELDS"]} == {"D3.client_name", "D3.client_id_numbers", "D3.consent"}
    assert rec["result"]["A_B_COMPARISONS"][0]["FIELD_IDS"] == ["D3.client_name"]
    question = chat.calls[-1][2][1]
    assert "Case source (where the customer request came in, from the Studio report): Email" in question
    assert "Ended 03/11/2026 10:06 AM" in question and "[D3.p1.l4] ST MARY CHARITY" in question
    assert "PROVENANCE" not in json.dumps(rec["DOCUMENTS"])


def test_a_comparison_on_a_low_confidence_field_becomes_could_not_compare(base):
    ci, di = make_case(base, form_pages=di_result(FORM_PAGES, hw_words={"MARY"}, low={"MARY": 0.41}))
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [review_answer()]})
    rec = make_reviewer(di, chat).review(ci)
    row = rec["result"]["A_B_COMPARISONS"][0]
    assert row["MATCH"] is None and row["EXPLANATION"].startswith("Could not compare")
    assert rec["state"] == "result_inconclusive" and rec["result"]["REVIEW_RESULT"] == "Inconclusive"
    assert "Program note" in rec["result"]["JUSTIFICATION"]
    assert make_reviewer(di, chat).problems(rec["result"], None) == []      # still valid by the contract


def test_an_unreadable_key_field_or_a_short_form_is_never_correct(base):
    ci, di = make_case(base, form_pages=di_result({1: FORM_PAGES[1]}, low={"SYN00042": 0.5}))
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [review_answer()]})
    rec = make_reviewer(di, chat).review(ci)
    items = [r["ITEM"] for r in rec["result"]["A_B_COMPARISONS"]]
    assert rec["state"] == "result_inconclusive"
    assert any(i.startswith("Program check - Client ID") for i in items)
    assert any(i.startswith("Program check - pages of") for i in items)


def test_an_answer_breaking_the_contract_is_asked_again_then_invalid(base):
    ci, di = make_case(base)
    wrong = review_answer(verdict="Incorrect")           # Incorrect without any mismatch: the contract says no
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [wrong, review_answer()]})
    rec = make_reviewer(di, chat).review(ci)
    assert rec["state"] == "result_correct" and "rejected" in chat.calls[-1][2][-1]
    ci2, di2 = make_case(base / "b")
    chat2 = FakeChat({"form_fields": [form_answer()], "case_review": [wrong] * 3})
    rec2 = make_reviewer(di2, chat2).review(ci2)
    assert rec2["state"] == "result_invalid" and "COPILOT_RESULT" not in rec2["reason"]
    assert "Incorrect needs a MATCH false" in rec2["reason"] and len(chat2.calls) == 1 + 3


def test_held_cases_unreadable_types_no_rules_refusals_and_program_errors(base):
    ci, di = make_case(base / "a", files={"mail.msg": b"x"})
    assert "cannot read: mail.msg" in make_reviewer(di, FakeChat({})).review(ci)["reason"]
    ci, di = make_case(base / "b")
    ci.rules = None
    assert make_reviewer(di, FakeChat({})).review(ci)["state"] == "held"
    ci, di = make_case(base / "c")
    rec = make_reviewer(di, FakeChat({"form_fields": [AZ.Refused("form reading: refused")]})).review(ci)
    assert rec["state"] == "held" and "refused" in rec["reason"]
    ci, di = make_case(base / "d")
    di.analyze = lambda data, paged=True: 1 / 0
    rec = make_reviewer(di, FakeChat({})).review(ci)
    assert rec["state"] == "held" and "program error" in rec["reason"] and "ZeroDivisionError" in rec["_trace"]


def test_a_safe_stop_carries_the_cases_record(base):
    ci, di = make_case(base)
    chat = FakeChat({"form_fields": [AZ.SafeStop("quota used up")]})
    with pytest.raises(AZ.SafeStop) as info:
        make_reviewer(di, chat).review(ci)
    assert info.value.record["state"] == "not_sent_stopped"


def test_no_form_map_reads_every_document_as_supporting(base):
    ci, di = make_case(base)
    chat = FakeChat({"case_review": [review_answer(field_ids=())]})
    rec = make_reviewer(di, chat, forms={}).review(ci)
    assert rec["state"] == "result_correct" and rec["EXTRACTED_FIELDS"] == []
    assert [d["KIND"] for d in rec["DOCUMENTS"]] == ["snapshot", "audit_trail", "supporting"]


# ---------------------------------------------------------------------------------------------- review command

def test_review_writes_one_result_file_per_case_with_a_state_and_the_reports(base, monkeypatch, capsys):
    ci, di = make_case(base)
    csv = base / "studio.csv"
    other = "f" * 32
    csv.write_text("Request Id,Client Account Number,Client Number,Process Status,Sub Worktype,End Date,Case Source\n"
                   f"{IID},{ACCT},C1,Ended,Email Instruction Authority,03/11/2026 10:06 AM,Email\n"
                   f"{other},SYN00099,C2,Ended,Email Instruction Authority,,Post\n", "utf-8")
    guidance = base / "guidance"
    guidance.mkdir()
    (guidance / "Email Instruction Authority.md").write_text("1. Check the form.", "utf-8")
    cfg = {"runtime_root": str(base / "rt"), "reviewer": "azure", "azure_docintel_endpoint": "https://di.example",
           "azure_openai_endpoint": "https://oa.example", "azure_auth": "key", "azure_docintel_key": "SECRETKEY1",
           "azure_openai_key": "SECRETKEY2", "azure_form_model": "form-m", "azure_review_model": "case-m"}
    (base / "cfg.json").write_text(json.dumps(cfg), "utf-8")
    monkeypatch.setenv("SENDER_CONFIG", str(base / "cfg.json"))
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [review_answer()]})
    monkeypatch.setattr(ACR, "build", lambda cfg, case_model=None, stop=None, on_wait=None: make_reviewer(di, chat))
    out = base / "out"
    code = ACR.main(["review", "--csv", str(csv), "--cases-root", str(base / "cases"), "--guidance", str(guidance),
                     "--out", str(out)])
    assert code == 0
    a = json.loads((out / f"{IID}.json").read_text("utf-8"))
    b = json.loads((out / f"{other}.json").read_text("utf-8"))
    assert a["state"] == "result_correct" and a["case_source"] == "Email" and "_turns" not in a
    assert b["state"] == "held" and "not found" in b["reason"]
    reports = "".join(p.read_text("utf-8") for p in out.glob("azure-*.md"))
    assert len(list(out.glob("azure-responses-*.md"))) == 1 and len(list(out.glob("azure-run-report-*.md"))) == 1
    assert "SECRETKEY" not in reports and "di.example" not in reports and "oa.example" not in reports
    assert "SUMMARY" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- form recognition

RECOG_FORMS = {
    "change-address-form": {"title": "Change of address form", "pages": 4,
                            "match": ["Use this form to change the address"], "fields": []},
    "change-signatory-form": {"title": "Change of signatory form", "pages": 6,
                              "match": ["Use this form to remove and/or add new signatories",
                                        "Removal of authorised signatories"], "fields": []},
    "coif-amend": {"title": "Income and withdrawal payments amendment form", "pages": 4,
                   "match": ["COIF Charity Funds"], "fields": []},
    "coif-mandate": {"title": "Mandate form", "pages": 19, "match": ["COIF Charity Funds"], "fields": []}}


def doc(pages):
    return EV.DocEvidence.from_di("D3", "x.pdf", [di_result(pages)])


def test_a_form_naming_another_form_in_its_notes_is_still_recognised_by_its_own_title():
    pages = {n: ["filler"] for n in range(1, 7)}
    pages[1] = ["Use this form to remove and/or add new signatories", "Change of signatory form",
                "If only your address changed, please use our Change of address form instead.",
                "Removal of authorised signatories"]
    assert RV.detect_form(RECOG_FORMS, doc(pages), "Change of authorised signatories form.pdf") == \
        ("change-signatory-form", "")


def test_a_close_score_is_settled_by_the_page_count_else_the_form_is_unknown_with_a_check_note():
    three = {1: ["COIF Charity Funds", "Income and withdrawal payments", "amendment form"], 2: ["x"], 3: ["y"]}
    assert RV.detect_form(RECOG_FORMS, doc(three), "a.pdf")[0] == "coif-amend"     # 3 pages: closer to 4 than 19
    tie = {"a": {"title": "Alpha form", "pages": 2, "match": ["Shared heading"], "fields": []},
           "b": {"title": "Beta form", "pages": 2, "match": ["Shared heading"], "fields": []}}
    slug, note = RV.detect_form(tie, doc({1: ["Shared heading"], 2: ["z"]}), "scan.pdf")
    assert slug == "" and note.startswith("CHECK:") and "Alpha form" in note and "Beta form" in note


def test_text_documents_are_never_forms(base):
    ev = EV.DocEvidence.from_text("D4", "Email from client.eml", "Change of address form\nUse this form to change "
                                  "the address")
    assert RV.detect_form(RECOG_FORMS, ev, "Email from client.eml") == ("", "")
    ci, di = make_case(base, files={"Email from client.eml": b"Change of address form\nUse this form to change"})
    chat = FakeChat({"form_fields": [form_answer()], "case_review": [review_answer()]})
    rec = make_reviewer(di, chat, forms={**FORMS, **RECOG_FORMS}).review(ci)
    kinds = {d["FILE"]: d["KIND"] for d in rec["DOCUMENTS"]}
    assert kinds["Email from client.eml"] == "supporting" and kinds["Email instruction authority.pdf"] == "form"


# ---------------------------------------------------------------------------------------------- signatures

def placed(page_lines, size=(8.5, 11.0)):
    """A Layout result whose lines sit at given positions: [(text, x, y, handwritten, confidence)] on page 1."""
    content, words, lines, hw = "", [], [], []
    for text, x, y, is_hw, conf in page_lines:
        start = len(content)
        for k, w in enumerate(text.split()):
            off = len(content)
            content += w + " "
            cx = x + k * 0.5
            words.append({"content": w, "confidence": conf, "span": {"offset": off, "length": len(w)},
                          "polygon": [cx - 0.1, y - 0.05, cx + 0.1, y - 0.05, cx + 0.1, y + 0.05, cx - 0.1, y + 0.05]})
            if is_hw:
                hw.append({"offset": off, "length": len(w)})
        lines.append({"content": text, "polygon": [x, y - 0.05, x + 1, y - 0.05, x + 1, y + 0.05, x, y + 0.05],
                      "spans": [{"offset": start, "length": len(content) - start}]})
    return {"content": content, "styles": [{"isHandwritten": True, "spans": hw}] if hw else [],
            "pages": [{"pageNumber": 1, "width": size[0], "height": size[1], "lines": lines, "words": words}]}


SIG = {"id": "trustee_1_signature", "label": "Signature", "kind": "signature", "key": True, "page": 1,
       "rect": [72, 72 * 5, 72 * 4, 72 * 6]}            # 1-4 in, 5-6 in on an A4-sized page (scaled to the page)


def test_any_mark_in_the_signature_space_is_signed_and_never_flagged_for_reading_confidence():
    a4 = (8.27, 11.69)
    ev = EV.DocEvidence.from_di("D3", "f.pdf", [placed([("Signature", 1.1, 4.8, False, 0.99),
                                                        ("vrrDalos", 2.0, 5.5 * 11.69 / 11.69, True, 0.31)], a4)])
    row = EV.derive_field(ev, SIG, "", [], 0.80)                # the model saw nothing: the area check finds it
    assert row["VALUE"] == "signed" and not row["LOW_CONFIDENCE"] and row["CONFIDENCE"] >= 0.80
    assert row["HANDWRITTEN"] and row["CITATIONS"] == ["D3.p1.l2.w1"]


def test_an_empty_signature_space_is_possibly_not_signed_and_always_flagged():
    a4 = (8.27, 11.69)
    ev = EV.DocEvidence.from_di("D3", "f.pdf", [placed([("Signature", 1.1, 4.8, False, 0.99)], a4)])
    row = EV.derive_field(ev, SIG, "signed", ["D3.p1.l1"], 0.80)    # the printed label is not a signature
    assert row["VALUE"] == "possibly not signed" and row["LOW_CONFIDENCE"] and row["CONFIDENCE"] == 0.0
    text = [("For those persons who have authorised this form and live", 1.1, 5.5, False, 0.99)]
    shifted = EV.DocEvidence.from_di("D3", "f.pdf", [placed(text, a4)])          # running printed text in the area
    assert EV.derive_field(shifted, SIG, "", [], 0.80)["VALUE"] == "possibly not signed"


def test_a_rule_check_resting_on_a_possibly_missing_signature_is_not_determinable(base):
    ci, di = make_case(base)
    forms = {"email-instruction-authority-form": {**FORMS["email-instruction-authority-form"], "fields": [
        *FORMS["email-instruction-authority-form"]["fields"],
        {"id": "trustee_1_signature", "label": "Signature", "page": 1, "kind": "signature", "key": True}]}}
    answer = {**form_answer(), "FIELDS": form_answer()["FIELDS"] + [
        {"FIELD": "trustee_1_signature", "VALUE": "possibly not signed", "CITATIONS": []}]}
    bad = review_answer(verdict="Incorrect", status="Not satisfied", rule_ids=("D3.trustee_1_signature",))
    chat = FakeChat({"form_fields": [answer], "case_review": [bad]})
    rec = make_reviewer(di, chat, forms=forms).review(ci)
    rule = rec["result"]["RULE_CHECKS"][0]
    assert rule["STATUS"] == "Not determinable" and "Not satisfied" in rule["NOTE"]
    assert rec["state"] == "result_inconclusive" and rec["result"]["DISCREPANCY_TYPE"] == "None"


# ---------------------------------------------------------------------------------------------- format-safe fixes

@pytest.mark.parametrize("fdef,read,value,flagged", [
    ({"id": "account_number_1"}, "C01234567", "CO1234567", False),        # 0 -> O in the 2-letter prefix
    ({"id": "account_number_1"}, "C81234567", "CB1234567", False),
    ({"id": "account_number_1"}, "C0400106", "C0400106", False),          # 6 digits: not the format, left alone
    ({"id": "account_number_1"}, "CO12345X7", "CO12345X7", False),        # digits part not digits: left alone
    ({"id": "client_id_numbers", "label": "Client ID number(s)"}, "40O1067", "4001067", False),
    ({"id": "new_postcode"}, "CHI INQ", "CHI 1NQ", False),                # inward: digit + 2 letters only
    ({"id": "new_postcode"}, "0X1 1AB", "OX1 1AB", False),              # outward code starts with a letter
    ({"id": "old_postcode"}, "NN67AP", "NN6 7AP", False),
    ({"id": "daytime_phone"}, "01904 9O1010", "01904901010", False),
    ({"id": "date_of_birth", "kind": "date"}, "17////1977", "17/11/1977", False),
    ({"id": "signed_date", "kind": "date"}, "17/////977", "17/11/1977", False),
    ({"id": "signed_date", "kind": "date"}, "31/02/2026", "31/02/2026", True),   # still invalid: flagged
])
def test_format_safe_normalisation_keeps_the_raw_reading(fdef, read, value, flagged):
    ev = EV.DocEvidence.from_text("D3", "f.txt", read)
    row = EV.derive_field(ev, {"kind": "text", **fdef}, read, ["D3.p1.l1"], 0.80)
    assert row["VALUE"] == value and row["RAW_VALUE"] == read and row["LOW_CONFIDENCE"] is flagged
    assert bool(row["NORMALISED"]) is (value != read)


def test_an_empty_key_field_is_flagged_and_its_mismatch_becomes_could_not_compare(base):
    ci, di = make_case(base)
    empty = {**form_answer(), "FIELDS": [{"FIELD": "client_name", "VALUE": "", "CITATIONS": []}]
             + form_answer()["FIELDS"][1:]}
    wrong = review_answer(verdict="Incorrect", match=False)
    chat = FakeChat({"form_fields": [empty], "case_review": [wrong]})
    rec = make_reviewer(di, chat).review(ci)
    field = next(f for f in rec["EXTRACTED_FIELDS"] if f["FIELD"] == "client_name")
    assert field["LOW_CONFIDENCE"] and field["CONFIDENCE"] == 0.0 and "key field empty" in field["REASON"]
    assert rec["result"]["A_B_COMPARISONS"][0]["MATCH"] is None and rec["state"] == "result_inconclusive"


# ---------------------------------------------------------------------------------------------- connections

class Server:
    """A fake Azure: Document Intelligence (info, analyze, result) and the model, in v1 or classic style."""

    def __init__(self, v1=True, strict=True, ssl_fail=0):
        self.v1, self.strict, self.ssl_fail, self.calls, self.mounted = v1, strict, ssl_fail, [], []

    def factory(self):
        server = self

        class Sess:
            def mount(self, prefix, adapter):
                server.mounted.append(type(adapter).__name__)

            def request(self, method, url, headers=None, timeout=None, **kw):
                server.calls.append((method, url, dict(headers or {}), kw))
                if server.ssl_fail:
                    server.ssl_fail -= 1
                    import requests
                    raise requests.exceptions.SSLError("certificate verify failed")
                return server.answer(method, url, kw)
        return Sess()

    def answer(self, method, url, kw):
        if "/documentintelligence/info" in url:
            return Resp(200, {"customDocumentModels": {}})
        if ":analyze" in url:
            return Resp(202, headers={"Operation-Location": "https://di.example/op/1"})
        if "/op/1" in url:
            return Resp(200, {"status": "succeeded", "analyzeResult": {
                "content": "AZURE REVIEWER CHECK", "pages": [{"pageNumber": 1}]}})
        if "/openai/v1/" in url and not self.v1:
            return Resp(404, {"error": {"code": "404", "message": "Resource not found"}})
        if not self.strict:
            return Resp(400, {"error": {"code": "BadRequest", "message": "response_format value as json_schema is "
                                                                        "enabled only for api versions 2024-08-01"}})
        body = {"RESULT": "Inconclusive", "ROWS": [{"ITEM": "check", "MATCH": None}]}
        return Resp(200, {"model": "fake-model", "choices": [{"finish_reason": "stop",
                                                              "message": {"content": json.dumps(body)}}]})


def key_cfg(**over):
    return types.SimpleNamespace(**{"azure_auth": "key", "azure_docintel_key": "SECRET-DI-KEY",
                                    "azure_openai_key": "SECRET-OA-KEY", "az_cli_path": "", **over})


def test_a_classic_endpoint_is_called_with_its_deployment_and_api_version():
    server = Server(v1=False)
    http = AZ.Http(AZ.Auth(key_cfg()), sleep=lambda s: None, session_factory=server.factory)
    chat = AZ.Chat("https://fnz-apim.example/aoai/openai/deployments/gpt-x/chat/completions?api-version=2025-01-01",
                   http)
    assert chat.ping("case-dep") == "fake-model"
    url = server.calls[-1][1]
    assert url == "https://fnz-apim.example/aoai/openai/deployments/case-dep/chat/completions?api-version=2025-01-01"
    assert server.calls[-1][2]["api-key"] == "SECRET-OA-KEY"


def test_v1_answering_404_switches_to_classic_for_the_rest_of_the_run():
    server = Server(v1=False)
    http = AZ.Http(AZ.Auth(key_cfg()), sleep=lambda s: None, session_factory=server.factory)
    chat = AZ.Chat("https://res.example/", http, "2024-10-21")
    chat.ping("form-dep")
    chat.ping("case-dep")
    urls = [c[1] for c in server.calls]
    assert urls[0].endswith("/openai/v1/chat/completions") and "deployments/form-dep" in urls[1]
    assert len(urls) == 3 and "deployments/case-dep" in urls[2] and "api-version=2024-10-21" in urls[2]
    assert chat.style_used.startswith("classic")


def test_a_model_without_strict_json_schema_is_a_plain_safe_stop():
    server = Server(strict=False)
    http = AZ.Http(AZ.Auth(key_cfg()), sleep=lambda s: None, session_factory=server.factory)
    with pytest.raises(AZ.SafeStop, match="does not accept strict JSON schema"):
        AZ.Chat("https://res.example", http).ping("old-dep")


def test_tls_inspection_switches_once_to_the_windows_certificate_store():
    server = Server(ssl_fail=1)
    http = AZ.Http(AZ.Auth(key_cfg()), sleep=lambda s: None, session_factory=server.factory)
    assert http.call("GET", "https://di.example/documentintelligence/info", "di", "t").status_code == 200
    assert http.tls == "windows" and server.mounted == ["WindowsTLS"]
    http.call("GET", "https://di.example/documentintelligence/info", "di", "t")
    assert server.mounted == ["WindowsTLS"]                       # kept, not mounted again
    still = Server(ssl_fail=2)
    http = AZ.Http(AZ.Auth(key_cfg()), sleep=lambda s: None, session_factory=still.factory)
    with pytest.raises(AZ.SafeStop, match="TLS") as info:
        http.call("GET", "https://di.example/x", "di", "Document Intelligence check")
    assert "di.example" not in str(info.value)


def test_proxy_and_connection_problems_are_said_plainly():
    import requests
    h, _sess, _waits = http_with([requests.exceptions.ProxyError("p")] * 4)
    with pytest.raises(AZ.TemporaryError, match="proxy"):
        h.call("GET", "https://x", "di", "t")
    h, _sess, _waits = http_with([requests.exceptions.ConnectionError("c")] * 4)
    with pytest.raises(AZ.TemporaryError, match="network, proxy or firewall"):
        h.call("GET", "https://x", "di", "t")


def test_the_paid_tier_reads_the_whole_document_in_one_request(base):
    http = PieceHttp(7)
    di = AZ.DocIntel("https://di", http, base / "cache", free=False)
    res = di.analyze(b"%PDF seven pages")
    assert http.posts == ["1-7"] and sum(len(r["pages"]) for r in res) == 7 and di.pages_used == 7


def test_check_proves_the_production_calls_with_keys_and_never_shows_them(base, monkeypatch, capsys):
    cfg = {"runtime_root": str(base / "rt"), "reviewer": "azure", "azure_docintel_endpoint": "https://di.example",
           "azure_openai_endpoint": "https://oa.example", "azure_auth": "key", "azure_docintel_key": "SECRET-DI-KEY",
           "azure_openai_key": "SECRET-OA-KEY", "azure_form_model": "form-m", "azure_review_model": "case-m"}
    (base / "cfg.json").write_text(json.dumps(cfg), "utf-8")
    monkeypatch.setenv("SENDER_CONFIG", str(base / "cfg.json"))
    server = Server()
    real_http = AZ.Http

    def fake_http(auth, **kw):
        return real_http(auth, sleep=lambda s: None, session_factory=server.factory)
    monkeypatch.setattr(AZ, "Http", fake_http)
    assert ACR.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "[ok] Document Intelligence analyze (prebuilt-layout, 1 test page)" in out
    assert "[ok] form model 'form-m' answers in the strict JSON schema" in out
    assert "[ok] case model 'case-m' answers in the strict JSON schema" in out
    assert "Azure authentication - key" in out and "TLS): standard" in out
    assert "SECRET" not in out and "di.example" not in out and "oa.example" not in out
    di_calls = [c for c in server.calls if "di.example" in c[1]]
    oa_calls = [c for c in server.calls if "oa.example" in c[1]]
    assert all(c[2].get("Ocp-Apim-Subscription-Key") == "SECRET-DI-KEY" and "api-key" not in c[2] for c in di_calls)
    assert all(c[2].get("api-key") == "SECRET-OA-KEY" and "Ocp-Apim-Subscription-Key" not in c[2] for c in oa_calls)
    analyze = [c for c in di_calls if ":analyze" in c[1]]
    assert len(analyze) == 1 and "pages=1-2" in analyze[0][1] and "prebuilt-layout" in analyze[0][1]
    body = next(c[3]["json"] for c in oa_calls)
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"]
