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


def review_answer(verdict="Correct", match=True, field_ids=("D3.client_name",), status="Satisfied"):
    return {"REVIEW_RESULT": verdict, "DISCREPANCY_TYPE": "None" if match is not False else TM.PROCESSOR_ERROR,
            "HEADLINE": "h", "CASE_END_DATETIME": "03/11/2026 10:06 AM",
            "REGISTER_SNAPSHOT_DATETIME": "2026-11-04T10:00:00+00:00", "CLIENT_REQUEST": "c",
            "JUSTIFICATION": ("" if match is not False else TM.PROCESSOR_ERROR + ": ") + "j",
            "A_B_COMPARISONS": [{"ITEM": "Client name", "Customer_expectation": "ST MARY CHARITY",
                                 "CUSTOMER_SOURCE": "D3", "System_data": "ST MARY CHARITY", "SYSTEM_SOURCE": "A > B",
                                 "MATCH": match, "EXPLANATION": "Same value.", "FIELD_IDS": list(field_ids)}],
            "RULE_CHECKS": [{"RULE": "r", "STATUS": status, "EVIDENCE": "e", "NOTE": "n"}]}


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
    assert not EV.derive_field(ev, {"id": "sig", "kind": "signature"}, "not signed", [], 0.80)["LOW_CONFIDENCE"]


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
    assert S.validate_result({("COPILOT_RESULT" if k == "REVIEW_RESULT" else k): v for k, v in rec["result"].items()
                              if k != "A_B_COMPARISONS"} | {"A_B_COMPARISONS": [
        {k: v for k, v in r.items() if k != "FIELD_IDS"} for r in rec["result"]["A_B_COMPARISONS"]]}) == []


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
