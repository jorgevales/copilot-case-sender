"""The results workbook for the human reviewers (stdlib only: an .xlsx is a zip of XML files, nothing to install).

  RUNTIME_ROOT\\reports\\Copilot review results.xlsx     this run's valid Copilot answers, rewritten after every answer

Four tabs:
  Summary     totals by result, discrepancy type, Sub Worktype and issue type
  Cases       ONE row per case: result, headline, every issue as its own numbered '{field}: {discrepancy}' paragraph
  Issues      one row per red / yellow item (no match, could not compare, rule not satisfied / not determinable), the
              reviewers' to-do list; its ID links back to the case's row in Cases
  All checks  every comparison and rule check (audit), a thick line above each new case
Colours: red = no match / Not satisfied / Incorrect, yellow = could not compare / Not determinable / Inconclusive,
green = match / Satisfied / Correct, grey = Not applicable.  Empty reviewer columns are for the people who check.

At the start of a run an older workbook is renamed 'Copilot review results - older <its time>.xlsx' (`make_room`;
when Excel holds it open, the run waits for Enter and tries again).  During the run a workbook opened in Excel is
never waited for: the write is retried after the next answer, and if it is still open at the end a time-stamped
copy is written instead (`ReviewWorkbook.close` returns its path).
"""
from __future__ import annotations

import collections
import datetime
import io
import os
import re
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

NAME = "Copilot review results.xlsx"
WRAP = ' wrapText="1"'
SELECTED = ' tabSelected="1"'          # the tab Excel opens on
BAD_XML = re.compile("[" + re.escape("".join(map(chr, [*range(0, 9), 11, 12, *range(14, 32), 0xFFFE, 0xFFFF]))) + "]")
MAX_CELL = 32767                                      # Excel's limit per cell
HEAD = "D9E1F2"
RED, YELLOW, GREEN, GREY = "FFC7CE", "FFEB9C", "C6EFCE", "E7E6E6"
COLOUR = {"No match": RED, "Could not compare": YELLOW, "Match": GREEN, "Not satisfied": RED,
          "Not determinable": YELLOW, "Satisfied": GREEN, "Not applicable": GREY,
          "Incorrect": RED, "Inconclusive": YELLOW, "Correct": GREEN}
ISSUE_WORDS = ("No match", "Could not compare", "Not satisfied", "Not determinable")
FIXED_PHRASES = ("Blank on Register", "Not available in Register snapshot", "Not captured", "Not retrieved")
REVIEWER = [("Reviewer agrees (Y/N)", 12), ("Reviewer correction", 30), ("Reviewer notes", 30)]


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = BAD_XML.sub("", str(value))
    return text if len(text) <= MAX_CELL else text[:MAX_CELL - 40] + " ... (cut: see the Issues tab)"


def outcome_of(match) -> str:
    return "Match" if match is True else "No match" if match is False else "Could not compare"


def issues_of(result: dict) -> list:
    """The case's red and yellow items in order: (kind, field or rule, '{field}: {discrepancy}', comparison row or None,
    rule row or None).  The field name comes from the row itself, the discrepancy from Copilot's EXPLANATION / NOTE."""
    out = []
    for r in result.get("A_B_COMPARISONS") or []:
        kind = outcome_of(r.get("MATCH"))
        if kind != "Match":
            out.append((kind, _text(r.get("ITEM")), f"{_text(r.get('ITEM'))}: {_text(r.get('EXPLANATION'))}", r, None))
    for r in result.get("RULE_CHECKS") or []:
        if r.get("STATUS") in ("Not satisfied", "Not determinable"):
            out.append((r["STATUS"], _text(r.get("RULE")), f"{_text(r.get('RULE'))}: {_text(r.get('NOTE'))}", None, r))
    return out


def issue_details(result: dict) -> str:
    """Every issue as its own numbered paragraph, a blank line between them ('No issues' when there are none)."""
    items = issues_of(result)
    return "\n\n".join(f"{n}. [{kind}] {line}" for n, (kind, _, line, _, _) in enumerate(items, 1)) or "No issues"


