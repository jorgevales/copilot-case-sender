"""Part 2's customer summary page: the customer-level Charity Commission Number in each matching CSV row.
Synthetic pages and identifiers only.  Run like the other test files."""
import collections
import json
import sys
import threading
import types

import pytest

from test_account_details import PAGES, ROUTE, summary
from test_pipeline import FakeControl, P, base, load, p2, page   # noqa: F401  (pytest fixtures)

CUSTOMER = "https://system2.example.invalid/customer?id={customer}"
DAY = "2026-11-03"


def panel(rows, heading="Additional Info:", attrs=""):
    return (f"<div class='panel panel-default'{attrs}><div class='panel-heading'>{heading}</div>"
            f"<div class='panel-body'><table>{rows}</table></div></div>")


def row(label="Charity Commission Number:", value="<label>000123</label>", attrs=""):
    return f"<tr{attrs}><td><label>{label}</label></td><td>{value}</td></tr>"


def customer_html(body):
    return f"<html><head><title>Customer</title></head><body><a>Logout</a>{body}</body></html>"


def parse(p2, body):
    return p2.parse_customer_charity_number(p2.BeautifulSoup(customer_html(body), "html.parser"))


@pytest.fixture
def c2(p2):
    p2.CUSTOMER_PAGE = CUSTOMER
    p2.GATE = p2.Gate(FakeControl(), "sender")
    return p2


def serve(p2, monkeypatch, pages, calls=None):
    """soup_get answering each customer address with its synthetic page."""
    by_url = {CUSTOMER.replace("{customer}", c): customer_html(b) for c, b in pages.items()}

    def get(url, params=None):
        p2.GATE.check()
        if calls is not None:
            calls[url] += 1
        return page(p2, by_url[url], url=url)
    monkeypatch.setattr(p2, "soup_get", get)


def match(iid, client, account="AB1"):
    return {"instruction id": iid, "Client Account Number": account, "Client Number": client,
            "Process Status": "Ended", "Sub Worktype": "TYPE_A", "End Date": "01/01/1900"}


# ---------------------------------------------------------------------------------------------- the Additional Info panel

def test_a_populated_charity_number_is_extracted_as_text(p2):
    assert parse(p2, panel(row())) == {"captureStatus": "Captured", "value": "000123", "completeness": {
        "outcome": "Extracted successfully", "panelFound": True, "fieldFound": True}}


def test_spacing_a_trailing_colon_and_case_are_ignored_in_heading_and_label(p2):
    out = parse(p2, panel(row(label="  charity   COMMISSION number :  "), heading="  ADDITIONAL   info : "))
    assert out["value"] == "000123" and out["completeness"]["outcome"] == "Extracted successfully"


def test_a_field_shown_blank_is_confirmed_empty(p2):
    assert parse(p2, panel(row(value="<label> </label>"))) == {"captureStatus": "Blank on Register", "value": "",
                                                              "completeness": {"outcome": "Confirmed empty",
                                                                               "panelFound": True, "fieldFound": True}}


def test_a_panel_without_the_field_is_confirmed_not_shown(p2):
    assert parse(p2, panel(row(label="Registered Name", value="NAME_A"))) == {
        "captureStatus": "Not available", "value": None,
        "completeness": {"outcome": "Confirmed not shown", "panelFound": True, "fieldFound": False}}


def test_no_panel_is_an_unexpected_layout_never_no_number(p2):
    unexpected = {"captureStatus": "Not captured", "value": None,
                  "completeness": {"outcome": "Unexpected page layout", "panelFound": False, "fieldFound": False}}
    assert parse(p2, "<p>Welcome</p>") == unexpected
    assert parse(p2, panel(row(), heading="Other Info")) == unexpected


