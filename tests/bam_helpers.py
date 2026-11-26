"""Synthetic BAM Reports for the tests: 3 sheets, a 4-row title block, headers in row 5, 31 columns, real date cells.
Synthetic data only."""
import datetime
import io
import zipfile
from xml.sax.saxutils import escape

# The 31 columns, A..AE, as the real report has them (the ignored ones get filler names).
HEADERS = (["RequestId"] + [f"Ignored {c}" for c in "BCDEFGHIJ"] + ["Received Date", "Ignored L", "Ignored M",
           "Client Account Number", "Client Name", "Client Number", "Fund", "Fund Type", "Ignored S", "Ignored T",
           "Ignored U", "Process Status", "Ignored W", "Ignored X", "Source", "Email Subject", "Sub Worktype",
           "Ignored AB", "Ignored AC", "Ignored AD", "End Date"])
assert len(HEADERS) == 31


def col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def serial(dt: datetime.datetime) -> float:
    return (dt - datetime.datetime(1899, 12, 30)).total_seconds() / 86400


def _cell(ref, value):
    if value is None or value == "":
        return ""
    if isinstance(value, datetime.datetime):
        return f'<c r="{ref}" s="1"><v>{serial(value)!r}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _sheet(rows):
    body = []
    for n, row in enumerate(rows, 1):
        body.append(f'<row r="{n}">' + "".join(_cell(f"{col(i)}{n}", v) for i, v in enumerate(row, 1)) + "</row>")
    return ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            '2006/main"><sheetData>' + "".join(body) + "</sheetData></worksheet>")


def case(iid, account="AB100000", status="Ended", sub="Change of Address", client="C1", name="Synthetic Client One",
         fund="Fund A", fund_type="Income", source="Email", subject="Change please", received=None, end=None):
    """One report row as {header: value}."""
    return {"RequestId": iid, "Client Account Number": account, "Process Status": status, "Sub Worktype": sub,
            "Client Number": client, "Client Name": name, "Fund": fund, "Fund Type": fund_type, "Source": source,
            "Email Subject": subject,
            "Received Date": received or datetime.datetime(2026, 11, 20, 9, 15),
            "End Date": end or datetime.datetime(2026, 11, 25, 12, 23)}


def write_bam(path, cases, headers=HEADERS, title_rows=4, first_sheet_name="Report", sheet_order=("1", "2", "3")):
    """The report as the real one is laid out.  `sheet_order` lets a test put the report's file second in the zip."""
    rows = [[f"BAM Studio report - title line {i}"] for i in range(1, title_rows + 1)] + [list(headers)]
    for c in cases:
        rows.append([c.get(h, "") for h in headers])
    sheets = {"1": _sheet(rows), "2": _sheet([["Summary"], ["not used"]]), "3": _sheet([["Parameters"]])}
    names = {"1": first_sheet_name, "2": "Summary", "3": "Parameters"}
    book = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
            'main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(f'<sheet name="{names[k]}" sheetId="{i}" r:id="rId{k}"/>' for i, k in enumerate(sheet_order, 1))
            + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">' + "".join(
                f'<Relationship Id="rId{k}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/sheet{k}.xml"/>' for k in "123") + "</Relationships>")
    styles = ('<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
              '2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="22" applyNumberFormat="1"/></cellXfs>'
              '</styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles)
        for k, body in sheets.items():
            z.writestr(f"xl/worksheets/sheet{k}.xml", body)
    path.write_bytes(buf.getvalue())
    return path