# ---------------------------------------------------------------------------------------------- the sheets

class Sheet:
    """Rows of (text, style key) cells; style key = (bold, fill, wrap, thick top border, link)."""

    def __init__(self, name: str, widths: list, frozen: bool = True, filter_: bool = True, lists=None):
        self.name, self.widths, self.frozen, self.filter = name, widths, frozen, filter_
        self.rows, self.links, self.lists = [], [], lists or {}     # lists: column number -> "A,B,C"

    def add(self, values, fill="", wrap=False, bold=False, thick=False, fills=None) -> int:
        fills = fills or {}
        self.rows.append([(_text(v), (bold, fills.get(i, fill), wrap, thick, False)) for i, v in enumerate(values)])
        return len(self.rows)

    def link(self, row: int, col: int, sheet: str, target_row: int):
        text, (bold, fill, wrap, thick, _) = self.rows[row - 1][col - 1]
        self.rows[row - 1][col - 1] = (text, (bold, fill, wrap, thick, True))
        self.links.append((f"{column_letter(col)}{row}", f"'{sheet}'!A{target_row}"))


CASE_HEAD = (["Instruction ID", "Client Account Number", "Sub Worktype", "Copilot result", "Discrepancy type", "Headline",
              "Issues", "Issue details (one per field)", "Client request", "Justification", "Comparisons", "Matches",
              "No match", "Could not compare", "Rule checks", "Not satisfied", "Not determinable",
              "Case end date/time (UK)", "Register snapshot date/time", "Business days gap", "Case age statement",
              "Client Number"] + [h for h, _ in REVIEWER] + ["Reviewer corrected result"])
CASE_WIDTHS = [34, 16, 22, 13, 22, 40, 8, 70, 45, 60, 11, 9, 9, 11, 10, 11, 12, 20, 26, 10, 30, 14, 12, 30, 30, 16]
ISSUE_HEAD = (["Instruction ID", "Sub Worktype", "Copilot result", "Issue no.", "Type", "Field or rule",
               "Discrepancy ({field}: {discrepancy})", "Customer expectation", "Customer source", "Register value",
               "Register source", "Evidence"] + [h for h, _ in REVIEWER])
ISSUE_WIDTHS = [34, 22, 13, 8, 17, 30, 70, 26, 32, 26, 32, 32] + [w for _, w in REVIEWER]
CHECK_HEAD = ["Instruction ID", "Client Account Number", "Sub Worktype", "Copilot result", "Type", "What was checked",
              "Customer expectation", "Customer source", "Register value", "Register source", "Outcome",
              "Explanation / note", "Evidence"]
CHECK_WIDTHS = [34, 16, 22, 13, 11, 32, 28, 32, 28, 32, 16, 60, 32]
SHEETS = ("Summary", "Cases", "Issues", "All checks")


