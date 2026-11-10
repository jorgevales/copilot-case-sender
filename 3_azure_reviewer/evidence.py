"""What Document Intelligence read, as numbered evidence the model can cite - and the code that decides, from those
citations alone, whether a field is handwritten and how sure the reading is.

Citation ids shown to the model:  D1.p3.l5 = line 5 of page 3 of document D1,  D1.p3.l5.w2 = its 2nd word,
D1.p3.s1 = selection mark (tick box) 1 of page 3.  The model only says WHICH text it read a value from; the code
derives HANDWRITTEN / CONFIDENCE / LOW_CONFIDENCE.  A citation that does not exist, or a value that is not in the
cited text, is low confidence (the model may have guessed).
"""
from __future__ import annotations

import datetime
import html
import json
import re
from dataclasses import dataclass, field


@dataclass
class Word:
    id: str
    text: str
    conf: float
    hw: bool
    page: int = 0
    centre: tuple = (0.0, 0.0)


@dataclass
class Line:
    id: str
    text: str
    page: int
    words: list = field(default_factory=list)
    centre: tuple = (0.0, 0.0)

    @property
    def hw(self) -> bool:
        return any(w.hw for w in self.words)


@dataclass
class Mark:
    id: str
    selected: bool
    conf: float
    page: int
    near: str = ""


def _centre(polygon) -> tuple:
    pts = list(polygon or [])
    if len(pts) < 2:
        return (0.0, 0.0)
    xs, ys = pts[0::2], pts[1::2]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _overlaps(offset: int, length: int, spans: list) -> bool:
    end = offset + max(length, 1)
    return any(offset < b and a < end for a, b in spans)


class DocEvidence:
    """One document's numbered lines, words and tick boxes."""

    def __init__(self, doc_id: str, file: str):
        self.doc_id, self.file = doc_id, file
        self.lines, self.marks, self.index = [], [], {}
        self.page_numbers = []
        self.page_size = {}                # page number -> (width, height) in Azure's unit (inch for PDF, pixel)
        self.local = False                 # read on this computer (text file): confidence 1, never handwritten
        self.raw_text = ""                 # local documents that are shown whole (the snapshot JSON)

    # -- building -----------------------------------------------------------------------------
    @classmethod
    def from_di(cls, doc_id: str, file: str, results: list) -> "DocEvidence":
        ev = cls(doc_id, file)
        for ar in results:
            hw_spans = []
            for st in ar.get("styles") or []:
                if st.get("isHandwritten"):
                    hw_spans += [(s["offset"], s["offset"] + s["length"]) for s in st.get("spans") or []]
            for page in ar.get("pages") or []:
                n = int(page.get("pageNumber") or 0)
                if n in ev.page_numbers:
                    continue
                ev.page_numbers.append(n)
                ev.page_size[n] = (float(page.get("width") or 0), float(page.get("height") or 0))
                words = page.get("words") or []
                used = set()
                lines = []
                for k, ln in enumerate(page.get("lines") or [], 1):
                    lid = f"{doc_id}.p{n}.l{k}"
                    line = Line(lid, ln.get("content", ""), n, centre=_centre(ln.get("polygon")))
                    spans = [(s["offset"], s["offset"] + s["length"]) for s in ln.get("spans") or []]
                    j = 0
                    for wi, w in enumerate(words):
                        if wi in used:
                            continue
                        span = w.get("span") or {}
                        off = span.get("offset", -1)
                        if any(a <= off < b for a, b in spans):
                            used.add(wi)
                            j += 1
                            word = Word(f"{lid}.w{j}", w.get("content", ""), float(w.get("confidence", 0.0)),
                                        _overlaps(off, span.get("length", 1), hw_spans), n, _centre(w.get("polygon")))
                            line.words.append(word)
                            ev.index[word.id] = word
                    lines.append(line)
                    ev.index[lid] = line
                ev.lines += lines
                for k, sm in enumerate(page.get("selectionMarks") or [], 1):
                    c = _centre(sm.get("polygon"))
                    near = min(lines, key=lambda L: (L.centre[0] - c[0]) ** 2 + 4 * (L.centre[1] - c[1]) ** 2,
                               default=None)
                    mark = Mark(f"{doc_id}.p{n}.s{k}", sm.get("state") == "selected", float(sm.get("confidence", 0.0)),
                                n, near.text if near else "")
                    ev.marks.append(mark)
                    ev.index[mark.id] = mark
        return ev

    @classmethod
    def from_text(cls, doc_id: str, file: str, text: str) -> "DocEvidence":
        """A text document read here (.txt .md .csv .json .xml .eml): one 'page', every line citable, confidence 1."""
        ev = cls(doc_id, file)
        ev.local, ev.page_numbers = True, [1]
        for k, raw in enumerate(str(text).splitlines(), 1):
            if not raw.strip():
                continue
            lid = f"{doc_id}.p1.l{k}"
            line = Line(lid, raw.rstrip(), 1)
            for j, tok in enumerate(raw.split(), 1):
                w = Word(f"{lid}.w{j}", tok, 1.0, False)
                line.words.append(w)
                ev.index[w.id] = w
            ev.lines.append(line)
            ev.index[lid] = line
        return ev

    @classmethod
    def snapshot(cls, doc_id: str, file: str, data) -> "DocEvidence":
        """The Register snapshot: shown whole as JSON (comparisons cite its JSON location, not line ids)."""
        ev = cls(doc_id, file)
        ev.local, ev.page_numbers = True, [1]
        ev.raw_text = json.dumps(data, ensure_ascii=False, indent=1)
        return ev

    # -- showing ------------------------------------------------------------------------------
    @property
    def pages(self) -> int:
        return len(self.page_numbers)

    def handwritten_lines(self) -> int:
        return sum(1 for L in self.lines if L.hw)

    def render(self) -> str:
        """The text the model sees: one line per read line, '[id] text', '{handwritten}' where Azure saw handwriting;
        tick boxes as '[id] [X] ticked / [ ] not ticked, beside: <nearest line>'."""
        if self.raw_text:
            return self.raw_text
        out, page = [], None
        marks = {}
        for m in self.marks:
            marks.setdefault(m.page, []).append(m)
        for L in self.lines:
            if L.page != page:
                if page is not None:
                    out += _marks(marks.get(page, []))
                page = L.page
                out.append(f"--- page {page} ---")
            out.append(f"[{L.id}] {L.text}" + ("  {handwritten}" if L.hw else ""))
        if page is not None:
            out += _marks(marks.get(page, []))
        for p in sorted(set(marks) - {L.page for L in self.lines}):
            out.append(f"--- page {p} ---")
            out += _marks(marks[p])
        return "\n".join(out)


