"""The BAM Report: the Excel file (.xlsx) you give every run - the list of cases (stdlib only, nothing to install).

  - Only the FIRST sheet is read (the report); the other sheets are ignored.
  - Rows 1-4 are a title block and are skipped: the column headers are always in row 5, the cases start in row 6.
  - Columns are found by their HEADER NAME (case, spaces and punctuation ignored), wherever they are.  The report
    normally has 31 columns; another count is only a warning, as long as every needed header is there.

  Needed headers (the run stops and names any that is missing):
      RequestId               the instruction ID (32 characters)
      Client Account Number   the account the case is about
      Process Status          'Ended' = reviewed; 'Voided' (and anything else) = excluded automatically, listed at the end
      Sub Worktype            the case type
  Used when present (a missing one is a warning: its run choice or detail is then not available):
      Received Date, Client Name, Client Number, Fund, Fund Type, Source (Email / Post / blank), Email Subject, End Date

  Date columns are real Excel date-times; they are given on as text '25/11/2026 12:23 PM' (the case-timing format).
"""
from __future__ import annotations

import datetime
import re
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

HEADER_ROW = 5                       # rows 1-4 are the report's title block
EXPECTED_COLUMNS = 31
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# key -> (header as written in the report, the other spellings accepted)
COLUMNS = {
    "iid": ("RequestId", ("requestid", "instructionid")),
    "received": ("Received Date", ("receiveddate",)),
    "account": ("Client Account Number", ("clientaccountnumber",)),
    "client_name": ("Client Name", ("clientname",)),
    "client": ("Client Number", ("clientnumber",)),
    "fund": ("Fund", ("fund",)),
    "fund_type": ("Fund Type", ("fundtype",)),
    "status": ("Process Status", ("processstatus",)),
    "source": ("Source", ("source",)),
    "subject": ("Email Subject", ("emailsubject",)),
    "sub": ("Sub Worktype", ("subworktype",)),
    "end": ("End Date", ("enddate",)),
}
NEEDED = ("iid", "account", "status", "sub")
DATE_KEYS = ("received", "end")
SHOWN_FORMAT = "%d/%m/%Y %I:%M %p"   # the same text shared/timing.py reads


class ReportError(Exception):
    """The BAM Report cannot be used at all (the message is safe to show)."""


