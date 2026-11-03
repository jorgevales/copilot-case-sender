"""Part 2's nine account-detail pages: readers, retrieval, acceptance, the written snapshot and same-day reuse.
Synthetic pages and identifiers only (shaped after the sanitised parser reference).  Run like the other test files."""
import collections
import json
import threading
import types

import pytest

from test_pipeline import FakeControl, P, base, load, p2, page   # noqa: F401  (pytest fixtures)

ROUTE = "https://system2.example.invalid/detail{n}?id={{account}}"
HEADER = ('<div class="account-details"><h1>NAME_A</h1><dl><dt>Account Code</dt><dd>{account}</dd></dl></div>')


def html(body, account="AB1", title="Account"):
    return (f"<html><head><title>{title}</title></head><body><a>Logout</a><form id='frmmain'>"
            + (HEADER.format(account=account) if account else "") + body + "</form></body></html>")


PAGES = {
    "01 Personal Details": html(
        "<h3>Account Details</h3><table><tr><td>Account Name</td><td>NAME_A</td></tr><tr><td>Opened</td><td>01/01/1900</td></tr></table>"
        "<table><tr><th>Product</th><th>Status</th><th>Account Number</th><th>Action</th></tr>"
        "<tr><td>PRODUCT_A</td><td>STATUS_A</td><td>SUB_1</td><td><a class='btn'>Edit</a></td></tr>"
        "<tr><td>PRODUCT_B</td><td></td><td>SUB_2</td><td></td></tr>"
        "<tr style='display: none'><td>PRODUCT_HIDDEN</td><td>x</td><td>y</td><td></td></tr></table>"
        "<h3>Customer Details</h3>"
        "<div class='form-group'><label class='control-label'>Customer Type</label>"
        "<select><option>TYPE_A</option><option selected>TYPE_B</option><option>TYPE_C</option></select></div>"
        "<div class='form-group'><label class='control-label'>Reference</label><input type='text' value=''></div>"),
    "02 Contact Details": html(
        "<div class='panel'><div class='panel-heading'>Addresses</div><div class='panel-body'><div class='form-group'>"
        "<label class='control-label'>Correspondence Address</label><p class='form-control-static'>ADDRESS_LINE_A</p></div></div></div>"
        "<div class='panel'><div class='panel-heading'>Contact Information</div><div class='panel-body'>"
        "<div class='form-group'><label class='control-label'>Email</label><input type='text' value='x@example.com'></div>"
        "<div class='form-group hide'><label class='control-label'>Mobile</label><input type='text' value='HIDDEN_PHONE'></div></div></div>"
        "<div class='panel'><div class='panel-heading'>Preferred Contact Method</div><div class='panel-body'><div class='form-group'>"
        "<label class='control-label'>Method</label><select><option>Post</option><option selected>Email</option></select></div></div></div>"),
    "03 Correspondence Details": html(
        "<b>Preferences</b><div class='form-group'><label class='control-label'>Document Reporting Preference</label>"
        "<p id='documentReportingPreferenceStatic'>Email</p><select id='documentReportingPreference' class='hide'>"
        "<option>Post</option><option selected>Email</option></select></div>"
        "<p id='stateFrequencyStatic'></p><select id='statementFrequency'><option>Monthly</option><option selected>Quarterly</option></select>"
        "<p id='documentPostOptionStatic'>POST_OPTION</p>"
        "<b>Email Settings</b><table id='tblCorrespondenceSummary'><tr><th>Email</th><th>Type</th></tr>"
        "<tr><td>x@example.com</td><td>TYPE_A</td></tr></table>"
        "<b>Document Reporting Oversight Emails</b><table id='tblOversightEmails'><tr><th>Email Address</th><th>Action</th></tr>"
        "<tr><td>o1@example.com</td><td><a>Remove</a></td></tr><tr><td>o2@example.com</td><td><a>Remove</a></td></tr></table>"),
    "04 Trading Details": html(
        "<h3>Income Distribution Options</h3><div class='form-group'><label class='control-label'>Income</label>"
        "<p class='form-control-static'>OPTION_A</p></div>"
        "<div id='incomeDistributionGroup' class='hide'><div class='form-group'><label class='control-label'>To</label>x</div></div>"
        "<h3>Fees and Commissions</h3><table id='fundRebatesTable'><tr><th>Fund</th><th>Rate</th></tr>"
        "<tr><td>FUND_A</td><td>0.10</td></tr></table>"
        "<h3>Trade Settlement</h3><div class='form-group'><label class='control-label'>Settlement</label>"
        "<p class='form-control-static'>SETTLE_A</p></div>"),
    "05 Banking Details": html(
        "<h2>Banking Details</h2><table>"
        "<tr><td>BANK ACCOUNT 1</td></tr><tr><td>Account Name</td><td>HOLDER_A</td></tr>"
        "<tr><td>Sort Code</td><td><table><tr><td>00</td><td>11</td><td>22</td></tr></table></td></tr>"
        "<tr><td>Account Number</td><td>00012345</td></tr>"
        "<tr><td>Bank Ref</td><td><input type='hidden' value='ID_HIDDEN'></td></tr><tr><td>Action</td><td><a>Edit</a></td></tr>"
        "<tr><td>BANK ACCOUNT 2</td></tr><tr><td>Account Name</td><td>HOLDER_B</td></tr>"
        "<tr><td>Sort Code</td><td><table><tr><td>33</td><td>44</td><td>55</td></tr></table></td></tr></table>"),
    "06 Other Details": html(
        "<h3>Money Laundering</h3><input type='checkbox' id='launderingreg1' checked>"
        "<h4>Details of Individual</h4><table><tr><td>Name</td><td>PERSON_A</td></tr></table>"
        "<h4>Confirmation</h4><table><tr><td>Confirmed By</td><td>PERSON_B</td></tr></table><input type='checkbox' id='ML-Confirmation'>"
        "<table><tr><td>Details of Introducing Firm</td></tr><tr><td>Firm</td><td>FIRM_A</td></tr></table>"
        "<h3>Documentation</h3><table><tr><td>Document</td><td>DOC_A</td></tr></table>"
        "<h3>Appropriateness Test</h3><p>Appropriateness Test completed for Complex Direct Investments.</p>"
        "<div class='form-group'><label class='control-label'>Completed</label><input type='checkbox' checked></div>"),
    "07 Suspended Activity": html(
        "<select id='SubAccountList'><option>All</option><option selected>SUB_1</option></select>"
        "<input type='checkbox' name='Withdrawals' checked><input type='checkbox' name='Deposits'>"
        "<input type='checkbox' name='Buys' checked><input type='checkbox' name='Sells'>"),
    "08 Future Dated Instructions": html(
        "<table><tr><th>Date</th><th>Type</th><th>Amount</th><th>Action</th></tr>"
        "<tr><td>01/01/1900</td><td>TYPE_A</td><td>1.00</td><td><a>Delete</a></td></tr>"
        "<tr><td>02/01/1900</td><td></td><td>2.00</td><td></td></tr>"
        "<tr id='softCloseRow3'><td>SOFT</td><td>x</td><td>y</td><td></td></tr></table>"),
    "09 Flags and Custom Details": html(
        "<div class='panel'><div class='panel-heading'>General</div><div class='panel-body'>"
        "<div class='form-group'><label>Gone Away</label><i class='fa fa-check-circle-o'></i><div class='col-xs'>SET_A</div></div>"
        "<div class='form-group'><label>Vulnerable</label><i class='fa fa-circle-o'></i><div class='col-xs'></div></div>"
        "<div class='form-group'><label>Other</label><div class='col-xs'></div></div></div></div>"
        "<div class='panel'><div class='panel-heading'>CS Request</div><div class='panel-body'>"
        "<div class='form-group'><label>Hold</label><i class='fa fa-question'></i><div class='col-xs'></div></div></div></div>"
        "<table class='table-small-cell-padding'><tr><td>Custom A</td><td>VALUE_A</td></tr></table>"),
}


