"""Case timing for the base message (stdlib only, no time-zone database: the embedded Python has none).

Every case's message gets four values, put where the base message has these placeholders:

  {{CASE_END_DATETIME}}           the daily CSV's End Date, exactly as written there (UK time; an Excel serial
                                  number such as 46308.48958 is written as 13/10/2026 11:45 AM)
  {{REGISTER_SNAPSHOT_DATETIME}}  the snapshot's Account Summary metadata.retrievedUtc, to whole seconds
  {{BUSINESS_DAYS_GAP}}           business days from the case end to the snapshot (England & Wales working days)
  {{CASE_AGE_STATEMENT}}          one of the two fixed sentences of the result contract

Counting: both times in UK local time; weekends and England & Wales bank holidays do not count; the count starts the
day AFTER the case ended and includes the snapshot's day (ended Monday, snapshot Thursday = 3).  3 or less = within
the last 3 business days.  Anything that cannot be worked out safely is 'Not provided' (never a guess), with a
'CHECK:' note that the daily report shows.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

NOT_PROVIDED = "Not provided"
PLACEHOLDERS = ("{{CASE_END_DATETIME}}", "{{REGISTER_SNAPSHOT_DATETIME}}", "{{BUSINESS_DAYS_GAP}}", "{{CASE_AGE_STATEMENT}}")
WITHIN = "Case completed within the last 3 business days"
AGED = "Case completed more than 3 business days"
AGE_LIMIT = 3
PROCESSOR_ERROR, AGED_CASE = "Potential processor error", "Possible discrepancy due to aged case"
AGED_SENTENCE = "This case was completed more than 3 business days before the Register snapshot was captured."
HOLIDAY_FILE = "bank_holidays.txt"      # optional, beside daily_review_instructions.md: one date per line

# England & Wales bank holidays (gov.uk rules, substitute days included).  Later years: add them to bank_holidays.txt.
BANK_HOLIDAYS = {
    2025: ("2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05", "2025-05-26", "2025-08-25", "2025-12-25", "2025-12-26"),
    2026: ("2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28"),
    2027: ("2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-31", "2027-08-30", "2027-12-27", "2027-12-28"),
    2028: ("2028-01-03", "2028-04-14", "2028-04-17", "2028-05-01", "2028-05-29", "2028-08-28", "2028-12-25", "2028-12-26"),
}
# How the CSV's End Date may look (day first, as in the UK): '12/10/2026 02:17 PM', or as Excel re-saves it.
# Or as an Excel serial number (days since 30/12/1899, the time as the fraction): '46308.48958' = 13/10/2026 11:45 AM.
EXCEL_EPOCH = datetime.datetime(1899, 12, 30)
SERIAL_RE = re.compile(r"^\d{5}([.,]\d+)?$")         # 5 digits = the years 1927-2173 (a comma too)
SHOWN_FORMAT = "%d/%m/%Y %I:%M %p"                   # how a serial End Date is written into the message
END_FORMATS = ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
               "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


class Holidays:
    """The bank holidays in use: the built-in table plus bank_holidays.txt.  `years` = years known completely."""

    def __init__(self, folder=None):
        self.dates = {datetime.date.fromisoformat(d) for days in BANK_HOLIDAYS.values() for d in days}
        self.years = set(BANK_HOLIDAYS)
        self.problems, self.file = [], None
        path = Path(folder) / HOLIDAY_FILE if folder else None
        if path and path.is_file():
            self.file = path
            try:
                lines = path.read_text("utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError):
                self.problems.append(f"{HOLIDAY_FILE} could not be read")
                lines = []
            for n, line in enumerate(lines, 1):
                text = line.split("#", 1)[0].strip()
                if not text:
                    continue
                day = _date(text)
                if day is None:
                    self.problems.append(f"{HOLIDAY_FILE} line {n} is not a date (write 2029-01-01 or 01/01/2029)")
                else:
                    self.dates.add(day)
                    self.years.add(day.year)


def _date(text: str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_end(text):
    """The CSV's End Date as a plain UK local datetime, or None when it cannot be read.  An Excel serial number is
    rounded to the minute (the fraction is never exact)."""
    text = " ".join(str(text or "").split())
    if SERIAL_RE.match(text):
        moment = EXCEL_EPOCH + datetime.timedelta(days=float(text.replace(",", ".")))
        return (moment + datetime.timedelta(seconds=30)).replace(second=0, microsecond=0)
    for fmt in END_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def parse_utc(text):
    """An ISO time with its offset ('2026-10-14T12:23:38.123456+00:00', 'Z' too) as an aware datetime, or None."""
    text = str(text or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else None


def _last_sunday(year: int, month: int) -> datetime.date:
    day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def uk_local(moment: datetime.datetime) -> datetime.datetime:
    """UK clock time of an aware moment: summer time (BST, +1) from 01:00 UTC on the last Sunday of March to
    01:00 UTC on the last Sunday of October, otherwise GMT."""
    utc = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    start = datetime.datetime.combine(_last_sunday(utc.year, 3), datetime.time(1))
    end = datetime.datetime.combine(_last_sunday(utc.year, 10), datetime.time(1))
    return utc + datetime.timedelta(hours=1 if start <= utc < end else 0)


def business_days(ended: datetime.date, snapshot: datetime.date, holidays: Holidays) -> int:
    """Working days after `ended` up to and including `snapshot` (0 when the same day)."""
    count, day = 0, ended
    while day < snapshot:
        day += datetime.timedelta(days=1)
        if day.weekday() < 5 and day not in holidays.dates:
            count += 1
    return count


def snapshot_time(text) -> str:
    """retrievedUtc to whole seconds ('2026-10-14T12:23:38+00:00'); 'Not provided' when missing or unreadable."""
    value = parse_utc(text)
    return value.replace(microsecond=0).isoformat() if value else NOT_PROVIDED


def case_timing(end_text, retrieved_utc, holidays: Holidays, from_csv: bool = True) -> dict:
    """The four message values of one case + `notes` (CHECK lines for the daily report).
    `from_csv` False = there is no CSV (send-only): a missing End Date is then expected, not flagged."""
    out = {"case_end": NOT_PROVIDED, "snapshot": snapshot_time(retrieved_utc), "gap": NOT_PROVIDED,
           "age": NOT_PROVIDED, "notes": []}
    end_text = " ".join(str(end_text or "").split())
    ended = parse_end(end_text)
    if not end_text:
        if from_csv:
            out["notes"].append("CHECK: no End Date in the CSV - sent with case timing 'Not provided'")
    elif ended is None:
        out["notes"].append(f"CHECK: the CSV End Date '{end_text}' could not be read - sent with case timing 'Not provided'")
    else:       # as written in the CSV; an Excel serial number becomes '13/10/2026 11:45 AM' (Copilot cannot read it)
        out["case_end"] = (end_text if not SERIAL_RE.match(end_text)
                           else ended.strftime("%d/%m/%Y" if end_text.isdigit() else SHOWN_FORMAT))   # whole number = a day
    if out["snapshot"] == NOT_PROVIDED:
        out["notes"].append("CHECK: the snapshot has no readable retrieval time - sent with 'Not provided'")
    if ended is None or out["snapshot"] == NOT_PROVIDED:
        return out
    taken = uk_local(parse_utc(retrieved_utc))
    has_time = ":" in end_text or bool(SERIAL_RE.match(end_text) and not end_text.isdigit())
    if (taken < ended) if has_time else (taken.date() < ended.date()):     # a date alone: compare days only
        out["notes"].append("CHECK: the snapshot is older than the case end - business days sent as 'Not provided'")
        return out
    missing = sorted({y for y in range(ended.year, taken.year + 1)} - holidays.years)
    if missing:
        out["notes"].append(f"CHECK: no bank-holiday list for {', '.join(map(str, missing))} (add the dates to "
                            f"{HOLIDAY_FILE}) - business days sent as 'Not provided'")
        return out
    gap = business_days(ended.date(), taken.date(), holidays)
    out.update(gap=str(gap), age=WITHIN if gap <= AGE_LIMIT else AGED)
    return out


def missing_placeholders(template: str) -> list:
    return [p for p in PLACEHOLDERS if p not in template]


def fill(template: str, timing: dict) -> str:
    """The base message of ONE case: every placeholder replaced by its value (or 'Not provided')."""
    values = dict(zip(PLACEHOLDERS, (timing.get("case_end"), timing.get("snapshot"), timing.get("gap"), timing.get("age"))))
    return re.sub("|".join(re.escape(p) for p in PLACEHOLDERS), lambda m: str(values[m.group(0)] or NOT_PROVIDED), template)


def expected_discrepancy(timing: dict) -> str:
    """The DISCREPANCY_TYPE a case with at least one mismatch must carry (result contract, section 5): from the age
    statement; without one the business-day gap decides; without both it is a potential processor error."""
    if timing.get("age") == AGED:
        return AGED_CASE
    if timing.get("age") == WITHIN:
        return PROCESSOR_ERROR
    gap = str(timing.get("gap") or "")
    return AGED_CASE if gap.isdigit() and int(gap) > AGE_LIMIT else PROCESSOR_ERROR
