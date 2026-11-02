"""The results workbook for the human reviewers (stdlib only: an .xlsx is a zip of XML files, nothing to install).

  RUNTIME_ROOT\\reports\\Copilot review results.xlsx     this run's valid Copilot answers, rewritten after every answer

One row per comparison and per rule check; the case's own values (ID, account, result, justification, timing ...)
are repeated on each of its rows so Excel's filter and sort work.  Rows are tinted: red = no match / Not satisfied,
yellow = could not compare / Not determinable, green = match / Satisfied, grey = Not applicable.  Empty reviewer
columns at the end are for the people who check Copilot's work.

At the start of a run an older workbook is renamed 'Copilot review results - older <its time>.xlsx' (`make_room`;
when Excel holds it open, the run waits for Enter and tries again).  During the run a workbook opened in Excel is
never waited for: the write is retried after the next answer, and if it is still open at the end a time-stamped
copy is written instead (`ReviewWorkbook.close` returns its path).
"""
from __future__ import annotations

import datetime
import io
import os
import re
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

NAME = "Copilot review results.xlsx"
SHEET = "Results"

# (header, width, wrap) - case values first, then the row's own values, then timing, the Working text, reviewer columns.
CASE_COLUMNS = [("Instruction ID", 34, False), ("Client Account Number", 16, False), ("Sub Worktype", 22, True),
                ("Copilot result", 13, False), ("Discrepancy type", 22, True), ("Needs attention", 45, True),
                ("Client request", 45, True), ("Justification", 60, True)]
ROW_COLUMNS = [("Type", 11, False), ("What was checked", 32, True), ("Customer expectation", 28, True),
               ("Customer source", 32, True), ("Register value", 28, True), ("Register source", 32, True),
               ("Outcome", 16, False), ("Evidence", 32, True), ("Note", 32, True)]
TAIL_COLUMNS = [("Case end date/time (UK)", 20, False), ("Register snapshot date/time", 26, False),
                ("Business days gap", 10, False), ("Case age statement", 30, True), ("Client Number", 14, False),
                ("Working section", 60, False)]      # up to 25 lines: not wrapped, read it in the formula bar
REVIEWER_COLUMNS = [("Reviewer agrees (Y/N)", 12, False), ("Reviewer correction", 32, True),
                    ("Reviewer notes", 32, True), ("Reviewer corrected result", 16, False)]
COLUMNS = CASE_COLUMNS + ROW_COLUMNS + TAIL_COLUMNS + REVIEWER_COLUMNS
HEADERS = [c[0] for c in COLUMNS]

# Fill per outcome (styles.xml fill index): 0 none, 3 red, 4 yellow, 5 green, 6 grey.
FILL_OF = {"No match": 3, "Could not compare": 4, "Match": 5,
           "Not satisfied": 3, "Not determinable": 4, "Satisfied": 5, "Not applicable": 6}
DATA_FILLS = (0, 3, 4, 5, 6)
WRAP = ' wrapText="1"'
BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
MAX_CELL = 32767                                      # Excel's limit per cell


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = BAD_XML.sub("", str(value))
    return text if len(text) <= MAX_CELL else text[:MAX_CELL - 20] + " ... (cut by Excel)"


def outcome_of(match) -> str:
    return "Match" if match is True else "No match" if match is False else "Could not compare"


def needs_attention(result: dict) -> str:
    """The case's red and yellow items, one per line ('Nothing flagged' when there are none)."""
    lines = []
    for r in result.get("A_B_COMPARISONS") or []:
        if r.get("MATCH") is False:
            lines.append(f"No match: {_text(r.get('ITEM'))} (expected: {_text(r.get('Customer_expectation'))}; "
                         f"Register: {_text(r.get('System_data'))})")
        elif r.get("MATCH") is None:
            lines.append(f"Could not compare: {_text(r.get('ITEM'))}")
    for r in result.get("RULE_CHECKS") or []:
        if r.get("STATUS") in ("Not satisfied", "Not determinable"):
            lines.append(f"{r['STATUS']}: {_text(r.get('RULE'))}")
    return "\n".join(lines) or "Nothing flagged"


def case_rows(info: dict, result: dict, working: str = "") -> list:
    """The workbook rows of ONE case: (values in HEADERS order, outcome) per comparison, then per rule check.
    `info`: iid, account, sub_worktype, client_number, gap, age (what the script knows)."""
    case = [info.get("iid"), info.get("account"), info.get("sub_worktype"), result.get("COPILOT_RESULT"),
            result.get("DISCREPANCY_TYPE"), needs_attention(result), result.get("CLIENT_REQUEST"),
            result.get("JUSTIFICATION")]
    tail = [result.get("CASE_END_DATETIME"), result.get("REGISTER_SNAPSHOT_DATETIME"), info.get("gap"), info.get("age"),
            info.get("client_number"), working]
    blank_reviewer = [""] * len(REVIEWER_COLUMNS)
    rows = []
    for r in result.get("A_B_COMPARISONS") or []:
        outcome = outcome_of(r.get("MATCH"))
        own = ["Comparison", r.get("ITEM"), r.get("Customer_expectation"), r.get("CUSTOMER_SOURCE"), r.get("System_data"),
               r.get("SYSTEM_SOURCE"), outcome, "", ""]
        rows.append(([_text(v) for v in case + own + tail] + blank_reviewer, outcome))
    for r in result.get("RULE_CHECKS") or []:
        own = ["Rule check", r.get("RULE"), "", "", "", "", r.get("STATUS"), r.get("EVIDENCE"), r.get("NOTE")]
        rows.append(([_text(v) for v in case + own + tail] + blank_reviewer, r.get("STATUS")))
    return rows