def parse(p2, name, page_html=None):
    return p2.parse_account_detail_section(name, None, p2.BeautifulSoup(page_html or PAGES[name], "html.parser"))


def soup(p2, text):
    return p2.BeautifulSoup(text, "html.parser")


@pytest.fixture
def d2(p2):
    p2.DETAIL_PAGES = {n: ROUTE.format(n=i) for i, n in enumerate(P.DETAIL_SECTIONS)}
    p2.GATE = p2.Gate(FakeControl(), "sender")
    return p2


def serve(p2, monkeypatch, pages=None, account="AB1", calls=None):
    """soup_get answering each configured detail address with its synthetic page."""
    by_url = {ROUTE.format(n=i).replace("{account}", account): (pages or PAGES)[n] for i, n in enumerate(P.DETAIL_SECTIONS)}

    def get(url, params=None):
        p2.GATE.check()
        if calls is not None:
            calls.append(url)
        return page(p2, by_url[url], url=url)
    monkeypatch.setattr(p2, "soup_get", get)


# ---------------------------------------------------------------------------------------------- shared parsing layer

def test_only_visible_text_is_read_and_a_folded_section_counts_as_visible(p2):
    s = soup(p2, "<div id='r'>A<span hidden>B</span><span class='hidden'>C</span><span class='hide'>D</span>"
                 "<span class='d-none'>E</span><span style='display : none'>F</span><span style='visibility:hidden'>G</span>"
                 "<div class='collapse'>H</div><script>I</script><style>J</style><button>K</button><i class='fa fa-x'>L</i>"
                 "<svg><text>M</text></svg><noscript>N</noscript><a href='#'>Edit</a><a class='btn btn-default'>O</a>"
                 "<!-- P --><input type='checkbox' checked><input type='hidden' value='Q'>R</div>")
    assert p2.detail_value_text(s.find(id="r")) == "A H Yes R"
    assert p2.detail_hidden(s.find(class_="d-none")) and not p2.detail_hidden(s.find(class_="collapse"))


