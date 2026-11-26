"""The run's relationship map: which instructions of the same customer probably belong together (stdlib only).

Built by the coordinator right after the documents map (shared/document_map.py), from that map's instruction entries,
and sent to Mission Control together with it, before any case.  Agreed with the user on 2026-11-26:

  tree     customer (Client Number; System 1's when the report has none) -> Sub Worktype -> received-time cluster
           (instructions received within `relationship_hours` of each other, chained; setting, default 2)
  pairs    ONLY within the same Client Number, never across customers.  The highest matching level wins and every
           matching reason is listed:
             strong    they share a Document ID
             likely    received within the window (same time cluster), or the same Sub Worktype AND the same Source,
                       or the same email subject (tidied: case, spaces, RE:/FW:/FWD:) - the subject is supporting
                       evidence only (user, 2026-11-26): it never makes a pair strong and a different one counts nothing
             possible  the same customer only
"""
from __future__ import annotations

import datetime
import re

from shared import pipeline as P

MAP_VERSION = 1
MAP_FILE = "relationship_map.json"                  # in the run folder
COPY_SUFFIX = "-relationships.json"                 # CASES_ROOT\_Document maps\<run>-relationships.json
DEFAULT_HOURS = 2
STRENGTHS = ("strong", "likely", "possible")
UNKNOWN_CUSTOMER = "(unknown customer)"
NO_DATE = "(no received date)"
PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.I)
DATE_FORMATS = ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d/%m/%Y")

HOW_TO_READ = [
    "This map groups the run's instructions that probably belong together, as guidance next to the documents map. "
    "Instructions of one customer (Client Number) often support each other: a document in one instruction can back "
    "a change made in another instruction or on another of the customer's accounts.",
    "customers: one entry per Client Number, in the BAM Report's order -> sub_worktypes -> time_clusters "
    "(instructions received within 'window_hours' of each other, chained: A-B within the window and B-C within the "
    "window = one cluster). Every instruction of the run appears exactly once in this tree.",
    "instructions: one entry per instruction ID with its customer, account, Sub Worktype, received date, time "
    "cluster and 'related': the other instructions of the SAME customer, strongest first, each with a strength and "
    "every reason that matched. Instructions of different customers are never linked here (shared documents across "
    "customers are visible in the documents map).",
    "strength: 'strong' = they share a System 1 Document ID; 'likely' = received within the window, or the same Sub "
    "Worktype and the same Source, or the same email subject (ignoring case, spaces and RE:/FW:/FWD:); 'possible' = "
    "same customer only. The email subject is supporting evidence only: instructions with different subjects can "
    "still be related through the other values. It is guidance, not proof: check the documents.",
    "status: 'ready' = sent for review in this run; 'held' / 'waiting' / 'not complete' = not sent in this run.",
]


def received_at(text):
    t = " ".join(str(text or "").split()).upper()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def tidy_subject(text) -> str:
    return " ".join(PREFIX.sub("", str(text or "")).casefold().split())


def gap_words(minutes: float) -> str:
    m = int(round(abs(minutes)))
    return f"{m} min" if m < 120 else f"{m / 60:.1f} h"


def _clusters(members: list, when: dict, hours: float) -> dict:
    """iid -> 'T<n>' for the instructions with a received date, chained within `hours` (in time order)."""
    dated = sorted((i for i in members if when.get(i)), key=lambda i: when[i])
    out, n, last = {}, 0, None
    for i in dated:
        if last is None or (when[i] - when[last]).total_seconds() > hours * 3600:
            n += 1
        out[i], last = f"T{n}", i
    return out