def _marks(marks: list) -> list:
    return [f"[{m.id}] {'[X] ticked' if m.selected else '[ ] not ticked'}, beside: {m.near}" for m in marks]


# ---------------------------------------------------------------------------------------------- fields

TICKED = {"ticked", "yes", "selected", "checked", "x", "true"}
UNTICKED = {"not ticked", "no", "unselected", "unchecked", "", "false", "blank"}


def norm(text) -> str:
    """Comparison form of a value: lower case, letters/digits/@ only (spaces, punctuation and box gaps vanish)."""
    return re.sub(r"[^0-9a-z@]", "", html.unescape(str(text or "")).lower())




def derive_field(doc: DocEvidence, fdef: dict, value, citations, threshold: float) -> dict:
    """One EXTRACTED_FIELDS row.  CONFIDENCE = the lowest word confidence among the cited words that make up the
    value (tick box: the box's own confidence).  LOW_CONFIDENCE = below `threshold`, a citation that does not exist,
    a value that is not in the cited text, or an empty key field.  Signatures: presence only (signature_field).
    Format-safe corrections (account prefix, postcode, phone, date) are made after reading: VALUE is the corrected
    value, RAW_VALUE what was read, NORMALISED says what changed (a correction alone never flags a field)."""
    value = "" if value is None else str(value).strip()
    citations = [str(c).strip() for c in (citations or []) if str(c).strip()]
    row = {"FIELD_ID": f"{doc.doc_id}.{fdef['id']}", "DOC_ID": doc.doc_id, "FORM": fdef.get("_form", ""),
           "FIELD": fdef["id"], "LABEL": fdef.get("label", ""), "VALUE": value, "RAW_VALUE": value,
           "HANDWRITTEN": False, "CONFIDENCE": 1.0, "LOW_CONFIDENCE": False, "CITATIONS": citations, "REASON": "",
           "NORMALISED": ""}
    kind = fdef.get("kind", "text")
    found = [doc.index.get(c) for c in citations]
    missing = [c for c, f in zip(citations, found) if f is None or not c.startswith(doc.doc_id + ".")]
    words, marks = [], []
    for f in found:
        if isinstance(f, Line):
            words += f.words
        elif isinstance(f, Word):
            words.append(f)
        elif isinstance(f, Mark):
            marks.append(f)
    if kind == "signature":
        return signature_field(doc, fdef, row, words)
    reasons = []
    if missing:
        reasons.append("cited text not found: " + ", ".join(missing[:5]))
    blank = value == "" or (kind == "checkbox" and value.lower() in UNTICKED)
    if kind == "checkbox":
        if marks:
            ticked = norm(value) in {norm(t) for t in TICKED}
            row["CONFIDENCE"] = min(m.conf for m in marks)
            if any(m.selected != ticked for m in marks):
                reasons.append("the cited tick box does not show that")
            row["VALUE"] = "ticked" if ticked else "not ticked"
        elif words:                                     # a hand-drawn X read as text
            row["CONFIDENCE"] = min(w.conf for w in words)
            row["HANDWRITTEN"] = any(w.hw for w in words)
        elif not blank:
            reasons.append("no tick box cited")
            row["CONFIDENCE"] = 0.0
    elif blank:
        if fdef.get("key"):                             # an unreadable / crossed-out key value comes back empty
            reasons.append("key field empty or not found - check by eye")
            row["CONFIDENCE"] = 0.0
    else:
        want = norm(value)
        cited = "".join(norm(w.text) for w in words)
        if not words:
            reasons.append("no text cited for this value")
            row["CONFIDENCE"] = 0.0
        else:
            if want and want not in cited:
                reasons.append("the value is not in the cited text")
            parts = [w for w in words if norm(w.text) and norm(w.text) in want]
            pool = parts or words
            row["CONFIDENCE"] = min(w.conf for w in pool)
            row["HANDWRITTEN"] = any(w.hw for w in pool)
        fixed, note, bad = normalise(fdef, value)
        if note:
            row["VALUE"], row["NORMALISED"] = fixed, note
        if bad:
            reasons.append(bad)
    row["CONFIDENCE"] = round(float(row["CONFIDENCE"]), 3)
    if row["CONFIDENCE"] < threshold and not reasons:
        reasons.append(f"read with confidence {row['CONFIDENCE']:.2f} (below {threshold:.2f})")
    row["LOW_CONFIDENCE"] = bool(reasons)
    row["REASON"] = "; ".join(reasons)
    return row