def build(cases: list) -> list:
    """The four sheets from [(info, result)].  info: iid, account, sub_worktype, client_number, gap, age."""
    summary = Sheet("Summary", [46, 14, 12, 12, 12], frozen=False, filter_=False)
    cases_sh = Sheet("Cases", CASE_WIDTHS, lists={1 + CASE_HEAD.index("Reviewer agrees (Y/N)"): "Y,N",
                                                   1 + CASE_HEAD.index("Reviewer corrected result"): "Correct,Incorrect,Inconclusive"})
    issues_sh = Sheet("Issues", ISSUE_WIDTHS, lists={1 + ISSUE_HEAD.index("Reviewer agrees (Y/N)"): "Y,N"})
    checks_sh = Sheet("All checks", CHECK_WIDTHS)
    for sh, head in ((cases_sh, CASE_HEAD), (issues_sh, ISSUE_HEAD), (checks_sh, CHECK_HEAD)):
        sh.add(head, fill=HEAD, wrap=True, bold=True)
    totals, kinds, subs = collections.Counter(), collections.Counter(), {}
    issue_types, phrases = collections.Counter(), collections.Counter()
    for info, result in cases:
        verdict, kind = result.get("COPILOT_RESULT", ""), result.get("DISCREPANCY_TYPE", "")
        rows, rules = result.get("A_B_COMPARISONS") or [], result.get("RULE_CHECKS") or []
        items = issues_of(result)
        flags, statuses = [r.get("MATCH") for r in rows], [r.get("STATUS") for r in rules]
        totals[verdict] += 1
        kinds[kind] += 1
        subs.setdefault(info.get("sub_worktype") or "(unknown)", collections.Counter())[verdict] += 1
        issue_types.update(k for k, *_ in items)
        phrases.update(_text(r.get("System_data")) for r in rows if r.get("MATCH") is not True)
        case_row = cases_sh.add(
            [info.get("iid"), info.get("account"), info.get("sub_worktype"), verdict, kind, result.get("HEADLINE"),
             len(items), issue_details(result), result.get("CLIENT_REQUEST"), result.get("JUSTIFICATION"), len(rows),
             flags.count(True), flags.count(False), flags.count(None), len(rules), statuses.count("Not satisfied"),
             statuses.count("Not determinable"), result.get("CASE_END_DATETIME"), result.get("REGISTER_SNAPSHOT_DATETIME"),
             info.get("gap"), info.get("age"), info.get("client_number"), "", "", "", ""],
            wrap=True, fills={3: COLOUR.get(verdict, "")})
        for n, (k, name, line, comp, rule) in enumerate(items, 1):
            src = comp or {}
            at = issues_sh.add([info.get("iid"), info.get("sub_worktype"), verdict, n, k, name, line,
                                src.get("Customer_expectation"), src.get("CUSTOMER_SOURCE"), src.get("System_data"),
                                src.get("SYSTEM_SOURCE"), (rule or {}).get("EVIDENCE"), "", "", ""],
                               fill=COLOUR.get(k, ""), wrap=True, thick=n == 1)
            issues_sh.link(at, 1, "Cases", case_row)
        first = True
        for r in rows:
            out = outcome_of(r.get("MATCH"))
            checks_sh.add([info.get("iid"), info.get("account"), info.get("sub_worktype"), verdict, "Comparison",
                           r.get("ITEM"), r.get("Customer_expectation"), r.get("CUSTOMER_SOURCE"), r.get("System_data"),
                           r.get("SYSTEM_SOURCE"), out, r.get("EXPLANATION"), ""], fill=COLOUR.get(out, ""), wrap=True,
                          thick=first)
            first = False
        for r in rules:
            checks_sh.add([info.get("iid"), info.get("account"), info.get("sub_worktype"), verdict, "Rule check",
                           r.get("RULE"), "", "", "", "", r.get("STATUS"), r.get("NOTE"), r.get("EVIDENCE")],
                          fill=COLOUR.get(r.get("STATUS"), ""), wrap=True, thick=first)
            first = False
    summary.add(["Copilot review results", f"{datetime.datetime.now():%d/%m/%Y %H:%M}"], bold=True)
    summary.add([])
    summary.add(["Cases with a valid Copilot answer", len(cases)], bold=True)
    for word in ("Correct", "Incorrect", "Inconclusive"):
        summary.add([word, totals[word]], fills={0: COLOUR[word]})
    summary.add([])
    summary.add(["Discrepancy type", "Cases"], bold=True, fill=HEAD)
    for word in ("Potential processor error", "Discrepancy from aged case", "None"):
        summary.add([word, kinds[word]])
    summary.add([])
    summary.add(["Sub Worktype", "Cases", "Correct", "Incorrect", "Inconclusive"], bold=True, fill=HEAD)
    for sub in sorted(subs, key=str.casefold):
        c = subs[sub]
        summary.add([sub, sum(c.values()), c["Correct"], c["Incorrect"], c["Inconclusive"]])
    summary.add([])
    summary.add(["Issue type (all cases, see the Issues tab)", "Items"], bold=True, fill=HEAD)
    for word in ISSUE_WORDS:
        summary.add([word, issue_types[word]], fills={0: COLOUR[word]})
    summary.add([])
    summary.add(["Register value of the comparisons that did not match", "Items"], bold=True, fill=HEAD)
    for phrase in FIXED_PHRASES:
        summary.add([phrase, phrases[phrase]])
    summary.add(["(a real Register value)", sum(n for p, n in phrases.items() if p not in FIXED_PHRASES)])
    return [summary, cases_sh, issues_sh, checks_sh]


