"""Builds 3_azure_reviewer/form_maps.json: the fields of the 7 CCLA static-detail forms a reviewer compares with
Register (laptop tool; run with the synthetic venv's python, -I).

    tests/azure_synthetic/.synthetic-venv/Scripts/python.exe -I tests/azure_synthetic/build_form_maps.py

The field list below is written by hand from the forms (widget names read with pymupdf).  This script checks every
widget exists on the stated page, adds the signature boxes (no widget: placed under the printed word 'Signature') and
writes the JSON in a fixed order, so the file is the same every time.  Needs the downloaded forms (download.py).
The engine reads id / label / section / page / kind / key; `widget`, `widget_index` (n-th widget of that name on that
page) and `rect` are for the generator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
OUT = PROJECT / "3_azure_reviewer" / "form_maps.json"
FORMS_DIR = HERE / "_downloads" / "ccla"

# A person block: (id suffix, label, widget offset key, kind, key?)  - offsets resolved per form below.
PERSON = (("title", "Title", "text", False), ("forename", "Forename", "text", True), ("surname", "Surname", "text", True),
          ("date_of_birth", "Date of birth (dd/mm/yyyy)", "date", True), ("position", "Position", "text", False),
          ("email", "Email address", "text", False), ("home_address", "Home address", "text", False),
          ("postcode", "Postcode", "text", False))


def person(prefix, title, section, page, widgets, sig_page, sig_n, date_widget=None, date_page=None):
    """A trustee / signatory block: widgets = names for PERSON in order (None = not on this form)."""
    out = []
    for (sid, label, kind, key), w in zip(PERSON, widgets):
        if w:
            out.append({"id": f"{prefix}_{sid}", "label": f"{title} - {label}", "section": section, "page": page,
                        "widget": w, "kind": kind, "key": key})
    out.append({"id": f"{prefix}_signature", "label": f"{title} - Signature", "section": section, "page": sig_page,
                "signature_n": sig_n, "kind": "signature", "key": True})
    if date_widget:
        out.append({"id": f"{prefix}_signed_date", "label": f"{title} - Date signed (dd/mm/yyyy)", "section": section,
                    "page": date_page or sig_page, "widget": date_widget, "kind": "date", "key": False})
    return out


def f(id_, label, section, page, widget, kind="text", key=False, index=None):
    d = {"id": id_, "label": label, "section": section, "page": page, "widget": widget, "kind": kind, "key": key}
    if index is not None:
        d["widget_index"] = index
    return d


T1, T2 = "First trustee/executive director", "Second trustee/executive director"

FORMS = {
    "change-address-form": {
        "title": "Change of address form",
        "match": ["Use this form to change the address", "of an existing authorised correspondent", "Change of address form"],
        "fields": [
            f("account_name", "Account name", "Section 1 About your organisation", 1, "Text Field 1.1", key=True),
            f("account_number_1", "Account number(s) to which this change applies (1)", "Section 1 About your organisation", 1, "Text Field 1.2", key=True),
            f("account_number_2", "Account number(s) to which this change applies (2)", "Section 1 About your organisation", 1, "Text Field 1.3"),
            f("old_address", "Old address", "Section 1 About your organisation", 1, "Text Field 1.8"),
            f("old_postcode", "Old address - Postcode", "Section 1 About your organisation", 1, "Text Field 1.9"),
            f("new_address", "New address", "Section 1 About your organisation", 1, "Text Field 1.10", key=True),
            f("new_postcode", "New address - Postcode", "Section 1 About your organisation", 1, "Text Field 1.11", key=True),
            f("effective_date", "With effect from (dd/mm/yyyy)", "Section 1 About your organisation", 1, "Text Field 1.12", "date"),
            f("email", "Email address", "Section 1 About your organisation", 2, "Text Field 1.13"),
            *person("trustee_1", T1, "Section 2 Trustees' authorisation", 2,
                    ["Text Field 2-1", "Text Field 2-2", "Text Field 2-4", "Text Field 2-5", "Text Field 2-6",
                     "Text Field 2-9", "Text Field 2-10", "Text Field 2-11"], 2, 0, "Text Field 2-14"),
            *person("trustee_2", T2, "Section 2 Trustees' authorisation", 3,
                    ["Text Field 2-16", "Text Field 2-17", "Text Field 2-19", "Text Field 2-20", "Text Field 2-21",
                     "Text Field 2-24", "Text Field 2-25", "Text Field 2-26"], 3, 0, "Text Field 2-29"),
        ]},
    "email-instruction-authority-form": {
        "title": "Email instructions authority form",
        "match": ["Authority to accept email instructions", "Use this form to authorise CCLA", "Email instructions authority form"],
        "fields": [
            f("client_name", "Client name", "Authority to accept email instructions", 1, "Text Field 1", key=True),
            f("client_id_numbers", "Client ID number(s)", "Authority to accept email instructions", 1, "Text Field 2", key=True),
            {"id": "signatory_1_signature", "label": "First signatory - Signature", "section": "Authorisation", "page": 1,
             "signature_n": 0, "kind": "signature", "key": True},
            f("signatory_1_name", "First signatory - Print name", "Authorisation", 1, "Text Field 3", key=True),
            f("signatory_1_phone", "First signatory - Daytime telephone number", "Authorisation", 1, "Text Field 4"),
            {"id": "signatory_2_signature", "label": "Second signatory - Signature", "section": "Authorisation", "page": 1,
             "signature_n": 1, "kind": "signature", "key": True},
            f("signatory_2_name", "Second signatory - Print name", "Authorisation", 1, "Text Field 5", key=True),
            f("signatory_2_phone", "Second signatory - Daytime telephone number", "Authorisation", 1, "Text Field 6"),
            f("signed_date", "Date (dd/mm/yyyy)", "Authorisation", 1, "Text Field 7", "date"),
        ]},
    "change-organisation-name": {
        "title": "Change of account name form",
        "match": ["Previous client name", "New client name", "Change of account name form"],
        "fields": [
            f("previous_client_name", "Previous client name", "Section 1", 1, "Text Field 1-1", key=True),
            f("client_id_numbers", "Client ID number(s)", "Section 1", 1, "Text Field 1-2", key=True),
            f("new_client_name", "New client name", "Section 1", 1, "Text Field 1-3", key=True),
            f("old_charity_reference", "Old charity reference", "Section 1", 1, "Text Field 1-4"),
            f("new_charity_reference", "New charity reference", "Section 1", 1, "Text Field 1-5", key=True),
            *person("trustee_1", T1, "Section 2 Trustees' authorisation", 2,
                    ["Text Field 2-1", "Text Field 2-2", "Text Field 2-4", "Text Field 2-5", "Text Field 2-6",
                     "Text Field 2-9", "Text Field 2-10", "Text Field 2-11"], 2, 0, "Text Field 2-14"),
            *person("trustee_2", T2, "Section 2 Trustees' authorisation", 2,
                    ["Text Field 2-15", "Text Field 2-16", "Text Field 2-18", "Text Field 2-19", "Text Field 2-20",
                     "Text Field 2-23", None, None], 3, 0, "Text Field 2-28", 3),
        ]},
    "change-signatory-form": {
        "title": "Change of signatory form",
        "match": ["Removal of authorised signatories", "Accounts must be operated by at least two authorised signatories", "Change of signatory form"],
        "fields": [
            f("todays_date", "Today's date (dd/mm/yyyy)", "Section 1 About your organisation", 1, "Text Field 1-1", "date"),
            f("organisation_name", "Your organisation's name", "Section 1 About your organisation", 1, "Text Field 1-2", key=True),
            f("account_number_1", "CCLA account number(s) (1)", "Section 1 About your organisation", 1, "Text Field 1-3", key=True),
            f("account_number_2", "CCLA account number(s) (2)", "Section 1 About your organisation", 1, "Text Field 1-4"),
            f("removed_1_title", "Signatory to remove 1 - Title", "Section 2 Removal of authorised signatories", 1, "Text Field 2-1"),
            f("removed_1_forename", "Signatory to remove 1 - Forename", "Section 2 Removal of authorised signatories", 1, "Text Field 2-2", key=True),
            f("removed_1_surname", "Signatory to remove 1 - Surname", "Section 2 Removal of authorised signatories", 1, "Text Field 2-4", key=True),
            *person("new_signatory_1", "First new signatory", "Section 3 Addition of authorised signatories", 2,
                    ["Text Field 3-1", "Text Field 3-2", "Text Field 3-4", "Text Field 3-5", "Text Field 3-6",
                     "Text Field 3-9", "Text Field 3-10", "Text Field 3-11"], 2, 0, "Text Field 3-14"),
            *person("trustee_1", T1, "Section 4 Trustees' authorisation", 4,
                    ["Text Field 4-1", "Text Field 4-2", "Text Field 4-4", "Text Field 4-5", "Text Field 4-6",
                     "Text Field 4-9", "Text Field 4-11", "Text Field 4-12"], 4, 1, "Text Field 6-7"),
            *person("trustee_2", T2, "Section 4 Trustees' authorisation", 5,
                    ["Text Field 4-17", "Text Field 4-18", "Text Field 4-20", "Text Field 4-21", "Text Field 4-22",
                     "Text Field 4-25", "Text Field 4-27", "Text Field 4-28"], 5, 0, "Text Field 4-31"),
        ]},
    "change-correspondent-form": {
        "title": "Change of correspondent form",
        "match": ["Address for correspondence", "Change of correspondent form"],
        "fields": [
            f("todays_date", "Today's date (dd/mm/yyyy)", "Section 1 About your organisation", 1, "Text Field 1-1", "date"),
            f("organisation_name", "Your organisation's name", "Section 1 About your organisation", 1, "Text Field 1-2", key=True),
            f("account_number_1", "CCLA account number(s) (1)", "Section 1 About your organisation", 1, "Text Field 1-3", key=True),
            f("account_number_2", "CCLA account number(s) (2)", "Section 1 About your organisation", 1, "Text Field 1-4"),
            f("new_correspondent_title", "New correspondent - Title", "Section 2 New correspondent", 2, "Text Field 2-4"),
            f("new_correspondent_forename", "New correspondent - Forename", "Section 2 New correspondent", 2, "Text Field 2-5", key=True),
            f("new_correspondent_surname", "New correspondent - Surname", "Section 2 New correspondent", 2, "Text Field 2-7", key=True),
            f("new_correspondent_date_of_birth", "New correspondent - Date of birth (dd/mm/yyyy)", "Section 2 New correspondent", 2, "Text Field 2-8", "date", key=True),
            f("new_correspondent_position", "New correspondent - Position", "Section 2 New correspondent", 2, "Text Field 2-9"),
            f("new_correspondent_mobile", "New correspondent - Mobile number", "Section 2 New correspondent", 2, "Text Field 2-10"),
            f("new_correspondent_email", "New correspondent - Email address", "Section 2 New correspondent", 2, "Text Field 2-12", key=True),
            f("new_correspondent_home_address", "New correspondent - Home address", "Section 2 New correspondent", 2, "Text Field 2-13"),
            f("new_correspondent_postcode", "New correspondent - Postcode", "Section 2 New correspondent", 2, "Text Field 2-14"),
            f("correspondence_address", "Address for correspondence", "Section 2 New correspondent", 2, "Text Field 2-16", key=True),
            f("correspondence_postcode", "Address for correspondence - Postcode", "Section 2 New correspondent", 2, "Text Field 2-17", key=True),
            {"id": "new_correspondent_signature", "label": "New correspondent - Signature", "section": "Section 2 New correspondent",
             "page": 2, "signature_n": 0, "kind": "signature", "key": True},
            *person("trustee_1", T1, "Section 3 Trustees' authorisation", 3,
                    ["Text Field 3-1", "Text Field 3-2", "Text Field 3-4", "Text Field 3-5", "Text Field 3-6",
                     "Text Field 3-9", "Text Field 3-10", "Text Field 3-11"], 3, 0, "Text Field 3-14"),
            *person("trustee_2", T2, "Section 3 Trustees' authorisation", 3,
                    ["Text Field 3-16", "Text Field 3-17", "Text Field 3-19", "Text Field 3-20", "Text Field 3-21",
                     "Text Field 3-24", "Text Field 3-25", "Text Field 3-26"], 4, 0, "Text Field 3-29", 4),
        ]},
    "coif-charities-funds-mandate-form": {
        "title": "COIF Charity Funds mandate form",
        "match": ["This mandate form will supersede any other instructions we hold", "Mandate form COIF Charity Funds"],
        "fields": [
            f("todays_date", "Today's date (dd/mm/yyyy)", "Section 1 About your charity", 1, "Text Field 1-1", "date"),
            f("charity_name", "Charity name", "Section 1 About your charity", 1, "Text Field 1-2", key=True),
            f("account_number_1", "CCLA account numbers to which this mandate form applies (1)", "Section 1 About your charity", 1, "Text Field 1-5", key=True),
            f("account_number_2", "CCLA account numbers to which this mandate form applies (2)", "Section 1 About your charity", 1, "Text Field 1-6"),
            f("charity_registration_number", "Charity's registration number", "Section 1 About your charity", 2, "Text Field 1-14", key=True),
            f("office_address", "Charity's office address", "Section 1 About your charity", 2, "Text Field 1-17", key=True),
            f("office_postcode", "Charity's office address - Postcode", "Section 1 About your charity", 2, "Text Field 1-18", key=True),
            f("general_email", "General email address", "Section 1 About your charity", 2, "Text Field 1-19"),
            f("daytime_phone", "Daytime telephone number", "Section 1 About your charity", 2, "Text Field 1-20"),
            f("trustee_1_name", "Trustee/executive director 1 - Name", "Section 2 Trustees", 5, "Text Field 2-26", key=True),
            f("trustee_1_date_of_birth", "Trustee/executive director 1 - Date of birth (dd/mm/yyyy)", "Section 2 Trustees", 5, "Text Field 2-27", "date"),
            f("trustee_1_position", "Trustee/executive director 1 - Position", "Section 2 Trustees", 5, "Text Field 2-28"),
            f("trustee_2_name", "Trustee/executive director 2 - Name", "Section 2 Trustees", 5, "Text Field 2-31", key=True),
            f("trustee_2_date_of_birth", "Trustee/executive director 2 - Date of birth (dd/mm/yyyy)", "Section 2 Trustees", 5, "Text Field 2-32", "date"),
            f("trustee_2_position", "Trustee/executive director 2 - Position", "Section 2 Trustees", 5, "Text Field 2-33"),
            f("bank_name", "Bank/building society name", "Section 5 Nominated bank details", 9, "Text Field 5-2", key=True),
            f("bank_account_name", "Bank account name", "Section 5 Nominated bank details", 9, "Text Field 5-3", key=True),
            f("bank_sort_code", "Sort code", "Section 5 Nominated bank details", 9, "Text Field 5-4", key=True),
            f("bank_account_number", "Bank account number", "Section 5 Nominated bank details", 9, "Text Field 5-5", key=True),
            f("correspondent_title", "Correspondent - Title", "Section 6 Correspondent", 9, "Text Field 6-1"),
            f("correspondent_forename", "Correspondent - Forename", "Section 6 Correspondent", 9, "Text Field 6-2", key=True),
            f("correspondent_surname", "Correspondent - Surname", "Section 6 Correspondent", 9, "Text Field 6-4", key=True),
            f("correspondent_email", "Correspondent - Email address", "Section 6 Correspondent", 9, "Text Field 6-9"),
            {"id": "correspondent_signature", "label": "Correspondent - Signature", "section": "Section 6 Correspondent",
             "page": 10, "signature_n": 0, "kind": "signature", "key": True},
            f("signatory_1_title", "Authorised signatory 1 - Title", "Section 7 Authorised signatories", 10, "Text Field 7-1"),
            f("signatory_1_forename", "Authorised signatory 1 - Forename", "Section 7 Authorised signatories", 10, "Text Field 7-2", key=True),
            f("signatory_1_surname", "Authorised signatory 1 - Surname", "Section 7 Authorised signatories", 10, "Text Field 7-4", key=True),
            f("signatory_1_date_of_birth", "Authorised signatory 1 - Date of birth (dd/mm/yyyy)", "Section 7 Authorised signatories", 10, "Text Field 7-5", "date"),
            {"id": "signatory_1_signature", "label": "Authorised signatory 1 - Signature", "section": "Section 7 Authorised signatories",
             "page": 10, "signature_n": 1, "kind": "signature", "key": True},
            f("signatory_2_title", "Authorised signatory 2 - Title", "Section 7 Authorised signatories", 11, "Text Field 7-16"),
            f("signatory_2_forename", "Authorised signatory 2 - Forename", "Section 7 Authorised signatories", 11, "Text Field 7-17", key=True),
            f("signatory_2_surname", "Authorised signatory 2 - Surname", "Section 7 Authorised signatories", 11, "Text Field 7-19", key=True),
            f("signatory_2_date_of_birth", "Authorised signatory 2 - Date of birth (dd/mm/yyyy)", "Section 7 Authorised signatories", 11, "Text Field 7-20", "date"),
            {"id": "signatory_2_signature", "label": "Authorised signatory 2 - Signature", "section": "Section 7 Authorised signatories",
             "page": 11, "signature_n": 0, "kind": "signature", "key": True},
            f("trustee_declaration_1_name", "Trustee declaration 1 - Name", "Section 10 Trustees' declaration", 17, "Text Field 10-1", key=True),
            {"id": "trustee_declaration_1_signature", "label": "Trustee declaration 1 - Signature", "section": "Section 10 Trustees' declaration",
             "page": 17, "signature_n": 0, "kind": "signature", "key": True},
            f("trustee_declaration_1_date", "Trustee declaration 1 - Date (dd/mm/yyyy)", "Section 10 Trustees' declaration", 17, "Text Field 10-5", "date"),
            f("trustee_declaration_2_name", "Trustee declaration 2 - Name", "Section 10 Trustees' declaration", 17, "Text Field 10-6", key=True),
            {"id": "trustee_declaration_2_signature", "label": "Trustee declaration 2 - Signature", "section": "Section 10 Trustees' declaration",
             "page": 17, "signature_n": 1, "kind": "signature", "key": True},
            f("trustee_declaration_2_date", "Trustee declaration 2 - Date (dd/mm/yyyy)", "Section 10 Trustees' declaration", 17, "Text Field 10-10", "date"),
        ]},
    "coif-charities-funds-amendment-income-and-withdrawal-payments-form": {
        "title": "COIF Charity Funds income and withdrawal payments amendment form",
        "match": ["Income and withdrawal payments amendment form", "Use this form to make amendments to", "Nominated bank details"],
        "fields": [
            f("todays_date", "Today's date (dd/mm/yyyy)", "Section 1 About your charity", 1, "Text Field 1-1", "date"),
            f("charity_name", "Charity name", "Section 1 About your charity", 1, "Text Field 1-2", key=True),
            f("account_number_1", "CCLA account number(s) to which this change applies (1)", "Section 1 About your charity", 1, "Text Field 1-3", key=True),
            f("account_number_2", "CCLA account number(s) to which this change applies (2)", "Section 1 About your charity", 1, "Text Field 1-4"),
            f("deposit_income_add_to_balance", "2.1 Deposit Fund: Add income to the balance of this account(s)", "Section 2 Income", 1, "Check Box 2-1", "checkbox", index=0),
            f("deposit_income_pay_to_deposit_account", "2.1 Deposit Fund: Pay income to another COIF Charities Deposit Fund account", "Section 2 Income", 1, "Check Box 2-1", "checkbox", index=1),
            f("deposit_income_pay_to_bank", "2.1 Deposit Fund: Pay income to the nominated bank account (in Section 3)", "Section 2 Income", 2, "Check Box 2-1", "checkbox", index=0),
            f("bank_name", "Bank/building society name", "Section 3 Nominated bank details", 2, "Text Field 3-1", key=True),
            f("bank_account_name", "Bank account name", "Section 3 Nominated bank details", 2, "Text Field 3-2", key=True),
            f("bank_sort_code", "Sort code", "Section 3 Nominated bank details", 2, "Text Field 3-3", key=True),
            f("bank_account_number", "Bank account number", "Section 3 Nominated bank details", 2, "Text Field 3-4", key=True),
            *person("trustee_1", T1, "Section 4 Trustees' authorisation", 3,
                    ["Text Field 4-1", "Text Field 4-2", "Text Field 4-4", "Text Field 4-5", "Text Field 4-6",
                     "Text Field 4-9", "Text Field 4-10", "Text Field 4-11"], 3, 0, "Text Field 4-14"),
            *person("trustee_2", T2, "Section 4 Trustees' authorisation", 3,
                    ["Text Field 4-16", "Text Field 4-17", "Text Field 4-19", "Text Field 4-20", "Text Field 4-21",
                     "Text Field 4-24", "Text Field 4-25", "Text Field 4-26"], 4, 0, "Text Field 4-29", 4),
        ]},
}


def widgets_of(doc):
    """{(page, name): [rect, ...]} in page order (checkbox groups have several widgets under one name)."""
    out = {}
    for pn, page in enumerate(doc, 1):
        for w in page.widgets() or []:
            out.setdefault((pn, w.field_name), []).append([round(v, 1) for v in w.rect])
    return out


def signature_boxes(doc):
    """{page: [rect, ...]}: the box under each printed 'Signature' word (top to bottom), never the declaration text."""
    out = {}
    for pn, page in enumerate(doc, 1):
        words = [w for w in page.get_text("words") if w[4] == "Signature"]
        widgets = [w.rect for w in page.widgets() or []]
        for x0, y0, x1, y1, *_ in sorted(words, key=lambda w: (w[1], w[0])):
            right = min([r.x0 - 10 for r in widgets if r.x0 > x0 + 60 and r.y0 < y1 + 45 and r.y1 > y1] or [x0 + 250])
            out.setdefault(pn, []).append([round(x0, 1), round(y1 + 4, 1), round(min(right, x0 + 250), 1), round(y1 + 46, 1)])
    return out


def build():
    import fitz  # pymupdf, synthetic venv only
    forms = {}
    for slug, spec in FORMS.items():
        doc = fitz.open(FORMS_DIR / f"{slug}.pdf")
        widgets, sigs = widgets_of(doc), signature_boxes(doc)
        fields, seen = [], set()
        for fd in spec["fields"]:
            fd = dict(fd)
            if fd["id"] in seen:
                raise SystemExit(f"{slug}: duplicate field id {fd['id']}")
            seen.add(fd["id"])
            if fd["kind"] == "signature":
                boxes = sigs.get(fd["page"]) or []
                n = fd.pop("signature_n")
                if n >= len(boxes):
                    raise SystemExit(f"{slug}: no signature box {n} on page {fd['page']}")
                fd["widget"] = ""
                fd["rect"] = boxes[n]
            else:
                rects = widgets.get((fd["page"], fd["widget"]))
                if not rects:
                    raise SystemExit(f"{slug}: widget {fd['widget']!r} not on page {fd['page']}")
                fd["rect"] = rects[fd.get("widget_index", 0)]
            fields.append({k: fd[k] for k in ("id", "label", "section", "page", "widget", "widget_index", "rect", "kind", "key")
                           if k in fd})
        text = " ".join(" ".join(pg.get_text().split()) for pg in doc).casefold()
        absent = [m for m in spec["match"] if " ".join(m.split()).casefold() not in text]
        if absent:
            raise SystemExit(f"{slug}: match phrase(s) not printed on the form: {absent}")
        forms[slug] = {"title": spec["title"], "match": spec["match"], "pages": doc.page_count, "fields": fields}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"version": 1, "note": "Fields of the CCLA static-detail forms a reviewer compares with "
                               "Register. match = phrases printed on the form (each checked against the PDF text), "
                               "most distinctive first; the bare title can also appear in a covering email. id/label/section/page/kind/key are for the reviewer; widget/widget_index/rect "
                               "(PDF points, page origin top-left) are for the synthetic test generator.",
                               "forms": forms}, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"wrote {OUT} ({sum(len(v['fields']) for v in forms.values())} fields in {len(forms)} forms)")


if __name__ == "__main__":
    sys.exit(build())