def test_a_hidden_row_or_a_hidden_panel_is_ignored(p2):
    hidden_row = panel(row(value="<label>999999</label>", attrs=" style='display:none'") + row(label="Other", value="x"))
    assert parse(p2, hidden_row)["completeness"]["outcome"] == "Confirmed not shown"
    hidden_cell = panel("<tr><td><label>Charity Commission Number</label></td><td class='hide'>999999</td></tr>")
    assert parse(p2, hidden_cell)["completeness"]["outcome"] == "Confirmed empty"
    out = parse(p2, panel(row(value="999999"), attrs=" hidden") + panel(row()))
    assert out["value"] == "000123"
    only_hidden = panel(row()).replace("class='panel panel-default'", "class='panel panel-default hide'")
    assert parse(p2, only_hidden)["completeness"]["outcome"] == "Unexpected page layout"


def test_the_same_label_in_another_panel_is_ignored(p2):
    out = parse(p2, panel(row(value="999999"), heading="Company Details") + panel(row(label="Other", value="x")))
    assert out["completeness"]["outcome"] == "Confirmed not shown"
    out = parse(p2, panel(row(value="999999"), heading="Company Details") + panel(row()))
    assert out["value"] == "000123"


def test_several_tables_inside_the_panel_are_searched(p2):
    body = ("<div class='panel panel-default'><div class='panel-heading'>Additional Info</div><div class='panel-body'>"
            "<table><tr><td>Registered Name</td><td>NAME_A</td></tr></table>"
            "<table><tr><td>Charity Commission Number</td><td>000123</td></tr></table></div></div>")
    assert parse(p2, body)["value"] == "000123"


def test_the_displayed_value_wins_over_hidden_and_editable_controls(p2):
    value = ("<span class='form-control-static'>000123</span><input type='text' value='EDIT_A'>"
             "<input type='hidden' value='ID_HIDDEN'><a class='btn'>Edit</a><button>Save</button>")
    assert parse(p2, panel(row(value=value)))["value"] == "000123"
    assert parse(p2, panel(row(value="<label>000123</label><input type='text' value='EDIT_A'>")))["value"] == "000123"
    assert parse(p2, panel(row(value="<input type='text' value='EDIT_A'>")))["completeness"]["outcome"] == "Confirmed empty"
    assert parse(p2, panel(row(value="<input type='text' readonly value='000123'>")))["value"] == "000123"


def test_leading_zeros_and_non_numeric_formatting_are_kept(p2):
    assert parse(p2, panel(row(value="0001")))["value"] == "0001"
    assert parse(p2, panel(row(value="<label>AB-12 /  3</label>")))["value"] == "AB-12 / 3"


def test_a_label_value_pair_outside_a_table_is_read_only_inside_the_panel(p2):
    pair = ("<div class='panel'><div class='panel-heading'>Additional Info</div><div class='panel-body'><div class='form-group'>"
            "<label class='control-label'>Charity Commission Number</label><p class='form-control-static'>000123</p></div></div></div>")
    assert parse(p2, pair)["value"] == "000123"
    assert parse(p2, "<label>Charity Commission Number</label><p>999999</p>" + panel(row(label="Other", value="x"))
                 )["completeness"]["outcome"] == "Confirmed not shown"


def test_two_different_values_are_never_guessed_between(p2):
    out = parse(p2, panel(row() + row(value="999999")))
    assert out["captureStatus"] == "Not captured" and out["value"] is None and out["completeness"]["fieldFound"]


# ---------------------------------------------------------------------------------------------- retrieval

def test_the_page_is_requested_for_the_client_number_and_read(c2, monkeypatch):
    calls = collections.Counter()
    serve(c2, monkeypatch, {"C%2F1": panel(row())}, calls)
    assert c2.fetch_customer_additional_info("C/1")["value"] == "000123"
    assert calls == {"https://system2.example.invalid/customer?id=C%2F1": 1}