# ---------------------------------------------------------------------------------------------- signatures

A4 = (595.28, 841.89)                   # form_maps.json rectangles are in PDF points of an A4 page
LABEL_WORDS = {"signature", "signatures", "signed", "sign", "here", "date", "print", "name", "of", "the", "and",
               "trustee", "signatory", "first", "second", "third", "correspondent", "please", "new", "authorised"}
SIGNED_CONFIDENCE = 0.95                # presence, not legibility
MAX_PRINTED_MARKS = 4                   # more printed words than this in a signature space = form text, not a signature


def area_words(doc: DocEvidence, fdef: dict) -> list:
    """Words Azure read inside the field's rectangle (on its form page), scaled from A4 points to the page's unit."""
    rect, page = fdef.get("rect"), fdef.get("page")
    if not rect or len(rect) != 4 or page not in doc.page_size:
        return []
    w, h = doc.page_size[page]
    if not w or not h:
        return []
    sx, sy = w / A4[0], h / A4[1]
    x0, y0, x1, y1 = rect[0] * sx, rect[1] * sy, rect[2] * sx, rect[3] * sy
    return [wd for L in doc.lines if L.page == page for wd in L.words
            if x0 <= wd.centre[0] <= x1 and y0 <= wd.centre[1] <= y1]


def _is_mark(word: Word, label: set) -> bool:
    if not norm(word.text):
        return False
    letters = set(re.findall(r"[a-z]+", word.text.lower()))
    return word.hw or not letters or not letters <= label