def test_controls_give_their_current_value_never_their_option_list(p2):
    s = soup(p2, "<select id='a'><option>X</option><option selected>Y</option></select><select id='b'><option>X</option>"
                 "<option>Z</option></select><select id='c' multiple><option>X</option></select>"
                 "<input id='d' value='V'><input id='e' type='password' value='S'><input id='f' type='radio'>"
                 "<textarea id='g'> T </textarea><div id='h'><p class='form-control-static'></p><input value='U'></div>")
    v = lambda i: p2.detail_value(s.find(id=i))
    assert (v("a"), v("b"), v("c"), v("d"), v("e"), v("f"), v("g")) == ("Y", "X", "", "V", "", False, "T")
    assert v("h") == ""                                      # an explicitly empty static display is the shown value


def test_labels_repeat_into_lists_and_action_labels_are_ignored(p2):
    out = {}
    for k, v in (("Name", "A"), ("Name", "A"), ("Name", "B"), ("Edit", "x"), ("", "y"), ("Add new", "z"), ("Blank", "")):
        p2.detail_put(out, k, v)
    assert out == {"Name": ["A", "B"], "Blank": ""}


def test_field_pairs_skip_grids_nested_tables_and_hidden_groups_and_keep_blanks(p2):
    s = soup(p2, "<div id='r'><table><tr><td>Label A</td><td>VALUE_A</td><td>Label B</td><td></td></tr>"
                 "<tr><td>Outer</td><td><table><tr><td>Inner</td><td>VALUE_I</td></tr></table></td></tr></table>"
                 "<table><tr><th>Col</th><th>Col 2</th></tr><tr><td>G1</td><td>G2</td></tr></table>"
                 "<div class='form-group'><label class='control-label'>Status</label><p class='form-control-static'>SHOWN</p>"
                 "<select><option selected>CONTROL</option></select></div>"
                 "<div class='form-group' style='display:none'><label class='control-label'>Secret</label><input value='S'></div>"
                 "<dl><dt>Term</dt><dd>DEF</dd></dl></div>")
    f = p2.detail_fields(s.find(id="r"))
    assert f == {"Label A": "VALUE_A", "Label B": "", "Inner": "VALUE_I", "Status": "SHOWN", "Term": "DEF"}


