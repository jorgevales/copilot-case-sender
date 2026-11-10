"""The System 2 (Register) snapshot of a synthetic case, made by Part 2's OWN code offline.

Synthetic Register pages (account summary, Interested Party profiles and links, the nine account-detail pages, the
customer summary page) are served to Part 2's readers through a stand-in for `soup_get`; the snapshot is then written
by Part 2's `write_result`, so it is exactly the shape (and passes exactly the checks) of a real run.  Retrieval times
are fixed, so the file is the same on every run.  Needs beautifulsoup4 (synthetic venv); no network, no browser.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import html
import importlib.util
import io
import sys
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
BASE = "https://system2.example.invalid"
ROUTE = BASE + "/detail{n}?id={{account}}"
FIXED = _dt.datetime(2026, 11, 6, 15, 0, 0, tzinfo=_dt.timezone.utc)
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 24).decode()
NOTE = "SYNTHETIC TEST DATA"


class _FixedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz else FIXED.replace(tzinfo=None)


_P2 = None


def part2():
    """Part 2's module, loaded once, wired to the synthetic pages (never to a real System 2)."""
    global _P2
    if _P2 is not None:
        return _P2
    import bs4
    for p in (PROJECT, PROJECT / "3_copilot_sender"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location("system2_snapshots_synthetic", PROJECT / "2_system2_snapshots" / "system2_snapshots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BeautifulSoup, mod.Tag, mod.NavigableString = bs4.BeautifulSoup, bs4.Tag, bs4.NavigableString
    from shared import pipeline as P
    mod.configure(types.SimpleNamespace(
        system2_url=BASE + "/home", systems_cdp_port=9223, system2_account_page=BASE + "/account?id={account}",
        system2_party_marker="partyprofile", system2_party_links_page=BASE + "/links?party={party}",
        system2_customer_summary_page=BASE + "/customer?id={customer}",
        system2_detail_pages={n: ROUTE.format(n=i) for i, n in enumerate(P.DETAIL_SECTIONS)}))
    mod.GATE = types.SimpleNamespace(check=lambda: None)
    mod.datetime = _FixedDatetime
    mod.P = P
    _P2 = mod
    return mod


# ---------------------------------------------------------------------------------------------- page builders

def e(v) -> str:
    return html.escape(str(v), quote=True)


def page(body, account=None, name="", title="Account"):
    head = (f"<div class='account-details'><h1>{e(name)}</h1><dl><dt>Account Code</dt><dd>{e(account)}</dd></dl></div>"
            if account else "")
    return (f"<html><head><title>{title}</title></head><body><a>Logout</a><p class='note'>{NOTE}</p>"
            f"<form id='frmmain'>{head}{body}</form></body></html>")


def full_name(p):
    return " ".join(x for x in (p["title"], p["first"], p.get("middle", ""), p["last"]) if x)


def tick(on):
    return "<input type='checkbox' checked> Yes" if on else "<input type='checkbox'> No"


def account_html(r):
    rows = "".join(
        f"<tr><td><a href='/x/partyprofile?InterestedPartyid={e(p['id'])}'>{e(full_name(p))}</a></td>"
        f"<td>{e(p['role'])}</td><td>{tick(p.get('signatory'))}</td><td>{tick(p.get('correspondent'))}</td><td>Active</td></tr>"
        for p in r["parties"])
    return (f"<html><head><title>Account</title></head><body><a>Logout</a><p class='note'>{NOTE}</p>"
            f"<div class='account-details'><h1>{e(r['name'])}</h1><dl><dt>Account Code</dt><dd>{e(r['account'])}</dd>"
            f"<dt>Client Number</dt><dd>{e(r['client'])}</dd><dt>Status</dt><dd>Open</dd></dl></div>"
            "<div id='headerid'>"
            "<div class='panel panel-default'><div class='panel-heading'>Account Details</div><div class='panel-body'><dl>"
            f"<dt>Account Name</dt><dd>{e(r['name'])}</dd><dt>Fund</dt><dd>{e(r['fund'])}</dd>"
            f"<dt>Opened</dt><dd>{e(r['opened'])}</dd></dl></div></div>"
            "<div class='panel panel-default'><div class='panel-heading'>Interested Parties</div><div class='panel-body'>"
            "<table><tr><th>Full Name</th><th>Role</th><th>Signatory</th><th>Correspondent</th><th>Status</th></tr>"
            f"{rows}</table></div></div></div></body></html>")


def _js(fn, table, entry, records):
    """A profile setup function as the real page writes it (values are plain synthetic text: no quotes)."""
    calls = "".join("    LastId++;\n    row = template({\n        Id: ++LastId,\n"
                    + "".join(f'        {k}: "{v}",\n' for k, v in rec.items())
                    + f'        {entry}: "ID"\n    }});\n' + f"    $('#{table} tr:last').after(row);\n" for rec in records)
    return f"function {fn}() {{\n    var row;\n    var template = tmpl(\"T\");\n{calls}}}\n"


def party_html(p):
    lines = (list(p.get("address", [])) + ["", "", "", "", "", ""])[:6]
    script = ("var LastId = 0;\n"
              + _js("AddExistingCustomerInterestedPartyEmails", "EmailContacts", "EmailEntry",
                    [{"EmailType": "Personal", "EmailAddress": p["email"]}] if p.get("email") else [])
              + _js("AddExistingCustomerInterestedPartyPhoneNumbers", "PhoneContacts", "PhoneEntry",
                    [{"PhoneType": t, "PhoneCountry": "44", "PhoneAreaCode": "", "PhoneNumber": n}
                     for t, n in (("Mobile", p.get("mobile")), ("Daytime", p.get("phone"))) if n])
              + _js("AddExistingCustomerInterestedPartyAddresses", "addressList", "AddressEntry",
                    [{"AddressType": "Home", "AddressCountry": "United Kingdom",
                      **{f"AddressLine{i + 1}": v for i, v in enumerate(lines)}, "Postcode": p.get("postcode", "")}]
                    if p.get("address") else []))
    sel = lambda opts, chosen: "".join(f"<option{' selected' if o == chosen else ''}>{e(o)}</option>" for o in opts)
    comp = lambda code, name: (f"<h3>{name}</h3><div class='form-group'><label class='control-label'>{name} Check Status</label>"
                               f"<select id='entryForm{code}CheckStatus'>{sel(['Not started', 'Complete'], 'Complete')}</select></div>"
                               f"<div class='form-group'><label class='control-label'>{name} Check Result</label>"
                               f"<select id='entryForm{code}CheckResult'>{sel(['', 'Pass', 'Refer'], 'Pass')}</select></div>"
                               "<div class='form-group'><label class='control-label'>Financial Sanctions Check Comment</label>"
                               f"<textarea id='entryForm{code}CheckComment'></textarea></div>")
    head = "".join(f"<td class='TableHead'>{h}</td>" for h in ["Address Type", "Country"] + [f"Address Line {n}" for n in range(1, 7)]
                   + ["Postcode", "Edit"])
    return ("<html><head><title>Interested Party</title></head><body><a>Logout</a>"
            f"<p class='note'>{NOTE}</p><div id='interestPartyContainer'><h3>Personal Details</h3>"
            f"<div class='form-group'><label class='control-label'>Title</label><select id='entryFormTitle'>"
            f"{sel(['', 'MR', 'MRS', 'MS', 'MISS', 'DR', 'REV'], p['title'])}</select></div>"
            f"<div class='form-group'><label class='control-label'>First Name</label><input id='entryFormFirstName' type='text' value='{e(p['first'])}'></div>"
            f"<div class='form-group'><label class='control-label'>Middle Name</label><input id='entryFormMiddleName' type='text' value='{e(p.get('middle', ''))}'></div>"
            f"<div class='form-group'><label class='control-label'>Surname</label><input id='entryFormSurname' type='text' value='{e(p['last'])}'></div>"
            f"<div class='form-group'><label class='control-label'>Date of Birth</label><input id='entryFormDateOfBirth' type='text' value='{e(p['dob'])}' readonly></div>"
            f"<div class='form-group'><label class='control-label'>Position</label><input id='entryFormPosition' type='text' value='{e(p.get('position', ''))}'></div>"
            "<h3>Email</h3><table id='EmailContacts'><tbody><tr><th>Email Type</th><th>Email Address</th><th>Action</th></tr>"
            "<tr id='NoEmail'><td colspan='3'>Click Add New to add an email address</td></tr></tbody></table>"
            "<h3>Phone numbers</h3><table id='PhoneContacts'><tbody><tr><th>Phone Type</th><th>Country</th><th>Area Code</th>"
            "<th>Number</th><th>Action</th></tr><tr id='NoPhone'><td colspan='5'>No phone numbers</td></tr></tbody></table>"
            f"<h3>Address</h3><table id='addressList'><tbody><tr>{head}</tr><tr id='NoAddress'><td colspan='4'>No addresses</td></tr></tbody></table>"
            + comp("AML", "AML") + comp("PEP", "PEP") + comp("FS", "Financial Sanctions")
            + f"<h3>Signature</h3><img id='imgSignature' src='{PNG}'></div>"
            f"<script>{script}</script></body></html>")


def links_html(p, r):
    return ("<html><head><title>Interested Party Links</title></head><body><a>Logout</a>"
            "<table id='CustomerInterestedParties'><tr><th>Customer</th><th>Role</th><th>Type</th><th>Since</th><th>Status</th></tr>"
            f"<tr><td>{e(r['name'])} ({e(r['client'])})</td><td>{e(p['role'])}</td><td>Charity</td><td>{e(r['opened'])}</td><td>Active</td></tr></table>"
            "<table id='AccountInterestedParties'><tr><th>Account</th><th>Role</th><th>Since</th><th>Status</th></tr>"
            f"<tr><td>{e(r['name'])} ({e(r['account'])})</td><td>{e(p['role'])}</td><td>{e(r['opened'])}</td><td>Active</td></tr></table>"
            "</body></html>")


def static(label, value):
    return (f"<div class='form-group'><label class='control-label'>{e(label)}</label>"
            f"<p class='form-control-static'>{e(value)}</p></div>")


def detail_pages(r):
    a, n = r["account"], r["name"]
    addr = ", ".join(r["address"])
    bank = r.get("bank")
    sort = (bank or {}).get("sort", "--").split("-")
    banking = ("<h2>Banking Details</h2><table>"
               + (f"<tr><td>BANK ACCOUNT 1</td></tr><tr><td>Account Name</td><td>{e(bank['account_name'])}</td></tr>"
                  f"<tr><td>Bank Name</td><td>{e(bank['bank'])}</td></tr>"
                  f"<tr><td>Sort Code</td><td><table><tr>{''.join(f'<td>{e(x)}</td>' for x in sort)}</tr></table></td></tr>"
                  f"<tr><td>Account Number</td><td>{e(bank['number'])}</td></tr><tr><td>Action</td><td><a>Edit</a></td></tr>"
                  if bank else "") + "</table>")
    custom = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in r["custom"].items())
    return {
        "01 Personal Details": page(
            f"<h3>Account Details</h3><table><tr><td>Account Name</td><td>{e(n)}</td></tr><tr><td>Opened</td><td>{e(r['opened'])}</td></tr>"
            f"<tr><td>Charity Commission Number</td><td>{e(r['charity'])}</td></tr></table>"
            "<table><tr><th>Product</th><th>Status</th><th>Account Number</th><th>Action</th></tr>"
            f"<tr><td>{e(r['fund'])}</td><td>Open</td><td>{e(a)}</td><td><a class='btn'>Edit</a></td></tr></table>"
            "<h3>Customer Details</h3><div class='form-group'><label class='control-label'>Customer Type</label>"
            "<select><option>Individual</option><option selected>Charity</option></select></div>", a, n),
        "02 Contact Details": page(
            "<div class='panel'><div class='panel-heading'>Addresses</div><div class='panel-body'>"
            + static("Correspondence Address", addr) + static("Correspondence Postcode", r["postcode"])
            + static("Address Effective From", r["effective"])
            + static("Office Address", addr) + static("Office Postcode", r["postcode"]) + "</div></div>"
            "<div class='panel'><div class='panel-heading'>Contact Information</div><div class='panel-body'>"
            f"<div class='form-group'><label class='control-label'>Email</label><input type='text' value='{e(r['email'])}'></div>"
            f"<div class='form-group'><label class='control-label'>Telephone</label><input type='text' value='{e(r['phone'])}'></div></div></div>"
            "<div class='panel'><div class='panel-heading'>Preferred Contact Method</div><div class='panel-body'><div class='form-group'>"
            "<label class='control-label'>Method</label><select><option selected>Post</option><option>Email</option></select></div></div></div>", a, n),
        "03 Correspondence Details": page(
            "<b>Preferences</b><div class='form-group'><label class='control-label'>Document Reporting Preference</label>"
            "<p id='documentReportingPreferenceStatic'>Post</p></div>"
            "<p id='stateFrequencyStatic'>Quarterly</p>"
            f"<b>Post Settings</b><p id='documentPostOptionStatic'>Correspondence address: {e(addr)} {e(r['postcode'])}</p>"
            "<b>Document Reporting Oversight Emails</b><table id='tblOversightEmails'><tr><th>Email Address</th><th>Action</th></tr>"
            f"<tr><td>{e(r['email'])}</td><td><a>Remove</a></td></tr></table>", a, n),
        "04 Trading Details": page(
            "<h3>Income Distribution Options</h3>" + static("Income", r["income"])
            + "<h3>Trade Settlement</h3>" + static("Settlement", "Standard"), a, n),
        "05 Banking Details": page(banking, a, n),
        "06 Other Details": page(
            "<h3>Money Laundering</h3><input type='checkbox' id='launderingreg1' checked>"
            f"<h4>Details of Individual</h4><table><tr><td>Name</td><td>{e(full_name(r['parties'][0]))}</td></tr></table>"
            "<h4>Confirmation</h4><table><tr><td>Confirmed By</td><td>Processor PR-17</td></tr></table><input type='checkbox' id='ML-Confirmation' checked>"
            "<h3>Documentation</h3><table><tr><td>Document</td><td>Governing document on file</td></tr></table>", a, n),
        "07 Suspended Activity": page(
            "<select id='SubAccountList'><option selected>All</option></select>"
            "<input type='checkbox' name='Withdrawals'><input type='checkbox' name='Deposits'>"
            "<input type='checkbox' name='Buys'><input type='checkbox' name='Sells'>", a, n),
        "08 Future Dated Instructions": page("<p>No FDT Instructions linked to Account.</p>", a, n),
        "09 Flags and Custom Details": page(
            "<div class='panel'><div class='panel-heading'>General</div><div class='panel-body'>"
            "<div class='form-group'><label>Gone Away</label><i class='fa fa-circle-o'></i><div class='col-xs'></div></div>"
            "<div class='form-group'><label>Vulnerable</label><i class='fa fa-circle-o'></i><div class='col-xs'></div></div></div></div>"
            f"<table class='table-small-cell-padding'>{custom}</table>", a, n),
    }


def customer_html(r):
    return ("<html><head><title>Customer</title></head><body><a>Logout</a>"
            "<div class='panel panel-default'><div class='panel-heading'>Additional Info:</div><div class='panel-body'><table>"
            f"<tr><td><label>Charity Commission Number:</label></td><td><label>{e(r['charity'])}</label></td></tr>"
            "</table></div></div></body></html>")


# ---------------------------------------------------------------------------------------------- the snapshot

def write_snapshot(target: Path, register: dict, csv_row: dict) -> Path:
    """Write `target` (<ACCOUNT>_Complete_Account_Profile.json) through Part 2's readers and write_result."""
    p2 = part2()
    P = p2.P
    a = register["account"]
    pages = {f"{BASE}/account?id={a}": account_html(register), f"{BASE}/customer?id={register['client']}": customer_html(register)}
    for p in register["parties"]:
        pages[f"{BASE}/x/partyprofile?InterestedPartyid={p['id']}"] = party_html(p)
        pages[f"{BASE}/links?party={p['id']}"] = links_html(p, register)
    for i, (name, body) in enumerate(detail_pages(register).items()):
        pages[ROUTE.format(n=i).replace("{account}", a)] = body
    assert list(detail_pages(register)) == list(P.DETAIL_SECTIONS)

    def soup_get(url, params=None):
        if url not in pages:
            raise RuntimeError(f"synthetic Register has no page {url}")
        return types.SimpleNamespace(status_code=200, url=url), p2.BeautifulSoup(pages[url], "html.parser")

    p2.soup_get = soup_get
    data = p2.fetch_account(a, [{**csv_row, "_account": a}])
    p2.merge_customer_additional_info(data, {register["client"]: p2.fetch_customer_additional_info(register["client"])})
    pr, lr = {}, {}
    for d in data["_interestedPartyLinks"]:
        pr[d["url"]] = p2.fetch_party(d)
        lr[p2.link_url(d)] = p2.fetch_links(d)
    parties, links, missing = p2.assemble(data, pr, {}, lr, {})
    assert not missing, "a synthetic party page was not read"
    details = {n: p2.fetch_account_section(a, n, ROUTE.format(n=i)) for i, n in enumerate(P.DETAIL_SECTIONS)}
    bad = [n for n, x in details.items() if x["completeness"]["outcome"] not in p2.ACCEPTED]
    assert not bad, f"synthetic detail page(s) not readable by Part 2: {bad}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.redirect_stdout(io.StringIO()):
        p2.write_result(target, a, data, parties, links, details)
    return target