# ---------------------------------------------------------------------------------------------- the .xlsx file

def column_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def _style(fill: int, wrap: bool) -> int:
    """cellXfs index: 0 default, 1 header, then one style per (data fill, wrap)."""
    return 2 + DATA_FILLS.index(fill) * 2 + (0 if wrap else 1)


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
          '<fills count="7"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          + "".join(f'<fill><patternFill patternType="solid"><fgColor rgb="FF{c}"/><bgColor indexed="64"/></patternFill></fill>'
                    for c in ("D9E1F2", "FFC7CE", "FFEB9C", "C6EFCE", "E7E6E6")) +
          '</fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
          '<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right>'
          '<top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>'
          '</borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          f'<cellXfs count="{2 + 2 * len(DATA_FILLS)}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" '
          'applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
          + "".join(f'<xf numFmtId="0" fontId="0" fillId="{f}" borderId="1" xfId="0" applyFill="1" applyBorder="1" '
                    f'applyAlignment="1"><alignment vertical="top"{WRAP if wrap else ""}/></xf>'
                    for f in DATA_FILLS for wrap in (True, False)) +
          '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                 '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                 '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                 '</Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
             '</Relationships>')
WORKBOOK_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                 '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                 '</Relationships>')


def _cell(ref: str, text: str, style: int) -> str:
    if not text:
        return f'<c r="{ref}" s="{style}"/>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def xlsx_bytes(rows: list) -> bytes:
    """The whole workbook: one sheet, header row frozen (and the ID column), a filter on every column, a Y/N list in
    'Reviewer agrees' and a result list in 'Reviewer corrected result'.  `rows` = case_rows() output, all cases."""
    last_col, last_row = column_letter(len(HEADERS)), len(rows) + 1
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           f'<dimension ref="A1:{last_col}{last_row}"/>'
           '<sheetViews><sheetView workbookViewId="0"><pane xSplit="1" ySplit="1" topLeftCell="B2" activePane="bottomRight" '
           'state="frozen"/><selection pane="bottomRight" activeCell="B2" sqref="B2"/></sheetView></sheetViews>'
           '<sheetFormatPr defaultRowHeight="15"/><cols>']
    xml += [f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, (_, w, _) in enumerate(COLUMNS, 1)]
    xml.append('</cols><sheetData><row r="1">')
    xml += [_cell(f"{column_letter(i)}1", h, 1) for i, h in enumerate(HEADERS, 1)]
    xml.append("</row>")
    for n, (values, outcome) in enumerate(rows, 2):
        fill = FILL_OF.get(outcome, 0)
        xml.append(f'<row r="{n}">')
        xml += [_cell(f"{column_letter(i)}{n}", v, _style(fill, COLUMNS[i - 1][2])) for i, v in enumerate(values, 1)]
        xml.append("</row>")
    xml.append(f'</sheetData><autoFilter ref="A1:{last_col}{last_row}"/>')
    agree = column_letter(HEADERS.index("Reviewer agrees (Y/N)") + 1)
    corrected = column_letter(HEADERS.index("Reviewer corrected result") + 1)
    xml.append('<dataValidations count="2">'
               f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{agree}2:{agree}5000">'
               '<formula1>"Y,N"</formula1></dataValidation>'
               f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{corrected}2:{corrected}5000">'
               '<formula1>"Correct,Incorrect,Inconclusive"</formula1></dataValidation></dataValidations>'
               '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')
    book = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<bookViews><workbookView/></bookViews><sheets><sheet name="{SHEET}" sheetId="1" r:id="rId1"/></sheets>'
            f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"{SHEET}!$A$1:${last_col}${last_row}</definedName></definedNames></workbook>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        z.writestr("xl/styles.xml", STYLES)
        z.writestr("xl/worksheets/sheet1.xml", "".join(xml))
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
        self.cases = []                   # [rows of one case] in the order the answers arrived
        self.dirty = False                # an answer is not in the file yet (Excel had it open)

    def add(self, info: dict, result: dict, working: str = "") -> bool:
        """Add one case's valid answer and write the file.  False = not written now (open in Excel; retried later)."""
        self.cases.append(case_rows(info, result, working))
        self.dirty = True
        return self.write()

    def write(self, path: Path | None = None) -> bool:
        target = path or self.path
        rows = [row for case in self.cases for row in case]
        tmp = target.with_name(f".{target.stem}.tmp-{uuid.uuid4().hex[:8]}.xlsx")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(xlsx_bytes(rows))
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