def test_a_grid_keeps_order_blank_cells_and_messages_and_drops_actions_hidden_and_soft_close(p2):
    s = soup(p2, "<table><tr><th>B</th><th>A</th><th>Action</th></tr><tr><td>b1</td><td></td><td><a>Edit</a></td></tr>"
                 "<tr class='hide'><td>H</td><td>H</td><td></td></tr><tr id='SoftCloseRow'><td>S</td><td>S</td><td></td></tr>"
                 "<tr><td>b2</td><td>a2</td><td></td></tr><tr><td colspan='3'>No more records</td></tr></table>")
    g = p2.detail_grid(s.find("table"))
    assert g == {"columns": ["B", "A"], "rows": [{"B": "b1", "A": ""}, {"B": "b2", "A": "a2"}], "message": "No more records"}
    assert p2.detail_grid(None) == {"columns": [], "rows": [], "message": ""}


def test_a_named_region_stops_at_the_next_heading_and_the_root_prefers_the_main_form(p2):
    s = soup(p2, "<body><div id='headerid'>X</div><form id='frmmain'><h3>One</h3><table><tr><td>K1</td><td>V1</td></tr></table>"
                 "<legend>Two</legend><div class='form-group'><label class='control-label'>K2</label>V2</div></form></body>")
    root = p2.detail_main_root(s)
    assert root.get("id") == "frmmain"
    assert p2.detail_section_fields(root, "One") == {"K1": "V1"} and p2.detail_section_fields(root, "Two") == {"K2": "V2"}
    assert p2.detail_section_fields(root, "Three") == {}
    assert p2.detail_main_root(soup(p2, "<body><div id='headerid'></div></body>")).get("id") == "headerid"


# ---------------------------------------------------------------------------------------------- the nine readers

def test_every_page_reader_extracts_its_page(p2):
    out = {n: parse(p2, n) for n in P.DETAIL_SECTIONS}
    assert all(out[n]["completeness"]["outcome"] == "Extracted successfully" and out[n]["captureStatus"] == "Captured" for n in out)
    pd = out["01 Personal Details"]["data"]
    assert pd["Account Details"] == {"Account Name": "NAME_A", "Opened": "01/01/1900"}
    assert pd["Product Details"] == {"columns": ["Product", "Status", "Account Number"], "message": "",
                                     "rows": [{"Product": "PRODUCT_A", "Status": "STATUS_A", "Account Number": "SUB_1"},
                                              {"Product": "PRODUCT_B", "Status": "", "Account Number": "SUB_2"}]}
    assert pd["Customer Details"] == {"Customer Type": "TYPE_B", "Reference": ""}
    assert out["02 Contact Details"]["data"] == {"Addresses": {"Correspondence Address": "ADDRESS_LINE_A"},
                                                 "Contact Information": {"Email": "x@example.com"},
                                                 "Preferred Contact Method": {"Method": "Email"}}
    cd = out["03 Correspondence Details"]["data"]
    assert cd["Preferences"] == {"Document Reporting Preference": "Email", "Statement Frequency": "Quarterly"}
    assert "Post Settings" not in cd and cd["Email Settings"]["rows"] == [{"Email": "x@example.com", "Type": "TYPE_A"}]
    assert cd["Document Reporting Oversight Emails"]["rows"] == [{"Email Address": "o1@example.com"}, {"Email Address": "o2@example.com"}]
    td = out["04 Trading Details"]
    assert td["data"]["Income Distribution Options"] == {"Income": "OPTION_A"}
    assert td["data"]["Fees and Commissions"] == {"Fund-specific Rates": {"columns": ["Fund", "Rate"], "message": "",
                                                                          "rows": [{"Fund": "FUND_A", "Rate": "0.10"}]}}
    assert td["data"]["Trade Settlement"] == {"Settlement": "SETTLE_A"}
    assert td["completeness"]["optionalSectionsNotShown"] == ["Interest Distribution Options", "Distribution Transfer",
                                                              "Fund-specific Initial Charge Discount Rates"]
    assert out["07 Suspended Activity"]["data"] == {"Applies to": "SUB_1", "Payments": {"Withdrawals": True, "Deposits": False},
                                                    "Transactional": {"Buys": True, "Sells": False, "Inter-Account Transfer": None}}


