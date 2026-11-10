"""The two questions the Azure reviewer asks a language model, and the strict JSON shapes of their answers.

  1. Form filling (once per recognised form): which value is written in each field, and which numbered text it was
     read from (citations).  The code then decides handwriting and confidence from those citations.
  2. Case review (once per case): the existing daily instructions + the Sub Worktype rules + the result contract,
     answered in the contract's keys, except REVIEW_RESULT (neutral name) and FIELD_IDS on each comparison.
"""
from __future__ import annotations

import json

RESULT_WORDS = ("Correct", "Incorrect", "Inconclusive")
RULE_WORDS = ("Satisfied", "Not satisfied", "Not determinable", "Not applicable")
REVIEW_KEYS = ("REVIEW_RESULT", "DISCREPANCY_TYPE", "HEADLINE", "CASE_END_DATETIME", "REGISTER_SNAPSHOT_DATETIME",
               "CLIENT_REQUEST", "JUSTIFICATION", "A_B_COMPARISONS", "RULE_CHECKS")
ROW_KEYS = ("ITEM", "Customer_expectation", "CUSTOMER_SOURCE", "System_data", "SYSTEM_SOURCE", "MATCH", "EXPLANATION",
            "FIELD_IDS")
RULE_KEYS = ("RULE", "STATUS", "EVIDENCE", "NOTE")

FORM_SYSTEM = (
    "You transcribe one filled-in paper form. You cannot see the page: a document reader turned it into numbered "
    "lines. Each line starts with its id in brackets, e.g. [D2.p1.l7]; '{handwritten}' at the end means the reader "
    "saw handwriting in that line. Tick boxes appear as [D2.p1.s3] '[X] ticked' or '[ ] not ticked' with the nearest "
    "line. Words of a line can be cited one by one as <line id>.w<n> (w1 = first word).\n"
    "For EVERY field in the list, return exactly one entry:\n"
    "- VALUE: exactly what is written for that field, as written (keep spelling, do not correct, do not guess, do not "
    "reformat; join letters written in separate boxes, e.g. 'J O H N' -> 'JOHN'). Empty text when the field is blank "
    "or you cannot find it.\n"
    "- checkbox fields: VALUE 'ticked' or 'not ticked', citing the tick box id.\n"
    "- signature fields: VALUE 'signed' when there is handwriting in the signature space, else 'not signed'; cite the "
    "handwritten line(s).\n"
    "- CITATIONS: the ids of the line(s) or word(s) the VALUE was read from (prefer word ids when a line also holds "
    "the printed label). Empty list when VALUE is empty.\n"
    "Printed labels and instructions of the form are NOT values. Never invent a value that is not in the text.")


def form_schema(field_ids: list) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["FIELDS", "NOTES"],
            "properties": {
                "FIELDS": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["FIELD", "VALUE", "CITATIONS"],
                    "properties": {"FIELD": {"type": "string", "enum": list(field_ids)},
                                   "VALUE": {"type": "string"},
                                   "CITATIONS": {"type": "array", "items": {"type": "string"}}}}},
                "NOTES": {"type": "string"}}}


def form_message(form: dict, doc, expected_pages: int) -> str:
    fields = [{"FIELD": f["id"], "label": f.get("label", ""), "section": f.get("section", ""),
               "page": f.get("page"), "kind": f.get("kind", "text")} for f in form["fields"]]
    head = (f"Form: {form.get('title', '')} (document {doc.doc_id}, file '{doc.file}'; the blank form has "
            f"{expected_pages or '?'} page(s), this document has {doc.pages}).")
    return (head + "\n\nFIELDS TO FILL (one entry each):\n" + json.dumps(fields, ensure_ascii=False)
            + "\n\nTHE DOCUMENT AS READ:\n" + doc.render())