def test_an_unexpected_redirect_is_rejected_and_never_merged(c2, monkeypatch, base):
    monkeypatch.setattr(c2.time, "sleep", lambda s: None)
    monkeypatch.setattr(c2, "CAPTURES", base / "captures")
    r, s = page(c2, customer_html(panel(row())), url="https://system2.example.invalid/home?x=1")
    r.text = customer_html(panel(row()))
    monkeypatch.setattr(c2, "soup_get", lambda url, params=None: (r, s))
    with pytest.raises(c2.WrongPage):
        c2.with_retry(c2.fetch_customer_additional_info, "C1")
    try:
        c2.fetch_customer_additional_info("C1")
    except c2.WrongPage as e:
        failed = c2.customer_failure("C1", e)
    assert failed["captureStatus"] == "Not captured (System 2 redirected to another page)" and failed["value"] is None
    assert failed["completeness"]["outcome"] == "Request failed"
    copies = failed["completeness"]["pageCopies"]
    assert copies and all("C1" not in n for n in copies)                              # the page copy never names the customer
    s = summary()
    s["metadata"]["csvMatches"] = [match("i1", "C1")]
    c2.merge_customer_additional_info(s, {"C1": failed})
    assert c2.CHARITY_KEY not in s["metadata"]["csvMatches"][0]


def test_access_denied_twice_still_stops_the_whole_run(c2, monkeypatch):
    monkeypatch.setattr(c2.time, "sleep", lambda s: None)

    def denied(url, params=None):
        c2.GATE.check()
        raise c2.AccessDenied("System 2 refused access (HTTP 403)")
    monkeypatch.setattr(c2, "soup_get", denied)
    with pytest.raises(c2.AccessDenied):
        c2.with_retry(c2.fetch_customer_additional_info, "C1")
    assert "not hammered" in c2.GATE.control.stop_rec["reason"]
    with pytest.raises(c2.RunStopped):
        c2.fetch_customer_additional_info("C2")                          # nothing else is requested


def test_an_expired_sign_in_pauses_then_continues(c2, monkeypatch):
    control = c2.GATE.control
    monkeypatch.setattr(c2, "auth", lambda sender: ([{"name": "c", "value": "new"}], "UA"))
    serve(c2, monkeypatch, {"C1": panel(row())})
    real, state = c2.soup_get, {"n": 0}

    def expiring(url, params=None):
        state["n"] += 1
        if state["n"] == 1:
            raise c2.SessionExpired("System 2 sign-in expired")
        return real(url, params)
    monkeypatch.setattr(c2, "soup_get", expiring)
    threading.Timer(0.3, lambda: setattr(control, "resumed_seq", 1)).start()
    assert c2.with_retry(c2.fetch_customer_additional_info, "C1")["value"] == "000123" and control.pause_seq == 1


def test_one_request_per_client_number_however_many_rows_and_accounts(c2, monkeypatch):
    calls = collections.Counter()
    serve(c2, monkeypatch, {"C1": panel(row()), "C2": panel(row(value="000456"))}, calls)
    futures = {}
    with c2.ThreadPoolExecutor(max_workers=4) as pool:
        reused = c2.submit_customers(pool, futures, [match("i1", " C1 "), match("i2", "C1"), match("i3", "")])
        reused += c2.submit_customers(pool, futures, [match("i4", "C1", "AB2"), match("i5", "C2", "AB2")])
    assert list(futures) == ["C1", "C2"] and reused == 1
    assert sorted(calls.values()) == [1, 1]
    assert c2.collect_customers(futures)["C2"]["value"] == "000456"


# ---------------------------------------------------------------------------------------------- merge, write, read back

def results(p2, **pages):
    return {c: parse(p2, b) for c, b in pages.items()}


def test_every_row_of_a_customer_gets_its_own_value_right_after_the_client_number(p2):
    s = summary()
    rows = [match("i1", "C1"), match("i2", "C2"), match("i3", "C1"), match("i4", ""), match("i5", "C3")]
    s["metadata"]["csvMatches"] = [dict(r) for r in rows]
    blank = p2.merge_customer_additional_info(s, results(p2, C1=panel(row()), C2=panel(row(value="0002")),
                                                         C3=panel(row(label="Other", value="x"))))
    out = s["metadata"]["csvMatches"]
    assert blank == 1 and len(out) == 5
    assert list(out[0]) == ["instruction id", "Client Account Number", "Client Number", "Client Charity Commission Number",
                            "Process Status", "Sub Worktype", "End Date"]
    assert [r.get(p2.CHARITY_KEY) for r in out] == ["000123", "0002", "000123", None, None]
    assert [{k: v for k, v in r.items() if k != p2.CHARITY_KEY} for r in out] == rows       # nothing else changed
    info = s["metadata"]["customerAdditionalInfo"]
    assert [(e["Client Number"], e["completeness"]["outcome"]) for e in info] == [
        ("C1", "Extracted successfully"), ("C2", "Extracted successfully"), ("C3", "Confirmed not shown")]
    assert all(e["sourceReference"] == "configured customer summary page" and "value" not in e for e in info)
    assert p2.customer_problems(s) == []