def test_every_bank_account_is_its_own_object_with_composite_values_and_no_hidden_or_action_values(p2):
    b = parse(p2, "05 Banking Details")
    assert b["data"] == {"Bank Accounts": [
        {"Bank Account Label": "BANK ACCOUNT 1", "Account Name": "HOLDER_A", "Sort Code": "00-11-22", "Account Number": "00012345",
         "Bank Ref": ""},
        {"Bank Account Label": "BANK ACCOUNT 2", "Account Name": "HOLDER_B", "Sort Code": "33-44-55"}]}
    assert b["completeness"]["bankAccounts"] == 2 and "ID_HIDDEN" not in json.dumps(b)


def test_no_bank_account_is_confirmed_only_on_a_fully_loaded_page(p2):
    empty = parse(p2, "05 Banking Details", html("<h2>Banking Details</h2><p>Nothing here</p>"))
    assert empty["completeness"]["outcome"] == "Confirmed empty" and empty["data"] == {"Bank Accounts": []}
    assert empty["captureStatus"].startswith("Blank on Register")
    for page_html in (html("<p>Nothing here</p>"), html("<h2>Banking Details</h2>", account=None)):
        odd = parse(p2, "05 Banking Details", page_html)                  # no title / no account header: not proof
        assert odd["completeness"]["outcome"] == "Unexpected page layout"
        assert odd["captureStatus"].startswith("Not captured (unexpected page layout")


def test_tick_boxes_are_true_false_or_none_when_missing(p2):
    od = parse(p2, "06 Other Details")["data"]
    ml = od["Money Laundering"]
    assert ml["Identity verification advised"] is True and ml["Confirmation"] == {"Confirmed By": "PERSON_B", "Confirmed": False}
    assert ml["Details of Individual"] == {"Name": "PERSON_A"} and ml["Details of Introducing Firm"] == {"Firm": "FIRM_A"}
    assert od["Documentation"] == {"Document": "DOC_A"}
    assert od["Appropriateness Test"] == {"Completed": True, "Description": "Appropriateness Test completed for Complex Direct Investments."}
    gone = parse(p2, "06 Other Details", PAGES["06 Other Details"].replace("id='ML-Confirmation'", "id='other'"))
    assert gone["data"]["Money Laundering"]["Confirmation"]["Confirmed"] is None


def test_future_instructions_keep_every_row_several_tables_and_an_explicit_empty_message(p2):
    f = parse(p2, "08 Future Dated Instructions")
    assert f["data"]["FDT Instructions"] == {"columns": ["Date", "Type", "Amount"], "message": "",
                                             "rows": [{"Date": "01/01/1900", "Type": "TYPE_A", "Amount": "1.00"},
                                                      {"Date": "02/01/1900", "Type": "", "Amount": "2.00"}]}
    assert f["completeness"]["instructionRows"] == 2
    two = parse(p2, "08 Future Dated Instructions", html("<table><tr><th>A</th><th>X</th></tr><tr><td>1</td><td>x</td></tr></table>"
                                                         "<table><tr><th>B</th><th>Y</th></tr><tr><td>2</td><td>y</td></tr></table>"))
    assert [t["rows"] for t in two["data"]["FDT Instructions"]["tables"]] == [[{"A": "1", "X": "x"}], [{"B": "2", "Y": "y"}]]
    assert two["completeness"]["instructionRows"] == 2
    none = parse(p2, "08 Future Dated Instructions", html("<p>No FDT Instructions linked to Account.</p>"))
    assert none["completeness"]["outcome"] == "Confirmed empty"
    assert none["data"]["FDT Instructions"] == {"columns": [], "rows": [], "message": "No FDT Instructions linked to Account."}
    silent = parse(p2, "08 Future Dated Instructions", html("<table><tr><th>Date</th><th>Type</th></tr></table>"))
    assert silent["completeness"]["outcome"] == "Extraction failed"          # no rows is not proof of none
    assert parse(p2, "08 Future Dated Instructions", html("<p>Welcome</p>"))["completeness"]["outcome"] == "Unexpected page layout"