def review_schema(discrepancy_words) -> dict:
    row = {"type": "object", "additionalProperties": False, "required": list(ROW_KEYS),
           "properties": {"ITEM": {"type": "string"}, "Customer_expectation": {"type": "string"},
                          "CUSTOMER_SOURCE": {"type": "string"}, "System_data": {"type": "string"},
                          "SYSTEM_SOURCE": {"type": "string"}, "MATCH": {"type": ["boolean", "null"]},
                          "EXPLANATION": {"type": "string"},
                          "FIELD_IDS": {"type": "array", "items": {"type": "string"}}}}
    rule = {"type": "object", "additionalProperties": False, "required": list(RULE_KEYS),
            "properties": {"RULE": {"type": "string"}, "STATUS": {"type": "string", "enum": list(RULE_WORDS)},
                           "EVIDENCE": {"type": "string"}, "NOTE": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": list(REVIEW_KEYS),
            "properties": {"REVIEW_RESULT": {"type": "string", "enum": list(RESULT_WORDS)},
                           "DISCREPANCY_TYPE": {"type": "string", "enum": list(discrepancy_words)},
                           "HEADLINE": {"type": "string"}, "CASE_END_DATETIME": {"type": "string"},
                           "REGISTER_SNAPSHOT_DATETIME": {"type": "string"}, "CLIENT_REQUEST": {"type": "string"},
                           "JUSTIFICATION": {"type": "string"},
                           "A_B_COMPARISONS": {"type": "array", "items": row},
                           "RULE_CHECKS": {"type": "array", "items": rule}}}


REVIEW_SYSTEM = (
    "You review ONE account case for a bank operations team. You cannot see the documents: a document reader turned "
    "each one into numbered lines ([D3.p1.l7] = document D3, page 1, line 7; '{handwritten}' = handwriting seen in "
    "that line), and a first pass already read every field of the recognised forms (EXTRACTED_FIELDS, each with a "
    "FIELD_ID, its VALUE and how sure the reading is).\n"
    "Do the whole analysis exactly as the attached daily_review_instructions.md, the Sub Worktype rules file and "
    "result_contract.md require. Your answer is ONE JSON object in the required schema. It follows result_contract.md "
    "in every key, value and rule, with two differences only: (1) the first key is REVIEW_RESULT (where the contract "
    "says COPILOT_RESULT; same values); (2) every A_B_COMPARISONS row also has FIELD_IDS: the FIELD_IDs of "
    "EXTRACTED_FIELDS that its Customer_expectation rests on (empty list when it rests on no extracted field).\n"
    "LOW CONFIDENCE RULE: a field with LOW_CONFIDENCE true was not read reliably (handwriting read with low "
    "confidence, or the reading could not be verified). A comparison that rests on such a field cannot be made: "
    "MATCH null, EXPLANATION starting 'Could not compare: the form value was read with low confidence'. Never treat a "
    "low-confidence reading as a mismatch or a match. You may read the document lines yourself, but a value you read "
    "there that differs from the extracted VALUE is also only low confidence.\n"
    "Copy CASE_END_DATETIME and REGISTER_SNAPSHOT_DATETIME exactly from the case timing given. No markdown inside "
    "values.")


def review_message(base: str, case: dict, daily: str, rules_name: str, rules: str, contract: str,
                   documents: list, fields: list, texts: list) -> str:
    """The case-review question: the (filled) base message, the case facts incl. the case source, the three
    instruction files, the document list, the extracted fields and every document's text."""
    facts = [f"- Instruction ID: {case['iid']}", f"- Client Account Number: {case.get('account') or 'not stated'}",
             f"- Client Number: {case.get('client_number') or 'not stated'}",
             f"- Sub Worktype: {case.get('sub_worktype') or 'not stated'}",
             "- Case source (where the customer request came in, from the Studio report): "
             + (case.get("case_source") or "not stated")]
    docs = []
    for d in documents:
        extra = ""
        if d.get("FORM") and d.get("PAGES_EXPECTED"):
            extra = f"; recognised form '{d['FORM']}' ({d['PAGES_EXPECTED']} page(s) when complete)"
        docs.append(f"- {d['DOC_ID']}: '{d['FILE']}' - {d['KIND']}, {d['PAGES']} page(s) read{extra}")
    shown = [{k: f[k] for k in ("FIELD_ID", "LABEL", "VALUE", "HANDWRITTEN", "CONFIDENCE", "LOW_CONFIDENCE")}
             for f in fields]
    parts = ["# THE CASE MESSAGE", base.strip(), "", "# CASE FACTS", *facts, "",
             "# ATTACHED FILE: daily_review_instructions.md", daily.strip(), "",
             f"# ATTACHED FILE: Sub Worktype rules file '{rules_name}'", rules.strip(), "",
             "# ATTACHED FILE: result_contract.md (answer with REVIEW_RESULT + FIELD_IDS as told; the JSON schema "
             "replaces its 'one line of plain text' layout)", contract.strip(), "",
             "# DOCUMENTS OF THE CASE", *docs, "",
             "# EXTRACTED_FIELDS (from the form-reading pass)", json.dumps(shown, ensure_ascii=False, indent=0), ""]
    for d, text in texts:
        parts += [f"# DOCUMENT {d['DOC_ID']}: '{d['FILE']}' ({d['KIND']})", text, ""]
    return "\n".join(parts)


def fixup_message(problems: list) -> str:
    return ("The program that reads your answer rejected it. Answer again with the complete corrected JSON object, "
            "fixing exactly these problems and keeping the rest of your review the same:\n- " + "\n- ".join(problems))
