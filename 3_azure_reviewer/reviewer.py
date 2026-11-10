"""One case through the Azure reviewer: read every document, fill each recognised form, review the case.

The verdict logic is the result contract's (shared with the Copilot sender: its validate_result is reused); the
code adds only what a reading machine needs: a comparison resting on a low-confidence field is 'could not compare'
(MATCH null), and a case whose key field is unreadable, or whose form is short of pages, can never be Correct.
"""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import azure_client as AZ
import prompts as PR
from evidence import DocEvidence, derive_field

READ_DI = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".docx", ".xlsx", ".pptx", ".html", ".htm",
           ".heif"}
PAGED = {".pdf", ".tif", ".tiff"}                 # sent in 2-page pieces on the free tier
OFFICE = {".docx", ".xlsx", ".pptx"}
READ_LOCAL = {".txt", ".md", ".csv", ".json", ".xml", ".eml"}
HOLD_TYPES = {".msg", ".doc", ".xls", ".ppt", ".rtf", ".gif"}    # cannot be read: the whole case is held
AUDIT_RE = re.compile(r"audit\s*tr(ai|ia)l", re.I)
STATE_OF = {"Correct": "result_correct", "Incorrect": "result_incorrect", "Inconclusive": "result_inconclusive"}


class Held(Exception):
    """This case is held (not reviewed), with a plain-English reason."""


@dataclass
class CaseInput:
    iid: str
    folder: Path
    account: str = ""
    client_number: str = ""
    sub_worktype: str = ""
    case_source: str = ""
    rules: Path | None = None
    end_date: str = ""
    from_csv: bool = True
    order: list = field(default_factory=list)       # file names in System 1's order (pipeline), else by name
    alias: str = ""


def _words(text) -> str:
    return " ".join(re.findall(r"[a-z0-9@]+", str(text or "").lower()))