def test_flags_are_active_inactive_or_unknown_never_inferred(p2):
    f = parse(p2, "09 Flags and Custom Details")
    assert f["data"] == {"General": {"Gone Away": {"Active": True, "History": "SET_A"}, "Vulnerable": {"Active": False, "History": ""},
                                     "Other": {"Active": None, "History": ""}},
                         "CS Request": {"Hold": {"Active": None, "History": ""}}, "Custom Details": {"Custom A": "VALUE_A"}}
    assert f["completeness"]["optionalSectionsNotShown"] == ["Sub-type"]


def test_a_page_without_its_landmarks_or_values_is_never_a_success(p2):
    for name in P.DETAIL_SECTIONS:
        r = parse(p2, name, html("<p>Something else entirely</p>"))
        assert r["completeness"]["outcome"] in ("Unexpected page layout", "Extraction failed"), name
        assert r["completeness"]["outcome"] not in ("Extracted successfully", "Confirmed empty")
    blank = parse(p2, "01 Personal Details", html("<h3>Account Details</h3><h3>Customer Details</h3>"))
    assert blank["completeness"]["outcome"] == "Extraction failed" and blank["captureStatus"].startswith("Not captured")


# ---------------------------------------------------------------------------------------------- retrieval

def test_a_page_for_the_requested_account_is_read_and_marked_matched(d2, monkeypatch):
    calls = []
    serve(d2, monkeypatch, calls=calls)
    out = d2.fetch_account_section("AB1", "05 Banking Details", d2.DETAIL_PAGES["05 Banking Details"])
    assert calls == ["https://system2.example.invalid/detail4?id=AB1"]
    assert out["completeness"]["accountCheck"] == "matched" and out["completeness"]["outcome"] == "Extracted successfully"


def test_a_page_for_another_account_or_from_a_redirect_is_rejected(d2, monkeypatch):
    serve(d2, monkeypatch, pages={n: PAGES[n].replace("<dd>AB1</dd>", "<dd>AB2</dd>") for n in P.DETAIL_SECTIONS})
    with pytest.raises(d2.WrongAccount):
        d2.fetch_account_section("AB1", "01 Personal Details", d2.DETAIL_PAGES["01 Personal Details"])
    monkeypatch.setattr(d2, "soup_get", lambda url, params=None: page(d2, PAGES["01 Personal Details"],
                                                                      url="https://system2.example.invalid/home?x=1"))
    with pytest.raises(d2.WrongPage):
        d2.fetch_account_section("AB1", "01 Personal Details", d2.DETAIL_PAGES["01 Personal Details"])
    e = d2.section_failure("AB1", "01 Personal Details", d2.WrongAccount("x"))
    assert e["completeness"]["outcome"] == "Account validation failed" and e["data"] == {}


def test_all_nine_come_back_in_their_order_through_one_retry_each(d2, monkeypatch):
    monkeypatch.setattr(d2.time, "sleep", lambda s: None)
    serve(d2, monkeypatch)
    real, seen = d2.soup_get, collections.Counter()

    def flaky(url, params=None):
        seen[url] += 1
        if seen[url] == 1 and url.endswith("detail2?id=AB1"):
            raise RuntimeError("HTTP 502")
        return real(url, params)
    monkeypatch.setattr(d2, "soup_get", flaky)
    out = d2.fetch_all_account_sections("AB1")
    assert list(out) == list(P.DETAIL_SECTIONS)
    assert all(e["completeness"]["outcome"] == "Extracted successfully" for e in out.values())
    assert seen["https://system2.example.invalid/detail2?id=AB1"] == 2