# ---------------------------------------------------------------------------------------------- the .xlsx file

def column_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


class Styles:
    """cellXfs made on demand: one per (bold, fill, wrap, thick top border, link) combination."""

    def __init__(self):
        self.fills = ["", "gray125"]                # Excel's two required fills
        self.keys = [None]                          # xf 0 = default

    def index(self, key) -> int:
        if key not in self.keys:
            self.keys.append(key)
            if key[1] and key[1] not in self.fills:
                self.fills.append(key[1])
        return self.keys.index(key)

    def xml(self) -> str:
        fills = "".join('<fill><patternFill patternType="none"/></fill>' if f == "" else
                        '<fill><patternFill patternType="gray125"/></fill>' if f == "gray125" else
                        f'<fill><patternFill patternType="solid"><fgColor rgb="FF{f}"/><bgColor indexed="64"/></patternFill></fill>'
                        for f in self.fills)
        thin = '<{0} style="thin"><color rgb="FFBFBFBF"/></{0}>'
        sides = thin.format("left") + thin.format("right")
        borders = ('<border><left/><right/><top/><bottom/><diagonal/></border>'
                   f'<border>{sides}{thin.format("top")}{thin.format("bottom")}<diagonal/></border>'
                   f'<border>{sides}<top style="medium"><color rgb="FF404040"/></top>{thin.format("bottom")}<diagonal/></border>')
        xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
        for bold, fill, wrap, thick, link in self.keys[1:]:
            xfs.append(f'<xf numFmtId="0" fontId="{2 if link else 1 if bold else 0}" '
                       f'fillId="{self.fills.index(fill) if fill else 0}" borderId="{2 if thick else 1}" xfId="0" '
                       'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
                       f'<alignment vertical="top"{WRAP if wrap else ""}/></xf>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="3"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
                '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
                '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/><family val="2"/></font></fonts>'
                f'<fills count="{len(self.fills)}">{fills}</fills><borders count="3">{borders}</borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def _cell(ref: str, text: str, style: int) -> str:
    if not text:
        return f'<c r="{ref}" s="{style}"/>'
    if re.fullmatch(r"-?[0-9]{1,9}", text):          # counts stay numbers (sortable)
        return f'<c r="{ref}" s="{style}"><v>{text}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _last(sh: Sheet) -> str:
    return f"{column_letter(max([len(r) for r in sh.rows] + [len(sh.widths), 1]))}{max(len(sh.rows), 1)}"


def sheet_xml(sh: Sheet, styles: Styles) -> str:
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           f'<dimension ref="A1:{_last(sh)}"/><sheetViews><sheetView{SELECTED if sh.name == "Cases" else ""} '
           'workbookViewId="0">']
    if sh.frozen:                                     # the header row stays in view
        xml.append('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft"/>')
    xml.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>')
    xml += [f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(sh.widths, 1)]
    xml.append("</cols><sheetData>")
    for n, row in enumerate(sh.rows, 1):
        xml.append(f'<row r="{n}">')
        xml += [_cell(f"{column_letter(i)}{n}", text, styles.index(key)) for i, (text, key) in enumerate(row, 1)]
        xml.append("</row>")
    xml.append("</sheetData>")
    if sh.filter and len(sh.rows) > 1:
        xml.append(f'<autoFilter ref="A1:{_last(sh)}"/>')
    if sh.lists:
        xml.append(f'<dataValidations count="{len(sh.lists)}">')
        for col, options in sh.lists.items():
            c = column_letter(col)
            xml.append(f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{c}2:{c}5000">'
                       f'<formula1>"{options}"</formula1></dataValidation>')
        xml.append("</dataValidations>")
    if sh.links:
        xml.append("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" location="{escape(loc)}"/>' for ref, loc in sh.links)
                   + "</hyperlinks>")
    xml.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')
    return "".join(xml)


