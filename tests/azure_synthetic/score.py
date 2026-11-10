"""Accuracy report: the Azure reviewer's results on the synthetic cases against what the generator wrote (stdlib only).

    <python> tests/azure_synthetic/score.py --expected <out>/expected.json --results <results folder> --out <report.md>

Pass bar (all three must hold; exit 0 = PASS, 1 = FAIL):
  1. No trap case comes out Correct (a trap case with no result at all also fails: nothing proves it was caught).
  2. >= 95% of the handwritten text/date fields are read right.  STRICT: the reviewer's VALUE must equal the value
     written on the form, ignoring only capitals and extra spaces (line breaks and commas between address lines count
     as spaces; spaces next to . @ / - are ignored), after the agreed FORMAT-SAFE normalisation, applied to both sides:
       - phone numbers: digits only;
       - dates: dd/mm/yyyy (day and month zero-padded; O/I/L/S/B/Z read as 0/1/1/5/8/2);
       - UK postcodes: no spaces; a letter where the pattern needs a digit (and the reverse) is read by its place
         (inward code digit + 2 letters; the 3rd/4th outward character of AA9 / AA99 / AA9A);
       - CCLA account numbers: 2 letters + exactly 7 digits - only then a digit in the 2-letter prefix is read as its
         letter (C0 -> CO); a value of exactly 7 digits is the number written after a printed prefix.
     Fields the reviewer flagged as low confidence still count; the score over the unflagged fields only is shown
     beside it.  Deliberately illegible fields are left out of the score and must be flagged instead.
  3. Every extracted field with CONFIDENCE below 0.80 is marked LOW_CONFIDENCE (in every result file).
Everything else (typed fields, ticks, signatures, the non-trap verdicts) is reported, not part of the bar.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BAR = 0.95
LOW = 0.80
TRUE_WORDS = {"ticked", "true", "yes", "y", "x", "checked", "selected", "1", "signed", "present", "signature present"}


def norm(v) -> str:
    s = str(v if v is not None else "").casefold().replace("\n", " ").replace(",", " ").replace(";", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*([.@/\-])\s*", r"\1", s)


def norm_mark(v) -> str:
    """Ticks and signatures: present or not, however the reviewer words it ('possibly not signed' = not signed)."""
    if isinstance(v, bool):
        return "yes" if v else ""
    t = norm(v)
    if set(re.findall(r"[a-z]+", t)) & {"not", "no", "unsigned", "unticked", "blank", "empty", "absent", "missing"}:
        return ""
    return "yes" if t in TRUE_WORDS or t.startswith(("signed", "ticked", "yes")) else ""


TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"})
TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "7": "T"})


def norm_date(v) -> str:
    t = re.sub(r"\s+", "", str(v or "")).upper()
    parts = [x.translate(TO_DIGIT) for x in re.split(r"[/.\-]+", t) if x]
    if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4 and len(parts[0]) <= 2 and len(parts[1]) <= 2:
        return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
    return norm(v)


def norm_postcode(v) -> str:
    t = re.sub(r"\s+", "", str(v or "")).upper()
    if not 5 <= len(t) <= 7:
        return norm(v)
    out, inward = list(t[:-3]), t[-3:]
    inward = inward[0].translate(TO_DIGIT) + inward[1:].translate(TO_LETTER)
    out[0] = out[0].translate(TO_LETTER)
    if len(out) == 4:                        # AA99 / AA9A: letters, letters, digit
        out[1], out[2] = out[1].translate(TO_LETTER), out[2].translate(TO_DIGIT)
    elif len(out) == 3 and out[1].isalpha():  # AA9
        out[2] = out[2].translate(TO_DIGIT)
    return ("".join(out) + inward).casefold()


def norm_account(v) -> str:
    t = re.sub(r"\s+", "", str(v or "")).upper()
    if len(t) == 9 and t[2:].isdigit():
        return (t[:2].translate(TO_LETTER) + t[2:]).casefold()
    return norm(v)


def norm_field(exp: dict, v) -> str:
    """The value as compared for this field: the format-safe normalisation for its type, else norm()."""
    f = exp["field"]
    if "phone" in f or "mobile" in f:
        return re.sub(r"\D", "", str(v or ""))
    if exp["kind"] == "date":
        return norm_date(v)
    if "postcode" in f:
        return norm_postcode(v)
    if ("account_number" in f and not f.startswith("bank_")) or "client_id" in f:
        return norm_account(v)
    return norm(v)


def right(exp: dict, got) -> bool:
    if got is None:
        return False
    if exp["kind"] in ("checkbox", "signature"):
        return norm_mark(got) == norm_mark(exp["value"])
    return norm_field(exp, got) in {norm_field(exp, x) for x in [exp["value"], *exp.get("alternatives", [])]}


def load_results(folder: Path) -> dict:
    out = {}
    for f in sorted(Path(folder).glob("*.json")):
        try:
            obj = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("iid"):
            out[str(obj["iid"]).lower()] = obj
    return out


def extracted_by_file(res: dict) -> dict:
    """{(file name, field id): extracted field} of one result file."""
    files = {d.get("DOC_ID"): d.get("FILE", "") for d in res.get("DOCUMENTS") or [] if isinstance(d, dict)}
    out = {}
    for x in res.get("EXTRACTED_FIELDS") or []:
        if isinstance(x, dict):
            out.setdefault((files.get(x.get("DOC_ID"), ""), x.get("FIELD")), x)
    return out


def verdict(res) -> str:
    r = (res or {}).get("result") or {}
    return r.get("REVIEW_RESULT") or r.get("COPILOT_RESULT") or ""


def score(expected: dict, results: dict) -> dict:
    hand, hand_unflagged, other, illegible, conf_bad, cases = [], [], [], [], [], []
    for c in expected["cases"]:
        res = results.get(c["iid"].lower())
        got_verdict = verdict(res)
        state = (res or {}).get("state", "no result file")
        cases.append({"n": c.get("n"), "iid": c["iid"], "form": c["form"], "trap": c["trap"], "expected": c["expected"],
                      "got": got_verdict or f"(none: {state})", "ok": got_verdict in c["expected"],
                      "trap_caught": None if not c["trap"] else bool(got_verdict) and got_verdict != "Correct"})
        ext = extracted_by_file(res or {})
        for e in c["fields"]:
            x = ext.get((e["file"], e["field"]))
            value = x.get("VALUE") if x else None
            flagged = bool(x and x.get("LOW_CONFIDENCE"))
            row = {"case": c.get("n"), "field": e["field"], "expected": e["value"], "got": value,
                   "confidence": x.get("CONFIDENCE") if x else None, "flagged": flagged, "ok": right(e, value)}
            if e.get("illegible"):
                row["ok"] = flagged or row["ok"]
                illegible.append(row)
            elif e.get("handwritten"):
                hand.append(row)
                if x and not flagged:
                    hand_unflagged.append(row)
            else:
                other.append(row)
    for res in results.values():
        for x in res.get("EXTRACTED_FIELDS") or []:
            try:
                conf = float(x.get("CONFIDENCE"))
            except (TypeError, ValueError):
                conf = None
            if conf is not None and conf < LOW and not x.get("LOW_CONFIDENCE"):
                conf_bad.append({"iid": res.get("iid"), "field": x.get("FIELD_ID") or x.get("FIELD"), "confidence": conf})
    rate = lambda rows: (sum(r["ok"] for r in rows) / len(rows)) if rows else 0.0
    traps = [c for c in cases if c["trap"]]
    bars = {
        "traps": {"ok": all(c["trap_caught"] for c in traps) and bool(traps),
                  "text": f"Trap cases not Correct: {sum(bool(c['trap_caught']) for c in traps)} of {len(traps)}"},
        "handwritten": {"ok": bool(hand) and rate(hand) >= BAR,
                        "text": f"Handwritten fields right: {sum(r['ok'] for r in hand)} of {len(hand)} = {rate(hand):.1%} "
                                f"(bar {BAR:.0%}; unflagged only: {sum(r['ok'] for r in hand_unflagged)} of "
                                f"{len(hand_unflagged)} = {rate(hand_unflagged):.1%})"},
        "flagging": {"ok": not conf_bad,
                     "text": f"Fields below {LOW:.2f} confidence not flagged: {len(conf_bad)}"},
    }
    return {"pass": all(b["ok"] for b in bars.values()), "bars": bars, "cases": cases, "hand": hand, "other": other,
            "illegible": illegible, "conf_bad": conf_bad, "rates": {"hand": rate(hand), "hand_unflagged": rate(hand_unflagged),
                                                                   "other": rate(other)}}


def report(s: dict, expected: dict) -> str:
    L = ["# Azure reviewer - accuracy on the synthetic cases", "",
         "> SYNTHETIC TEST DATA - made-up cases. Pass bar agreed before using real cases on the VDI.", "",
         f"**Overall: {'PASS' if s['pass'] else 'FAIL'}**", "", "| Pass bar | Result |", "|---|---|"]
    L += [f"| {b['text']} | {'PASS' if b['ok'] else 'FAIL'} |" for b in s["bars"].values()]
    L += ["", f"Also: deliberately illegible fields flagged or read right: {sum(r['ok'] for r in s['illegible'])} of "
          f"{len(s['illegible'])}; typed fields, ticks and signatures right: {sum(r['ok'] for r in s['other'])} of "
          f"{len(s['other'])} = {s['rates']['other']:.1%} (not part of the bar).", "",
          "## Cases", "", "| # | form | trap | expected | reviewer said | as expected |", "|---|---|---|---|---|---|"]
    for c in s["cases"]:
        L.append(f"| {c['n']} | {c['form']} | {c['trap'] or '-'} | {' or '.join(c['expected'])} | {c['got']} | "
                 f"{'yes' if c['ok'] else 'NO'} |")
    wrong = [r for r in s["hand"] + s["illegible"] + s["other"] if not r["ok"]]
    L += ["", f"## Fields not right ({len(wrong)})", ""]
    if wrong:
        L += ["| case | field | written on the form | reviewer read | confidence | flagged |", "|---|---|---|---|---|---|"]
        for r in wrong:
            conf = "" if r["confidence"] is None else f"{float(r['confidence']):.2f}" if isinstance(r["confidence"], (int, float)) else str(r["confidence"])
            got = "(not extracted)" if r["got"] is None else str(r["got"]).replace("|", "/").replace("\n", " ")
            L.append(f"| {r['case']} | {r['field']} | {str(r['expected']).replace('|', '/')} | {got} | {conf} | "
                     f"{'yes' if r['flagged'] else 'no'} |")
    if s["conf_bad"]:
        L += ["", "## Below 0.80 confidence but NOT flagged", ""] + [f"- {x['iid']} {x['field']} ({x['confidence']:.2f})" for x in s["conf_bad"]]
    L += ["", f"Generator seed {expected.get('generator', {}).get('seed')}, {expected.get('di_pages')} Document Intelligence pages per run."]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score the Azure reviewer on the synthetic cases.")
    ap.add_argument("--expected", required=True)
    ap.add_argument("--results", required=True, help="folder of <iid>.json result files")
    ap.add_argument("--out", required=True, help="accuracy report (.md) to write")
    a = ap.parse_args(argv)
    expected = json.loads(Path(a.expected).read_text("utf-8"))
    s = score(expected, load_results(Path(a.results)))
    Path(a.out).write_text(report(s, expected), "utf-8")
    for b in s["bars"].values():
        print(("PASS  " if b["ok"] else "FAIL  ") + b["text"])
    print(("PASS" if s["pass"] else "FAIL") + f" - report: {a.out}")
    return 0 if s["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