def test_a_confirmed_blank_puts_an_empty_string_and_nothing_else_is_invented(p2):
    s = summary()
    s["metadata"]["csvMatches"] = [match("i1", "C1"), match("i2", "C2")]
    p2.merge_customer_additional_info(s, results(p2, C1=panel(row(value="")), C2="<p>Welcome</p>"))
    assert s["metadata"]["csvMatches"][0][p2.CHARITY_KEY] == "" and p2.CHARITY_KEY not in s["metadata"]["csvMatches"][1]
    assert p2.customer_problems(s) == []


def test_the_written_file_keeps_the_exact_string_and_the_row_order(p2, base):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    s = summary()
    s["metadata"]["csvMatches"] = [match("i1", "C1"), match("i2", "C1")]
    p2.merge_customer_additional_info(s, results(p2, C1=panel(row(value="<label>000123</label>"))))
    p2.write_result(target, "AB1", s, [], [], {n: p2.parse_account_detail_section(n, None, p2.BeautifulSoup(PAGES[n], "html.parser"))
                                               for n in P.DETAIL_SECTIONS})
    back = json.loads(target.read_text("utf-8"))["Account Summary"]["metadata"]["csvMatches"]
    assert [r[p2.CHARITY_KEY] for r in back] == ["000123", "000123"] and all(isinstance(r[p2.CHARITY_KEY], str) for r in back)
    assert list(back[1])[2:4] == ["Client Number", "Client Charity Commission Number"]
    assert p2.reusable(target, "AB1") == (True, "")


def test_read_back_catches_a_moved_copied_retyped_or_lost_number(p2, base, monkeypatch):
    def details():
        return {n: p2.parse_account_detail_section(n, None, p2.BeautifulSoup(PAGES[n], "html.parser")) for n in P.DETAIL_SECTIONS}

    def merged():
        s = summary()
        s["metadata"]["csvMatches"] = [match("i1", "C1"), match("i2", "C2")]
        p2.merge_customer_additional_info(s, results(p2, C1=panel(row()), C2=panel(row(label="Other", value="x"))))
        return s
    real = P.write_json
    for damage, why in ((lambda r: r[0].update({"Client Number": "C9"}), "has no customer summary page record"),
                        (lambda r: r[1].update({p2.CHARITY_KEY: "000123"}), "missing from, or added to"),
                        (lambda r: r[0].update({p2.CHARITY_KEY: 123}), "is not text"),
                        (lambda r: r[0].pop(p2.CHARITY_KEY), "missing from, or added to"),
                        (lambda r: r.append(dict(r[0])), "holds 3 CSV match row"),
                        (lambda r: r[0].update({"Process Status": "Changed"}), "a CSV match row changed")):
        def lossy(path, obj, damage=damage):
            copy = json.loads(json.dumps(obj))
            damage(copy["Account Summary"]["metadata"]["csvMatches"])
            real(path, copy)
        monkeypatch.setattr(p2.P, "write_json", lossy)
        target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
        with pytest.raises(p2.SnapshotIncomplete, match=why):
            p2.write_result(target, "AB1", merged(), [], [], details())
        assert not target.exists()
    moved = merged()
    r = moved["metadata"]["csvMatches"][0]
    moved["metadata"]["csvMatches"][0] = {p2.CHARITY_KEY: r[p2.CHARITY_KEY], **{k: v for k, v in r.items() if k != p2.CHARITY_KEY}}
    assert p2.customer_problems(moved) == ["a Charity Commission Number is not right after its Client Number"]