def load_forms(path: Path) -> dict:
    """form_maps.json -> {slug: form}; {} when it is not there yet (every document is then 'supporting')."""
    try:
        data = json.loads(Path(path).read_text("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    forms = data.get("forms") if isinstance(data, dict) else None
    return forms if isinstance(forms, dict) else {}


def detect_form(forms: dict, ev: DocEvidence, filename: str) -> str:
    """The form this document is: most of its 'match' phrases are printed on its first pages ('' = none)."""
    text = " " + _words(" ".join(L.text for L in ev.lines if L.page <= (min(ev.page_numbers or [1]) + 2))) + " "
    fname = _words(Path(filename).stem)
    best, best_score = "", 0.0
    for slug, form in forms.items():
        phrases = [p for p in (form.get("match") or []) if _words(p)]
        if not phrases:
            continue
        hits = sum(1 for p in phrases if f" {_words(p)} " in text)
        if hits < max(1, (len(phrases) + 1) // 2):
            continue
        score = hits / len(phrases) + (0.5 if _words(slug.replace("-", " ")) in fname else 0)
        if score > best_score:
            best, best_score = slug, score
    return best


def snapshot_retrieved(data) -> str | None:
    try:
        return ((data.get("Account Summary") or {}).get("metadata") or {}).get("retrievedUtc")
    except AttributeError:
        return None


class Reviewer:
    def __init__(self, *, di, chat, forms: dict, form_model: str, case_model: str, threshold: float,
                 daily: str, contract: str, base: str, validate, timing, discrepancy_words, snapshot_suffix: str,
                 free_tier: bool = True, ignore=lambda name: False, stop=lambda: False, fixups: int = 3):
        self.di, self.chat, self.forms = di, chat, forms
        self.form_model, self.case_model, self.threshold = form_model, case_model, float(threshold)
        self.daily, self.contract, self.base = daily, contract, base
        self.validate, self.timing = validate, timing       # sender.validate_result, (end, retrieved, from_csv) -> dict
        self.discrepancy_words, self.snapshot_suffix = tuple(discrepancy_words), snapshot_suffix
        self.free_tier, self.ignore, self.stop, self.fixups = free_tier, ignore, stop, fixups

    # -- one case -------------------------------------------------------------------------------
    def review(self, ci: CaseInput) -> dict:
        rec = {"iid": ci.iid, "account": ci.account, "case_source": ci.case_source, "state": "", "reason": "",
               "reviewer": "azure", "models": {"form_filling": self.form_model, "case_review": self.case_model},
               "result": None, "DOCUMENTS": [], "EXTRACTED_FIELDS": [], "notes": [], "_turns": []}
        try:
            self._review(ci, rec)
        except AZ.SafeStop as exc:
            rec.update(state="not_sent_stopped", reason=str(exc))
            exc.record = rec
            raise
        except AZ.Stopped:
            rec.update(state="not_sent_stopped", reason="run stopped before this case was reviewed")
        except (Held, AZ.Refused, AZ.TemporaryError, AZ.BadRequest) as exc:
            rec.update(state="held", reason=str(exc), result=None)
        except Exception as exc:                          # noqa: BLE001 - a program error: never retried, traced
            rec.update(state="held", result=None,
                       reason=f"program error ({type(exc).__name__}: {str(exc)[:160]}) - see the run report")
            rec["_trace"] = traceback.format_exc()
        return rec

    def _check_stop(self):
        if self.stop():
            raise AZ.Stopped()

    def files(self, ci: CaseInput) -> list:
        try:
            entries = sorted((p for p in Path(ci.folder).iterdir() if p.is_file()), key=lambda p: p.name.casefold())
        except OSError:
            raise Held("the case folder could not be listed") from None
        entries = [p for p in entries if not p.name.startswith((".", "~$")) and not self.ignore(p.name)]
        bad = [p.name for p in entries if p.suffix.lower() in HOLD_TYPES]
        if bad:
            raise Held("file type the Azure reviewer cannot read: " + ", ".join(bad[:5]) + " - review this case by hand")
        odd = [p.name for p in entries if p.suffix.lower() not in READ_DI | READ_LOCAL]
        if odd:
            raise Held("unsupported file type: " + ", ".join(odd[:5]) + " - review this case by hand")
        rank = {n.casefold(): i for i, n in enumerate(ci.order or [])}

        def key(p):
            kind = 0 if p.name.endswith(self.snapshot_suffix) else 1 if AUDIT_RE.search(p.name) else 2
            return (kind, rank.get(p.name.casefold(), len(rank)), p.name.casefold())
        return sorted(entries, key=key)

    def _review(self, ci: CaseInput, rec: dict) -> None:
        if not ci.rules or not Path(ci.rules).is_file():
            raise Held(f"no instructions file for Sub Worktype '{ci.sub_worktype}' in the guidance folder")
        rules_text = Path(ci.rules).read_text("utf-8-sig", errors="replace")
        files = self.files(ci)
        if not any(p.name.endswith(self.snapshot_suffix) for p in files):
            raise Held("no Register snapshot in the case folder")
        evs, docs, snapshot = self.read_documents(files, rec)
        rec["DOCUMENTS"] = docs
        timing = self.timing(ci.end_date, snapshot_retrieved(snapshot), ci.from_csv)
        rec["notes"] += [n for n in timing.get("notes", [])]
        fields = self.fill_forms(docs, evs, rec)
        rec["EXTRACTED_FIELDS"] = fields
        self._check_stop()
        result, state, reason = self.review_case(ci, rules_text, timing, docs, evs, fields, rec)
        rec.update(result=result, state=state, reason=reason)

    # -- reading --------------------------------------------------------------------------------
    def read_documents(self, files: list, rec: dict) -> tuple:
        evs, docs, snapshot = {}, [], None
        for i, path in enumerate(files, 1):
            self._check_stop()
            doc_id, ext = f"D{i}", path.suffix.lower()
            if path.name.endswith(self.snapshot_suffix):
                try:
                    snapshot = json.loads(path.read_text("utf-8-sig"))
                except (OSError, ValueError):
                    raise Held("the Register snapshot could not be read") from None
                ev, kind = DocEvidence.snapshot(doc_id, path.name, snapshot), "snapshot"
            elif ext in READ_LOCAL:
                ev = DocEvidence.from_text(doc_id, path.name, path.read_text("utf-8-sig", errors="replace"))
                kind = "supporting"
            else:
                results = self.di.analyze(path.read_bytes(), paged=ext in PAGED)
                ev, kind = DocEvidence.from_di(doc_id, path.name, results), "supporting"
                if ext in OFFICE and self.free_tier and ev.pages >= 2:
                    rec["notes"].append(f"CHECK: '{path.name}' is an Office file - the free tier may have read only "
                                        "its first 2 pages")
            form = ""
            if kind != "snapshot":
                if AUDIT_RE.search(path.name):
                    kind = "audit_trail"
                else:
                    form = detect_form(self.forms, ev, path.name)
                    kind = "form" if form else "supporting"
            row = {"DOC_ID": doc_id, "FILE": path.name, "KIND": kind, "FORM": form, "PAGES": ev.pages,
                   "PAGES_READ": ev.pages}
            if form:
                row["PAGES_EXPECTED"] = int(self.forms[form].get("pages") or 0)
                row["TITLE"] = self.forms[form].get("title", form)
            row["HANDWRITTEN_LINES"] = ev.handwritten_lines()
            docs.append(row)
            evs[doc_id] = ev
        return evs, docs, snapshot

    def fill_forms(self, docs: list, evs: dict, rec: dict) -> list:
        out = []
        for d in docs:
            if d["KIND"] != "form":
                continue
            self._check_stop()
            form, ev = self.forms[d["FORM"]], evs[d["DOC_ID"]]
            fdefs = [{**f, "_form": d["FORM"]} for f in form.get("fields") or [] if f.get("id")]
            if not fdefs:
                continue
            messages = [{"role": "system", "content": PR.FORM_SYSTEM},
                        {"role": "user", "content": PR.form_message(form, ev, d.get("PAGES_EXPECTED", 0))}]
            schema = PR.form_schema([f["id"] for f in fdefs])
            obj = None
            for attempt in range(2):
                try:
                    obj, raw, model = self.chat.complete(self.form_model, messages, "form_fields", schema,
                                                         f"form reading ({d['DOC_ID']})")
                    rec["_turns"].append({"kind": "form", "doc": d["DOC_ID"], "form": d["FORM"], "model": model,
                                          "reply": raw})
                    break
                except AZ.BadAnswer as exc:
                    rec["_turns"].append({"kind": "form-unusable", "doc": d["DOC_ID"], "reply": exc.raw,
                                          "problem": str(exc)})
            if obj is None:
                raise Held(f"the form-reading answer for {d['DOC_ID']} was not usable twice")
            got = {}
            for e in obj.get("FIELDS") or []:
                if isinstance(e, dict) and e.get("FIELD") not in got:
                    got[e.get("FIELD")] = e
            for f in fdefs:
                e = got.get(f["id"])
                if e is None:
                    row = derive_field(ev, f, "", [], self.threshold)
                    row.update(CONFIDENCE=0.0, LOW_CONFIDENCE=True, REASON="the model returned nothing for this field")
                else:
                    row = derive_field(ev, f, e.get("VALUE"), e.get("CITATIONS"), self.threshold)
                row["KEY"] = bool(f.get("key"))
                out.append(row)
        return out

    # -- the case review ------------------------------------------------------------------------
    def review_case(self, ci, rules_text, timing, docs, evs, fields, rec) -> tuple:
        expect = {**timing, "rules_file": True}
        case = {"iid": ci.iid, "account": ci.account, "client_number": ci.client_number,
                "sub_worktype": ci.sub_worktype, "case_source": ci.case_source}
        texts = [(d, evs[d["DOC_ID"]].render()) for d in docs]
        base = self._fill(self.base, timing)
        message = PR.review_message(base, case, self.daily, Path(ci.rules).name, rules_text, self.contract,
                                    docs, fields, texts)
        messages = [{"role": "system", "content": PR.REVIEW_SYSTEM}, {"role": "user", "content": message}]
        schema = PR.review_schema(self.discrepancy_words)
        by_id = {f["FIELD_ID"]: f for f in fields}
        problems, obj = [], None
        for attempt in range(1 + self.fixups):
            self._check_stop()
            try:
                obj, raw, model = self.chat.complete(self.case_model, messages, "case_review", schema, "case review")
            except AZ.BadAnswer as exc:
                raw, problems = exc.raw, [str(exc)]
                rec["_turns"].append({"kind": "review-unusable", "model": self.case_model, "reply": raw,
                                      "problems": problems})
            else:
                problems = self.problems(obj, expect)
                rec["_turns"].append({"kind": "review" if attempt == 0 else "review-fixup", "model": model,
                                      "reply": raw, "problems": problems})
                if not problems:
                    break
            messages += [{"role": "assistant", "content": raw or "(no answer)"},
                         {"role": "user", "content": PR.fixup_message(problems)}]
        if problems:
            return (self.ordered(obj) if obj else None, "result_invalid",
                    f"the answer did not follow the result contract after {self.fixups} fix-up request(s): "
                    + "; ".join(problems[:4]))
        notes = self.enforce(obj, by_id, docs)
        rec["notes"] += notes
        again = self.problems(obj, expect)
        if again:                                       # cannot happen when enforce() is right: never hide it
            return self.ordered(obj), "result_invalid", "program check left the answer invalid: " + "; ".join(again[:4])
        return self.ordered(obj), STATE_OF[obj["REVIEW_RESULT"]], "; ".join(notes)

    @staticmethod
    def _fill(base: str, timing: dict) -> str:
        values = {"{{CASE_END_DATETIME}}": timing.get("case_end"), "{{REGISTER_SNAPSHOT_DATETIME}}": timing.get("snapshot"),
                  "{{BUSINESS_DAYS_GAP}}": timing.get("gap"), "{{CASE_AGE_STATEMENT}}": timing.get("age")}
        for k, v in values.items():
            base = base.replace(k, str(v or "Not provided"))
        return base

    def problems(self, obj: dict, expect: dict) -> list:
        """The contract's checks (the sender's own validator) on the answer, read with COPILOT_RESULT's name."""
        if not isinstance(obj, dict):
            return ["the answer is not a JSON object"]
        out = []
        rows = obj.get("A_B_COMPARISONS")
        if isinstance(rows, list):
            for i, r in enumerate(rows, 1):
                if not isinstance(r, dict) or not isinstance(r.get("FIELD_IDS"), list):
                    out.append(f"comparison row {i}: FIELD_IDS must be a list")
        twin = {("COPILOT_RESULT" if k == "REVIEW_RESULT" else k): v for k, v in obj.items()}
        if isinstance(rows, list):
            twin["A_B_COMPARISONS"] = [{k: v for k, v in r.items() if k != "FIELD_IDS"} if isinstance(r, dict) else r
                                       for r in rows]
        out += [p.replace("COPILOT_RESULT", "REVIEW_RESULT") for p in self.validate(twin, expect)]
        return out

    def enforce(self, obj: dict, by_id: dict, docs: list) -> list:
        """The program's own rules on a valid answer.  Returns notes (shown in the report) for every change."""
        notes = []
        rows = obj["A_B_COMPARISONS"]
        for r in rows:
            r["FIELD_IDS"] = [i for i in r["FIELD_IDS"] if i in by_id]
            weak = [by_id[i] for i in r["FIELD_IDS"] if by_id[i]["LOW_CONFIDENCE"]]
            if weak and r["MATCH"] is not None:
                r["MATCH"] = None
                r["EXPLANATION"] = ("Could not compare: the form value was read with low confidence ("
                                    + "; ".join(f"{w['LABEL'] or w['FIELD']}: {w['REASON']}" for w in weak)
                                    + ") - check the document by hand. Reviewer model's reading: " + r["EXPLANATION"])
                notes.append(f"'{r['ITEM']}' set to could not compare (low-confidence field)")
        verdict = self._verdict(obj)
        if verdict == "Correct":
            used = {i for r in rows for i in r["FIELD_IDS"]}
            for f in by_id.values():
                if f.get("KEY") and f["LOW_CONFIDENCE"] and f["VALUE"] and f["FIELD_ID"] not in used:
                    doc = next((d for d in docs if d["DOC_ID"] == f["DOC_ID"]), {})
                    rows.append({"ITEM": f"Program check - {f['LABEL'] or f['FIELD']} ({doc.get('TITLE', f['FORM'])})",
                                 "Customer_expectation": f["VALUE"],
                                 "CUSTOMER_SOURCE": f"{f['DOC_ID']} '{doc.get('FILE', '')}'",
                                 "System_data": "Not captured", "SYSTEM_SOURCE": "Not compared",
                                 "MATCH": None,
                                 "EXPLANATION": f"Could not compare: the form value was read with low confidence "
                                                f"({f['REASON']}) - check the document by hand.",
                                 "FIELD_IDS": [f["FIELD_ID"]]})
                    notes.append(f"key field '{f['LABEL'] or f['FIELD']}' unreadable: added as could not compare")
            for d in docs:
                if d["KIND"] == "form" and d.get("PAGES_EXPECTED") and d["PAGES"] < d["PAGES_EXPECTED"]:
                    rows.append({"ITEM": f"Program check - pages of {d.get('TITLE', d['FORM'])} ({d['DOC_ID']})",
                                 "Customer_expectation": f"All {d['PAGES_EXPECTED']} pages of the form",
                                 "CUSTOMER_SOURCE": f"{d['DOC_ID']} '{d['FILE']}'", "System_data": "Not captured",
                                 "SYSTEM_SOURCE": "Not compared", "MATCH": None,
                                 "EXPLANATION": f"Could not compare: the document has {d['PAGES']} of the form's "
                                                f"{d['PAGES_EXPECTED']} pages - a page may be missing; check it by hand.",
                                 "FIELD_IDS": []})
                    notes.append(f"{d['DOC_ID']} has {d['PAGES']} of {d['PAGES_EXPECTED']} form pages: added as "
                                 "could not compare")
        new = self._verdict(obj)
        if new != obj["REVIEW_RESULT"]:
            old = obj["REVIEW_RESULT"]
            obj["REVIEW_RESULT"] = new
            if False not in [r["MATCH"] for r in rows]:
                obj["DISCREPANCY_TYPE"] = "None"
            obj["JUSTIFICATION"] = (obj["JUSTIFICATION"].rstrip() + f" [Program note: result changed from {old} to "
                                    f"{new} - " + "; ".join(notes) + ".]")
            notes.append(f"result changed from {old} to {new}")
        return notes

    @staticmethod
    def _verdict(obj: dict) -> str:
        flags = [r["MATCH"] for r in obj["A_B_COMPARISONS"]]
        statuses = [r["STATUS"] for r in obj["RULE_CHECKS"]]
        if False in flags or "Not satisfied" in statuses:
            return "Incorrect"
        if None in flags or "Not determinable" in statuses or not statuses:
            return "Inconclusive"
        return "Correct"

    @staticmethod
    def ordered(obj: dict) -> dict:
        out = {k: obj.get(k) for k in PR.REVIEW_KEYS}
        if isinstance(out["A_B_COMPARISONS"], list):
            out["A_B_COMPARISONS"] = [{k: r.get(k) for k in PR.ROW_KEYS} if isinstance(r, dict) else r
                                      for r in out["A_B_COMPARISONS"]]
        if isinstance(out["RULE_CHECKS"], list):
            out["RULE_CHECKS"] = [{k: r.get(k) for k in PR.RULE_KEYS} if isinstance(r, dict) else r
                                  for r in out["RULE_CHECKS"]]
        return out