def signature_field(doc: DocEvidence, fdef: dict, row: dict, cited: list) -> dict:
    """Presence, not legibility: any mark or word in the signature space (cited by the model, or inside the field's
    rectangle) = 'signed', never flagged for a low reading confidence.  Nothing found = 'possibly not signed', always
    flagged for a human eye - never 'not signed' with full confidence."""
    label = set(re.findall(r"[a-z]+", str(fdef.get("label", "")).lower())) | LABEL_WORDS
    seen, marks = set(), []
    for w in list(cited) + area_words(doc, fdef):
        if w.id not in seen and _is_mark(w, label):
            seen.add(w.id)
            marks.append(w)
    printed = [w for w in marks if not w.hw]
    if len(printed) > MAX_PRINTED_MARKS:              # running printed text (e.g. a page shifted by a missing page)
        marks = [w for w in marks if w.hw]
    if marks:
        row.update(VALUE="signed", CONFIDENCE=SIGNED_CONFIDENCE, LOW_CONFIDENCE=False,
                   HANDWRITTEN=any(w.hw for w in marks), CITATIONS=[w.id for w in marks],
                   REASON=f"presence: {len(marks)} mark(s) in the signature space (legibility not checked)")
    else:
        row.update(VALUE="possibly not signed", CONFIDENCE=0.0, LOW_CONFIDENCE=True, HANDWRITTEN=False,
                   CITATIONS=[], REASON="no marks found in the signature space - check by eye")
    return row


# ---------------------------------------------------------------------------------------------- format-safe fixes

TO_LETTER = {"0": "O", "8": "B", "5": "S", "1": "I", "2": "Z", "6": "G"}
TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "|": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
PHONE_DIGIT = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}
DATE_DIGIT = {"I": "1", "l": "1", "|": "1", "/": "1", "O": "0", "o": "0"}


def field_type(fdef: dict) -> str:
    text = (str(fdef.get("id", "")) + " " + str(fdef.get("label", ""))).lower().replace("_", " ")
    if "postcode" in text or "post code" in text:
        return "postcode"
    if any(w in text for w in ("phone", "telephone", "mobile")):
        return "phone"
    if fdef.get("kind") == "date" or re.search(r"\bdate\b", text):
        return "date"
    if ("account" in text and ("number" in text or " no" in text)) or "client id" in text:
        return "account"
    return ""


def normalise(fdef: dict, value: str) -> tuple:
    """(value, note of what changed, problem) - only what the format itself decides; anything else is left alone.
    Account (user rule): 2 characters + exactly 7 digits, the 2 characters made letters by O<-0 B<-8 S<-5 I<-1 Z<-2
    G<-6; digits only (CO printed on the form): exactly 7 digits.  Postcode: the inward code is digit + 2 letters and
    the outward code starts with a letter; one space between.  Phone: digits only.  Date: dd/mm/yyyy."""
    kind = field_type(fdef)
    if kind == "account":
        v = re.sub(r"\s+", "", value).upper()
        if len(v) == 9 and v[2:].isdigit():
            prefix = "".join(TO_LETTER.get(c, c) for c in v[:2])
            if prefix.isalpha() and prefix + v[2:] != value:
                return prefix + v[2:], f"account read as '{value}', prefix format fixed", ""
        elif len(v) == 7 and not v.isdigit() and sum(c.isdigit() for c in v) >= 5:
            d = "".join(TO_DIGIT.get(c, c) for c in v)
            if d.isdigit():
                return d, f"account read as '{value}', digit format fixed", ""
        return value, "", ""
    if kind == "postcode":
        v = re.sub(r"\s+", "", value).upper()
        if 5 <= len(v) <= 7:
            out, inn = v[:-3], v[-3:]
            inn = TO_DIGIT.get(inn[0], inn[0]) + "".join(TO_LETTER.get(c, c) for c in inn[1:])
            out = TO_LETTER.get(out[0], out[0]) + out[1:]
            new = f"{out} {inn}"
            if inn[0].isdigit() and inn[1:].isalpha() and out[0].isalpha() and new != value:
                return new, f"postcode read as '{value}', format fixed", ""
        return value, "", ""
    if kind == "phone":
        v = value.strip()
        lead = "+" if v.startswith("+") else ""
        d = lead + "".join(PHONE_DIGIT.get(c, c) for c in v.lstrip("+") if c not in " -().")
        if d.lstrip("+").isdigit() and len(d) >= 7 and d != value:
            return d, f"phone read as '{value}', compared as digits only", ""
        return value, "", ""
    if kind == "date":
        v = re.sub(r"\s+", "", value)
        if len(v) == 10 and v[2] in "/.-" and v[5] in "/.-":
            v = "".join("/" if i in (2, 5) else DATE_DIGIT.get(c, c) for i, c in enumerate(v))
        try:
            datetime.datetime.strptime(v, "%d/%m/%Y")
        except ValueError:
            return value, "", "not a valid dd/mm/yyyy date - check by eye"
        return (v, f"date read as '{value}', format fixed", "") if v != value else (value, "", "")
    return value, "", ""