def xlsx_bytes(cases: list) -> bytes:
    """The whole workbook from [(info, result)]; it opens on the Cases tab."""
    sheets, styles = build(cases), Styles()
    bodies = [sheet_xml(sh, styles) for sh in sheets]         # first: registers every style the sheets use
    n = len(sheets)
    names = "".join(f'<sheet name="{sh.name}" sheetId="{i}" r:id="rId{i}"/>' for i, sh in enumerate(sheets, 1))
    book = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<bookViews><workbookView activeTab="1"/></bookViews><sheets>{names}</sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                      f'worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
            + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
              'styles" Target="styles.xml"/></Relationships>')
    types_ = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-'
                        'officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, n + 1))
              + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
              '</Types>')
    root = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types_)
        z.writestr("_rels/.rels", root)
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles.xml())
        for i, body in enumerate(bodies, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", body)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------- the file on disk

def _stamp(moment: float | None = None) -> str:
    when = datetime.datetime.fromtimestamp(moment) if moment is not None else datetime.datetime.now()
    return when.strftime("%Y-%m-%d %H%M%S")


def _free(path: Path) -> Path:
    """`path`, or '<name> (2).xlsx', '(3)' ... when it is already taken."""
    n, out = 2, path
    while out.exists():
        out = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return out


def set_aside(folder) -> Path | None:
    """Rename an older 'Copilot review results.xlsx' to '... - older <the time it was made>.xlsx'.  None when there is
    none.  PermissionError when Windows refuses (Excel has it open)."""
    path = Path(folder) / NAME
    if not path.exists():
        return None
    st = path.stat()
    made = getattr(st, "st_birthtime", None) or st.st_mtime
    target = _free(path.with_name(f"{path.stem} - older {_stamp(made)}{path.suffix}"))
    os.rename(path, target)
    return target


def make_room(folder, wait=input, say=print) -> Path | None:
    """Start of a run: set the older workbook aside.  While Excel holds it open: say so, wait for Enter, try again."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    while True:
        try:
            moved = set_aside(folder)
        except PermissionError:
            say(f"\n'{NAME}' is open (probably in Excel), so it cannot be renamed before this run.\n"
                "Close it in Excel, then press Enter to try again (Ctrl+C stops).")
            try:
                wait("")
            except EOFError:
                raise RuntimeError(f"'{NAME}' is open in Excel and there is no console to wait on - close it and run again") from None
            continue
        if moved:
            say(f"Results workbook from an earlier run renamed to: {moved.name}")
        return moved


class ReviewWorkbook:
    """This run's results workbook, rewritten whole (temp file + one replace) after every valid answer."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / NAME
        self.cases = []                   # [(info, result)] in the order the answers arrived
        self.dirty = False                # an answer is not in the file yet (Excel had it open)

    def add(self, info: dict, result: dict, working: str = "") -> bool:
        """Add one case's valid answer and write the file.  False = not written now (open in Excel; retried later)."""
        self.cases.append((info, result))
        self.dirty = True
        return self.write()

    def write(self, path: Path | None = None) -> bool:
        target = path or self.path
        tmp = target.with_name(f".{target.stem}.tmp-{uuid.uuid4().hex[:8]}.xlsx")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(xlsx_bytes(self.cases))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except PermissionError:
            return False
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if target == self.path:
            self.dirty = False
        return True

    def close(self) -> Path | None:
        """End of the run: the workbook's final path (None when no answer arrived).  Still open in Excel = a
        time-stamped copy is written beside it instead."""
        if not self.cases:
            return None
        if not self.dirty or self.write():
            return self.path
        copy = _free(self.path.with_name(f"{self.path.stem} - {_stamp()}{self.path.suffix}"))
        return copy if self.write(copy) else None