def test_an_expired_sign_in_pauses_the_detail_pages_then_continues(d2, monkeypatch):
    control = d2.GATE.control
    monkeypatch.setattr(d2, "auth", lambda sender: ([{"name": "c", "value": "new"}], "UA"))
    serve(d2, monkeypatch)
    real, state = d2.soup_get, {"n": 0}

    def expiring(url, params=None):
        state["n"] += 1
        if state["n"] == 1:
            raise d2.SessionExpired("System 2 sign-in expired")
        return real(url, params)
    monkeypatch.setattr(d2, "soup_get", expiring)
    threading.Timer(0.3, lambda: setattr(control, "resumed_seq", 1)).start()
    out = d2.with_retry(d2.fetch_account_section, "AB1", "07 Suspended Activity", d2.DETAIL_PAGES["07 Suspended Activity"])
    assert control.pause_seq == 1 and out["completeness"]["outcome"] == "Extracted successfully"


def test_access_denied_twice_stops_every_detail_request(d2, monkeypatch):
    monkeypatch.setattr(d2.time, "sleep", lambda s: None)

    def denied(url, params=None):
        d2.GATE.check()
        raise d2.AccessDenied("System 2 refused access (HTTP 403)")
    monkeypatch.setattr(d2, "soup_get", denied)
    out = d2.fetch_all_account_sections("AB1")
    assert d2.GATE.control.stop_rec and "not hammered" in d2.GATE.control.stop_rec["reason"]
    assert list(out) == list(P.DETAIL_SECTIONS) and {e["completeness"]["outcome"] for e in out.values()} == {"Request failed"}


def test_a_stopped_run_requests_nothing_and_pending_pages_are_cancelled(d2, monkeypatch):
    calls = []
    serve(d2, monkeypatch, calls=calls)
    d2.GATE.control.stop("test", "stop")
    out = d2.fetch_all_account_sections("AB1")
    assert calls == [] and {e["completeness"]["outcome"] for e in out.values()} == {"Request failed"}
    gate = threading.Event()
    with d2.ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(gate.wait, 5)
        futures = d2.submit_account_sections(pool, "AB1")
        for f in futures.values():
            f.cancel()
        gate.set()
    entries = d2.collect_account_sections(futures, "AB1")
    assert blocker.result() and all(e["captureStatus"] == "Not captured (cancelled (the run was stopping))" for e in entries.values())


# ---------------------------------------------------------------------------------------------- written snapshot and reuse

def good_details(p2):
    return {n: parse(p2, n) for n in P.DETAIL_SECTIONS}


def summary(account="AB1"):
    return {"metadata": {"requestedAccount": account, "csvMatches": [], "customerAdditionalInfo": []}, "sections": {},
            "_interestedPartyLinks": []}


def test_the_snapshot_has_exactly_four_blocks_and_nine_sections_in_order(p2, base):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    assert P.Paths(base, base, base / "run").snapshot("ab1", "2026-11-03").name == "AB1_Complete_Account_Profile.json"
    p2.write_result(target, "AB1", summary(), [], [], good_details(p2))
    back = json.loads(target.read_text("utf-8"))
    assert list(back) == ["Account Summary", "Interested Parties linked to the account",
                          "Interested Party links to other account numbers", "Nine Account Detail Sections"]
    assert list(back["Nine Account Detail Sections"]) == list(P.DETAIL_SECTIONS)
    assert all(list(e)[:3] == ["captureStatus", "data", "completeness"] for e in back["Nine Account Detail Sections"].values())
    assert len(back["Nine Account Detail Sections"]["05 Banking Details"]["data"]["Bank Accounts"]) == 2
    assert not list(base.glob(".*staging*")) and "_interestedPartyLinks" not in back["Account Summary"]