def norm_head(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


# ---------------------------------------------------------------------------------------------- reading the .xlsx

def _col_index(ref: str) -> int:
    """'A5' -> 0, 'AE5' -> 30."""
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n - 1


def _first_sheet(z: zipfile.ZipFile) -> str:
    """The zip path of the workbook's FIRST sheet (in the workbook's own order, not by file name)."""
    book = ET.fromstring(z.read("xl/workbook.xml"))
    first = book.find("m:sheets/m:sheet", NS)
    if first is None:
        raise ReportError("the Excel file has no sheet")
    rid = first.get(REL_ID)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("pr:Relationship", NS):
        if rel.get("Id") == rid:
            target = rel.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return str(PurePosixPath("xl") / target)
    raise ReportError("the Excel file's first sheet could not be found")


def _shared_strings(z: zipfile.ZipFile) -> list:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")) for si in root.findall("m:si", NS)]


def _date_styles(z: zipfile.ZipFile) -> set:
    """Indexes of the cell styles that show a date (built-in date formats or a custom one with d/m/y)."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom = {}
    for f in root.findall("m:numFmts/m:numFmt", NS):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", f.get("formatCode", "")).lower()
        custom[int(f.get("numFmtId", "0"))] = bool(re.search(r"[dy]", code))
    out = set()
    xfs = root.find("m:cellXfs", NS)
    for i, xf in enumerate(xfs.findall("m:xf", NS) if xfs is not None else []):
        fid = int(xf.get("numFmtId", "0"))
        if 14 <= fid <= 22 or 45 <= fid <= 47 or custom.get(fid):
            out.add(i)
    return out


def read_sheet(path) -> list:
    """Every row of the first sheet as a list of cell values (text, float, or a datetime for a date cell); empty cells
    are '' and a row keeps its column positions."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ReportError("the file is not a readable .xlsx Excel file (save it as 'Excel Workbook (*.xlsx)')") from None
    with z:
        try:
            strings, dates = _shared_strings(z), _date_styles(z)
            root = ET.fromstring(z.read(_first_sheet(z)))
        except (KeyError, ET.ParseError):
            raise ReportError("the Excel file is damaged or not a normal workbook") from None
    rows = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        n = int(row.get("r", len(rows) + 1))
        while len(rows) < n - 1:
            rows.append([])
        cells = []
        for c in row.findall("m:c", NS):
            i = _col_index(c.get("r", "")) if c.get("r") else len(cells)
            while len(cells) < i:
                cells.append("")
            kind, v = c.get("t", "n"), c.find("m:v", NS)
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            elif v is None or v.text is None:
                value = ""
            elif kind == "s":
                value = strings[int(v.text)] if int(v.text) < len(strings) else ""
            elif kind in ("str", "e"):
                value = v.text
            elif kind == "b":
                value = "TRUE" if v.text == "1" else "FALSE"
            else:
                try:
                    num = float(v.text)
                except ValueError:
                    value = v.text
                else:
                    value = serial_to_datetime(num) if int(c.get("s", "0")) in dates else num
            cells.append(value)
        rows.append(cells)
    return rows


def serial_to_datetime(num: float) -> datetime.datetime:
    """An Excel date serial (days since 30/12/1899, time as the fraction) -> datetime, rounded to the second."""
    return datetime.datetime(1899, 12, 30) + datetime.timedelta(seconds=round(float(num) * 86400))


def as_text(value) -> str:
    """One cell as the text the rest of the program uses ('' for empty; a whole number without '.0')."""
    if isinstance(value, datetime.datetime):
        return value.strftime(SHOWN_FORMAT)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return " ".join(str(value or "").split())


def as_date_text(value) -> str:
    """A date column: a real date cell, a serial number in an unformatted cell, or the text as written."""
    if isinstance(value, float) and 20000 < value < 80000:
        return serial_to_datetime(value).strftime(SHOWN_FORMAT)
    return as_text(value)


# ---------------------------------------------------------------------------------------------- the cases

def read_bam_report(path, id_re, reviewed_status: str, norm_account) -> tuple:
    """-> (cases to review, excluded rows, warnings).  Same case fields as the older CSV reader, plus the BAM extras.

    Excluded rows (never dropped silently): Process Status other than 'Ended' (Voided ...), an invalid or repeated
    RequestId, no Client Account Number.  Each has its 'reason'."""
    path = Path(path)
    if not path.is_file():
        raise ReportError("the BAM Report file was not found")
    if path.suffix.lower() != ".xlsx":
        raise ReportError("the BAM Report must be an Excel .xlsx file (this one is '" + (path.suffix or "no type") + "')")
    rows = read_sheet(path)
    if len(rows) < HEADER_ROW:
        raise ReportError(f"the report has fewer than {HEADER_ROW} rows - its headers must be in row {HEADER_ROW}")
    header = [as_text(h) for h in rows[HEADER_ROW - 1]]
    while header and not header[-1]:
        header.pop()
    at = {}
    for i, h in enumerate(header):
        k = norm_head(h)
        for key, (_, spellings) in COLUMNS.items():
            if k in spellings and key not in at:
                at[key] = i
    missing = [COLUMNS[k][0] for k in NEEDED if k not in at]
    if missing:
        raise ReportError(f"row {HEADER_ROW} of the first sheet has no column for: " + ", ".join(missing)
                          + f" (the headers must be in row {HEADER_ROW}; found: "
                          + (", ".join(h for h in header if h)[:300] or "nothing") + ")")
    warnings = []
    if len(header) != EXPECTED_COLUMNS:
        warnings.append(f"the report has {len(header)} columns instead of the usual {EXPECTED_COLUMNS} - "
                        "every needed column was found by its name, so the run can go on")
    absent = [COLUMNS[k][0] for k in COLUMNS if k not in at]
    if absent:
        warnings.append("these columns are not in the report: " + ", ".join(absent))
    cases, excluded, seen = [], [], set()
    for n, raw in enumerate(rows[HEADER_ROW:], HEADER_ROW + 1):
        def get(key, _raw=raw):
            i = at.get(key)
            if i is None or i >= len(_raw):
                return ""
            return as_date_text(_raw[i]) if key in DATE_KEYS else as_text(_raw[i])
        if not any(as_text(v) for v in raw):
            continue                                        # an empty line
        item = {"row_number": n, "iid": get("iid").lower(), "account": norm_account(get("account")),
                "client_number": get("client"), "status": get("status"), "sub_worktype_csv": get("sub"),
                "end_date": get("end"), "case_source": get("source"), "received_date": get("received"),
                "client_name": get("client_name"), "fund": get("fund"), "fund_type": get("fund_type"),
                "email_subject": get("subject"),
                "row": {header[i]: as_text(v) for i, v in enumerate(raw) if i < len(header) and header[i]}}
        if not id_re.match(item["iid"]):
            excluded.append({**item, "reason": "not a valid RequestId (32 characters)"})
        elif item["status"].casefold() != reviewed_status:
            excluded.append({**item, "reason": f"Process Status is {item['status'] or 'blank'}"})
        elif item["iid"] in seen:
            excluded.append({**item, "reason": "the same RequestId is on an earlier row"})
        elif not item["account"]:
            excluded.append({**item, "reason": "no Client Account Number"})
        else:
            seen.add(item["iid"])
            cases.append(item)
    return cases, excluded, warnings


def newest_report(folder) -> Path | None:
    """The most recently changed .xlsx in the BAM Reports folder (Excel's '~$' lock files ignored)."""
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() == ".xlsx"
                 and not p.name.startswith("~$")]
    except OSError:
        return None
    return max(files, key=lambda p: p.stat().st_mtime, default=None)