def build_relationships(doc_map: dict, hours: float = DEFAULT_HOURS) -> dict:
    """The relationship map from the documents map's instruction entries (report order kept)."""
    inst = (doc_map or {}).get("instructions") or {}
    order = list(inst)
    customer_of = {i: (inst[i].get("client_number") or "").strip() or UNKNOWN_CUSTOMER for i in order}
    when = {i: received_at(inst[i].get("received_date")) for i in order}
    subject = {i: tidy_subject(inst[i].get("email_subject")) for i in order}
    docs = {i: set(inst[i].get("document_ids") or []) for i in order}
    customers = list(dict.fromkeys(customer_of[i] for i in order))
    cluster = {}
    for cust in customers:
        members = [i for i in order if customer_of[i] == cust]
        for i, t in _clusters(members, when, hours).items():
            cluster[i] = f"{cust}/{t}" if cust != UNKNOWN_CUSTOMER else ""

    def pair(a, b):
        reasons, strength = [], "possible"
        shared = sorted(docs[a] & docs[b])
        if shared:
            reasons.append(f"share {len(shared)} document(s): {', '.join(shared)}")
            strength = "strong"
        same_subject = bool(subject[a]) and subject[a] == subject[b]
        if same_subject:                        # supporting evidence only (user, 2026-11-26): never 'strong' alone
            reasons.append("same email subject (supporting only)")
        same_type = (inst[a].get("sub_worktype") or "") and P.norm_key(inst[a].get("sub_worktype")) == \
            P.norm_key(inst[b].get("sub_worktype"))
        same_source = (inst[a].get("source") or "") and P.norm_key(inst[a].get("source")) == \
            P.norm_key(inst[b].get("source"))
        if cluster.get(a) and cluster.get(a) == cluster.get(b):
            reasons.append(f"received {gap_words((when[a] - when[b]).total_seconds() / 60)} apart "
                           f"(same time cluster, window {hours:g} h)")
            strength = "strong" if strength == "strong" else "likely"
        elif when.get(a) and when.get(b):
            reasons.append(f"received {gap_words((when[a] - when[b]).total_seconds() / 60)} apart")
        if same_type:
            reasons.append(f"same Sub Worktype: {inst[a].get('sub_worktype')}")
        if same_source:
            reasons.append(f"same Source: {inst[a].get('source')}")
        if ((same_type and same_source) or same_subject) and strength == "possible":
            strength = "likely"
        reasons.append(f"same customer: Client Number {customer_of[a]}")
        return strength, reasons

    entries, counts = {}, {s: 0 for s in STRENGTHS}
    for a in order:
        related = []
        if customer_of[a] != UNKNOWN_CUSTOMER:
            for b in order:
                if b != a and customer_of[b] == customer_of[a]:
                    strength, reasons = pair(a, b)
                    related.append({"instruction_id": b, "client_account_number": inst[b].get("client_account_number", ""),
                                    "sub_worktype": inst[b].get("sub_worktype", ""), "status": inst[b].get("status", ""),
                                    "strength": strength, "reasons": reasons})
                    if a < b:
                        counts[strength] += 1
        related.sort(key=lambda r: STRENGTHS.index(r["strength"]))     # stable: report order within a strength
        entries[a] = {"instruction_id": a, "client_number": inst[a].get("client_number", ""),
                      "client_name": inst[a].get("client_name", ""),
                      "client_account_number": inst[a].get("client_account_number", ""),
                      "sub_worktype": inst[a].get("sub_worktype", ""), "received_date": inst[a].get("received_date", ""),
                      "source": inst[a].get("source", ""), "email_subject": inst[a].get("email_subject", ""),
                      "status": inst[a].get("status", ""), "time_cluster": cluster.get(a) or None,
                      "document_group": inst[a].get("group"), "related": related}
    tree = []
    for cust in customers:
        members = [i for i in order if customer_of[i] == cust]
        types = []
        for sub in dict.fromkeys(inst[i].get("sub_worktype") or "" for i in members):
            of_type = [i for i in members if (inst[i].get("sub_worktype") or "") == sub]
            clusters = []
            for c in dict.fromkeys(cluster[i] for i in sorted(of_type, key=lambda i: when[i] or datetime.datetime.max)
                                   if cluster.get(i)):
                ids = [i for i in of_type if cluster.get(i) == c]
                times = sorted(when[i] for i in ids)
                clusters.append({"time_cluster": c, "first_received": times[0].isoformat(),
                                 "last_received": times[-1].isoformat(), "instructions": ids})
            undated = [i for i in of_type if not cluster.get(i)]
            if undated:
                clusters.append({"time_cluster": NO_DATE if cust != UNKNOWN_CUSTOMER else None,
                                 "first_received": "", "last_received": "", "instructions": undated})
            types.append({"sub_worktype": sub, "instructions": len(of_type), "time_clusters": clusters})
        tree.append({"client_number": cust,
                     "client_names": sorted({inst[i].get("client_name", "") for i in members} - {""}),
                     "client_account_numbers": sorted({inst[i].get("client_account_number", "") for i in members} - {""}),
                     "instructions": len(members), "sub_worktypes": types})
    return {
        "map_version": MAP_VERSION, "run": (doc_map or {}).get("run", ""), "created": P.now(), "how_to_read": HOW_TO_READ,
        "window_hours": hours, "run_details": (doc_map or {}).get("run_details", {}),
        "summary": {"instructions": len(order), "customers": len([c for c in customers if c != UNKNOWN_CUSTOMER]),
                    "customers_with_several_instructions": sum(1 for t in tree if t["instructions"] > 1
                                                               and t["client_number"] != UNKNOWN_CUSTOMER),
                    "pairs": counts, "without_customer": sum(1 for i in order if customer_of[i] == UNKNOWN_CUSTOMER)},
        "customers": tree, "instructions": entries,
    }


def write(paths: P.Paths, doc_map: dict, hours: float) -> tuple:
    """-> (path, CASES_ROOT copy or '').  Raises on a failure (write_map records it)."""
    rel = build_relationships(doc_map, hours)
    target = paths.run / MAP_FILE
    P.write_json(target, rel)
    copy = ""
    try:
        c = paths.cases_root / P.DOCUMENT_MAPS_DIR / f"{paths.run.name}{COPY_SUFFIX}"
        P.write_json(c, rel)
        copy = str(c)
    except OSError:
        pass
    return str(target), copy, rel["summary"]


def read(paths: P.Paths):
    return P.read_json(paths.run / MAP_FILE)


def slice_of(rel_map, iid: str) -> dict:
    """What a case's send carries: its place in the tree and its related instructions."""
    e = ((rel_map or {}).get("instructions") or {}).get(iid) or {}
    return {"client_number": e.get("client_number", ""), "sub_worktype": e.get("sub_worktype", ""),
            "time_cluster": e.get("time_cluster"), "related": e.get("related") or [],
            "window_hours": (rel_map or {}).get("window_hours")}