def test_a_mandatory_page_not_captured_writes_nothing(p2, base):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    details = good_details(p2)
    details["05 Banking Details"] = p2.section_failure("AB1", "05 Banking Details", RuntimeError("x"))
    details["08 Future Dated Instructions"] = parse(p2, "08 Future Dated Instructions", html("<p>Welcome</p>"))
    with pytest.raises(p2.DetailsIncomplete, match="05 Banking Details, 08 Future Dated Instructions"):
        p2.write_result(target, "AB1", summary(), [], [], details)
    assert not target.exists() and not list(base.iterdir())
    assert p2.detail_tally(details) == {"extracted": 7, "confirmed_empty": 0, "not_found": 0, "failed": 2,
                                        "failed_sections": ["05 Banking Details", "08 Future Dated Instructions"]}
    ok = good_details(p2)
    ok["05 Banking Details"] = parse(p2, "05 Banking Details", html("<h2>Banking Details</h2>"))     # confirmed empty is fine
    p2.write_result(target, "AB1", summary(), [], [], ok)
    assert target.exists()


def test_a_bank_account_or_table_row_lost_while_writing_is_caught_and_never_reused(p2, base, monkeypatch):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    real = P.write_json

    def lossy(path, obj):
        copy = json.loads(json.dumps(obj))
        copy["Nine Account Detail Sections"]["05 Banking Details"]["data"]["Bank Accounts"].pop()
        copy["Nine Account Detail Sections"]["08 Future Dated Instructions"]["data"]["FDT Instructions"]["rows"].pop()
        real(path, copy)
    monkeypatch.setattr(p2.P, "write_json", lossy)
    with pytest.raises(p2.SnapshotIncomplete, match="holds 1 bank account.*but 2 were read.*1 future-dated instruction row"):
        p2.write_result(target, "AB1", summary(), [], [], good_details(p2))
    assert not target.exists() and (base / (target.stem + ".incomplete.json")).exists()


def test_only_a_complete_snapshot_of_the_same_account_is_reused(p2, base):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    assert p2.reusable(target, "AB1") == (False, "")
    p2.write_result(target, "AB1", summary(), [], [], good_details(p2))
    assert p2.reusable(target, "AB1") == (True, "")
    assert p2.reusable(target, "AB2") == (False, "it is not for the requested account")
    assert not target.exists() and (base / (target.stem + ".incomplete.json")).exists()     # set aside, fetched again
    old = {"Account Summary": {"metadata": {"requestedAccount": "AB1"}}, "Interested Parties linked to the account": [],
           "Interested Party links to other account numbers": []}                          # the older three-block snapshot
    P.write_json(target, old)
    assert p2.reusable(target, "AB1") == (False, "it does not hold exactly the four snapshot blocks")
    broken = {**old, "Nine Account Detail Sections": {n: {"captureStatus": "Captured", "data": {},
                                                          "completeness": {"outcome": "Not found"}} for n in P.DETAIL_SECTIONS}}
    P.write_json(target, broken)
    assert p2.reusable(target, "AB1") == (False, "section '01 Personal Details' has no valid capture result")


def test_the_coordinator_refuses_to_start_without_every_detail_page(p2):
    R = load("run_pipeline", "orchestrator/run_pipeline.py")
    cfg = types.SimpleNamespace(system1_url="https://system1.example.invalid/", system1_brand_text="B", guidance_dir="g",
                                system2_url="https://system2.example.invalid/", system2_party_marker="m",
                                system2_account_page="https://system2.example.invalid/a?id={account}",
                                system2_party_links_page="https://system2.example.invalid/l?p={party}",
                                system2_customer_summary_page="https://system2.example.invalid/c?id={customer}",
                                completion_mode="submission_only", result_contract_path="", systems_cdp_port=9223, cdp_port=9333,
                                system2_detail_pages={n: ROUTE.format(n=i) for i, n in enumerate(P.DETAIL_SECTIONS)})
    assert R.pipeline_problems(cfg) == []
    cfg.system2_detail_pages = {**cfg.system2_detail_pages, "05 Banking Details": "https://system2.example.invalid/b",
                                "09 Flags and Custom Details": ""}
    assert R.pipeline_problems(cfg) == [
        "System 2 account-detail page '05 Banking Details' must contain {account} where the account number goes",
        "System 2 account-detail page '09 Flags and Custom Details' is not set (run setup)"]