def test_a_same_day_snapshot_without_the_record_or_with_a_page_not_captured_is_fetched_again(p2, base):
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    details = {n: p2.parse_account_detail_section(n, None, p2.BeautifulSoup(PAGES[n], "html.parser")) for n in P.DETAIL_SECTIONS}
    s = summary()
    s["metadata"]["csvMatches"] = [match("i1", "C1")]
    p2.merge_customer_additional_info(s, {"C1": p2.customer_failure("C1", RuntimeError("HTTP 500"))})
    p2.write_result(target, "AB1", s, [], [], details)                # optional: still written
    assert p2.reusable(target, "AB1") == (False, "a customer summary page was not captured")
    assert not target.exists()                                       # set aside as .incomplete.json, fetched again
    p2.write_result(target, "AB1", summary(), [], [], details)
    obj = json.loads(target.read_text("utf-8"))
    del obj["Account Summary"]["metadata"]["customerAdditionalInfo"]    # a snapshot made before this change
    P.write_json(target, obj)
    assert p2.reusable(target, "AB1") == (False, "it has no customer summary page record (made before the Charity "
                                                 "Commission Number was read)")


def test_the_coordinator_needs_the_page_with_its_slot_on_the_system2_host(p2):
    R = load("run_pipeline", "orchestrator/run_pipeline.py")
    cfg = types.SimpleNamespace(system1_url="https://system1.example.invalid/", system1_brand_text="B", guidance_dir="g",
                                system2_url="https://system2.example.invalid/", system2_party_marker="m",
                                system2_account_page="https://system2.example.invalid/a?id={account}",
                                system2_party_links_page="https://system2.example.invalid/l?p={party}",
                                system2_customer_summary_page=CUSTOMER,
                                completion_mode="submission_only", result_contract_path="", systems_cdp_port=9223, cdp_port=9333,
                                system2_detail_pages={n: ROUTE.format(n=i) for i, n in enumerate(P.DETAIL_SECTIONS)})
    assert R.pipeline_problems(cfg) == []
    for value, problem in (("", "System 2 customer summary page address is not set"),
                           ("https://system2.example.invalid/c?id=1", "system2_customer_summary_page must contain {customer}"),
                           ("https://other.example.invalid/c?id={customer}", "must be on the same host as system2_url")):
        cfg.system2_customer_summary_page = value
        problems = R.pipeline_problems(cfg)
        assert len(problems) == 1 and problem in problems[0] and "example.invalid" not in problems[0]


# ---------------------------------------------------------------------------------------------- one Part 2 run

ACCOUNT_HTML = ("<html><head><title>Account</title></head><body><a>Logout</a><div class='account-details'><h1>NAME_A</h1>"
                "<dl><dt>Account Code</dt><dd>{account}</dd></dl></div></body></html>")


def run_part2(p2, base, monkeypatch, cases, customers):
    """Part 2's run_pipeline on synthetic pages: accounts, nine detail pages and customer pages served by address."""
    runtime = base / "runtime"
    paths = P.Paths(runtime, base / "cases", runtime / "pipeline" / "runs" / "r1").create()
    P.write_json(paths.plan, {"day": DAY, "cases": [{"needs": "download", "account": r["Client Account Number"], "row": r,
                                                    "row_number": n} for n, r in enumerate(cases, 2)]})
    P.Control(paths).go()
    cfg = types.SimpleNamespace(runtime_root=str(runtime), cases_root=str(base / "cases"), systems_cdp_port=9223,
                                system2_url="https://system2.example.invalid/home", system2_party_marker="partyprofile",
                                system2_account_page="https://system2.example.invalid/account?id={account}",
                                system2_party_links_page="https://system2.example.invalid/links?party={party}",
                                system2_customer_summary_page=CUSTOMER,
                                system2_detail_pages={n: ROUTE.format(n=i) for i, n in enumerate(P.DETAIL_SECTIONS)})
    monkeypatch.setitem(sys.modules, "copilot_case_sender", types.SimpleNamespace(
        load_config=lambda: cfg, validate_config=lambda c: (None, []), bootstrap_storage=lambda c, policy: None))
    monkeypatch.setattr(p2, "load_libs", lambda: None)
    monkeypatch.setattr(p2, "auth", lambda sender: ([], "UA"))
    monkeypatch.setattr(p2.time, "sleep", lambda s: None)
    calls = collections.Counter()
    by_url = {}
    for a in {r["Client Account Number"] for r in cases}:
        by_url[f"https://system2.example.invalid/account?id={a}"] = ACCOUNT_HTML.format(account=a)
        for i, n in enumerate(P.DETAIL_SECTIONS):
            by_url[ROUTE.format(n=i).replace("{account}", a)] = PAGES[n].replace("<dd>AB1</dd>", f"<dd>{a}</dd>")
    for c, body in customers.items():
        by_url[CUSTOMER.replace("{customer}", c)] = body if body is None else customer_html(body)

    def get(url, params=None):
        p2.GATE.check()
        calls[url] += 1
        if by_url[url] is None:
            raise RuntimeError("HTTP 500")
        return page(p2, by_url[url], url=url)
    monkeypatch.setattr(p2, "soup_get", get)
    code = p2.run_pipeline(paths.run)
    snap = {a: json.loads(paths.snapshot(a, DAY).read_text("utf-8")) for a in {r["Client Account Number"] for r in cases}
            if paths.snapshot(a, DAY).exists()}
    diag = json.loads((paths.diagnostics("part2") / "accounts.json").read_text("utf-8"))
    events = (paths.sub("events") / "part2.jsonl").read_text("utf-8")
    return code, snap, calls, diag, events, paths


def test_a_run_merges_each_customer_once_and_keeps_only_counts_in_the_diagnostics(p2, base, monkeypatch):
    cases = [match("i1", "C1", "AB1"), match("i2", "C1", "AB1"), match("i3", "C1", "AB2"), match("i4", "C2", "AB2")]
    code, snap, calls, diag, events, _ = run_part2(p2, base, monkeypatch, cases,
                                                   {"C1": panel(row(value="000123")), "C2": panel(row(label="Other", value="x"))})
    assert code == 0 and set(snap) == {"AB1", "AB2"}
    assert calls[CUSTOMER.replace("{customer}", "C1")] == 1 and calls[CUSTOMER.replace("{customer}", "C2")] == 1
    rows = {a: snap[a]["Account Summary"]["metadata"]["csvMatches"] for a in snap}
    assert [r.get(p2.CHARITY_KEY) for r in rows["AB1"]] == ["000123", "000123"]
    assert [r.get(p2.CHARITY_KEY) for r in rows["AB2"]] == ["000123", None]
    assert list(snap["AB1"]) == list(p2.BLOCKS) and list(snap["AB1"]["Nine Account Detail Sections"]) == list(P.DETAIL_SECTIONS)
    assert diag["customer_summary_pages"] == {"customer_summary_pages_requested": 2, "customer_summary_pages_reused": 1,
                                              "charity_numbers_extracted": 1, "charity_numbers_confirmed_blank": 0,
                                              "charity_number_fields_not_shown": 1, "customer_summary_pages_failed": 0}
    text = json.dumps(diag) + events
    assert "000123" not in text and "C1" not in text and "C2" not in text and "customer?id" not in text


def test_a_run_saves_the_snapshot_when_a_customer_page_fails_and_says_so(p2, base, monkeypatch):
    code, snap, calls, diag, events, paths = run_part2(p2, base, monkeypatch, [match("i1", "C1", "AB1")], {"C1": None})
    assert code == 0 and "AB1" in snap                                       # optional: the case is not held
    assert calls[CUSTOMER.replace("{customer}", "C1")] == 2                  # one retry, like every other page
    meta = snap["AB1"]["Account Summary"]["metadata"]
    assert p2.CHARITY_KEY not in meta["csvMatches"][0]
    assert meta["customerAdditionalInfo"][0]["captureStatus"] == "Not captured (RuntimeError)"
    assert "customer summary page(s) not captured - snapshot saved without their Charity Commission Number" in events
    assert diag["accounts"]["AB1"]["customer_summary"]["customer_summary_pages_failed"] == 1
    assert p2.reusable(paths.snapshot("AB1", DAY), "AB1") == (False, "a customer summary page was not captured")
