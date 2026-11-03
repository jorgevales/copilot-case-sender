#!/usr/bin/env python3
"""PART 2 - System 2 snapshots: fast bulk account-platform extractor (the account snapshot script, in the daily pipeline).

For every account of the run (unique, from the CSV), in bulk with parallel authenticated requests: the account summary,
its linked Interested Parties, their other customer/account relationships and the nine account-detail pages -> one
JSON snapshot per account in RUNTIME_ROOT\\pipeline\\snapshots\\<today>\\<ACCOUNT>_Complete_Account_Profile.json (name:
shared/pipeline.py).  The coordinator copies it into each case folder of that account when Part 1 has finished the
case.  A snapshot made earlier the same day is reused only if it reads back complete for that account.

Started by orchestrator/run_pipeline.py with --run-dir; never asks anything on the console.  Kept from the original
script: every parser, route, field label, the 24/40 worker pools and the output schema.  Changed for the pipeline:
  - the sign-in cookies are read from the systems Edge directly over its DevTools endpoint (stdlib, loopback only) -
    no second Playwright driver to start;
  - Access Denied (HTTP 401/403 or a denial page), after one retry, STOPS THE WHOLE RUN (shared stop signal), so the
    system is not hammered; an expired sign-in PAUSES this part until you sign in again and press Enter;
  - any other failed account is retried once, then reported (its cases are held, never sent without a snapshot);
  - a party/link page is requested once (the original's eager setdefault could submit duplicates);
  - the nine account-detail pages (2026-11-03) go through the same requests, retry, pause, stop and party pool; the
    snapshot is written only when every one of them was read or positively confirmed empty.
"""
from __future__ import annotations

import argparse,json,os,re,sys,threading,time,traceback
from concurrent.futures import CancelledError,ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import parse_qs,quote,urljoin,urlparse

SCRIPT_DIR=Path(os.path.abspath(__file__)).parent      # never .resolve(): it turns S: into its UNC form
PROJECT=SCRIPT_DIR.parent
for _p in (PROJECT,PROJECT/"3_copilot_sender"):
    if str(_p) not in sys.path:sys.path.insert(0,str(_p))
from shared import pipeline as P

# requests / bs4 are imported by load_libs() after the storage bootstrap (they live in the private dependency folder).
requests=BeautifulSoup=Tag=NavigableString=HTTPAdapter=None

# System 2's pages are pasted once in setup and kept only in the local config - never written in this code.
PORT=9223; BASE=""; CHECK_URL=""; ACCOUNT_PAGE=""; PARTY_MARKER=""; LINKS_PAGE=""; ACCOUNT_COLUMN="Client Account Number"
DETAIL_PAGES={}   # {P.DETAIL_SECTIONS name: address with {account}}
ACCOUNT_WORKERS=24; PARTY_WORKERS=40; CONNECTIONS=80   # party, link AND account-detail pages share the party pool
DETAIL_WORKERS=9  # only when fetch_all_account_sections is called without the run's pool
TL=threading.local(); COOKIES=[]; UA="Mozilla/5.0"; COOKIE_GEN=0
CAPTURES=None   # RUNTIME_ROOT\logs\captures\<run>: the page System 2 answered with when an account fails
TITLE="Part 2 - System 2 snapshots"
DENIED=re.compile(r"access\s+(is\s+)?denied|not\s+authori[sz]ed|forbidden|you\s+do\s+not\s+have\s+(the\s+)?(permission|access)",re.I)
SIGNIN_URL=re.compile(r"login|logon|signin|sign-in|/sso/|oauth|saml",re.I)


class AccessDenied(Exception):
    """System 2 refused this user: after one retry the whole run stops."""


class SessionExpired(Exception):
    """System 2 shows no signed-in page: this part pauses until you sign in again and press Enter."""


class RunStopped(Exception):
    """The shared stop signal is set: no further request is made."""


def load_libs():
    global requests,BeautifulSoup,Tag,NavigableString,HTTPAdapter
    import requests as _requests
    from bs4 import BeautifulSoup as _BS,NavigableString as _NS,Tag as _Tag
    from requests.adapters import HTTPAdapter as _HA
    requests,BeautifulSoup,Tag,NavigableString,HTTPAdapter=_requests,_BS,_Tag,_NS,_HA


def configure(cfg):
    global PORT,BASE,CHECK_URL,ACCOUNT_PAGE,PARTY_MARKER,LINKS_PAGE,DETAIL_PAGES
    u=urlparse(cfg.system2_url)
    PORT=int(cfg.systems_cdp_port); BASE=f"{u.scheme}://{u.netloc}"; CHECK_URL=cfg.system2_url
    ACCOUNT_PAGE=cfg.system2_account_page; PARTY_MARKER=cfg.system2_party_marker.lower(); LINKS_PAGE=cfg.system2_party_links_page
    pages=getattr(cfg,"system2_detail_pages",None) or {}
    DETAIL_PAGES={n:(pages.get(n) or "") for n in P.DETAIL_SECTIONS}


def clean(v): return re.sub(r"\s+"," ",str(v or "")).strip()
def label(v): return clean(v).rstrip(":").strip()
def absolute(v): return urljoin(BASE,v) if v else None
def normhead(v): return re.sub(r"[^a-z0-9]","",clean(v).casefold())
def auth(sender):
    """System 2 cookies + user agent, read from the systems Edge over its own DevTools endpoint (loopback only)."""
    payload=sender.cdp_http(PORT,"/json/version",1.5)
    ws=sender.endpoint_valid(payload,PORT)
    if not ws:raise SessionExpired(f"the systems Edge is not open on port {PORT}")
    reply=sender.cdp_call(ws,PORT,"Storage.getCookies",{},5.0,target="browser")
    if reply is None:raise SessionExpired("the systems Edge did not answer the cookie request")
    host=urlparse(BASE).hostname or ""
    def matches(c):
        d=str(c.get("domain","")).lstrip(".").lower()
        return bool(d) and (host==d or host.endswith("."+d))
    cookies=[c for c in reply.get("cookies",[]) if matches(c)]
    if not cookies:raise SessionExpired("no System 2 sign-in found in the systems Edge - sign in to System 2 there")
    return cookies,(payload or {}).get("User-Agent") or UA
def session():
    s=getattr(TL,"s",None)
    if s and getattr(TL,"gen",-1)==COOKIE_GEN:return s
    s=requests.Session(); s.headers.update({"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Referer":CHECK_URL,"Connection":"keep-alive"})
    a=HTTPAdapter(pool_connections=CONNECTIONS,pool_maxsize=CONNECTIONS,max_retries=0,pool_block=False);s.mount("https://",a)
    for c in COOKIES:s.cookies.set(c["name"],c["value"],domain=c.get("domain"),path=c.get("path","/"))
    TL.s,TL.gen=s,COOKIE_GEN;return s
def judge(r,s):
    """Raise AccessDenied / SessionExpired / RuntimeError for a page that is not a normal signed-in page."""
    text=clean(s.get_text(" ",strip=True))
    heads=" ".join(clean(x.get_text(" ",strip=True)) for x in s.find_all(["title","h1"]))   # page title/heading only:
    # an ordinary field reading e.g. 'Not authorised' further down an account page must never stop the run
    if r.status_code in (401,403) or DENIED.search(heads) or re.search(r"access\s+(is\s+)?denied",text,re.I):
        raise AccessDenied(f"System 2 refused access (HTTP {r.status_code})")
    if r.status_code==200 and "logout" not in text.lower():
        raise SessionExpired("System 2 sign-in expired" if not SIGNIN_URL.search(r.url) else "System 2 asks to sign in again")
    if r.status_code!=200:raise RuntimeError(f"HTTP {r.status_code}: {r.url}")
def soup_get(url,params=None):
    GATE.check()
    r=session().get(url,params=params,timeout=(5,35),allow_redirects=True);r.encoding=r.apparent_encoding or "utf-8";s=BeautifulSoup(r.text,"html.parser")
    try:
        judge(r,s)
    except Exception as e:
        e.page=(r,s)                                # kept for capture(): what System 2 showed
        raise
    return r,s
def capture(label,exc):
    """For the reviewing Copilot: the page System 2 answered with when `label` failed (addresses cut before '?')."""
    page=getattr(exc,"page",None)
    return capture_page(label,*page,f"{type(exc).__name__}: {exc}") if page else []
def capture_page(label,r,s,why):
    """A page copy (.txt + .html) in captures\\<run>\\ - also for a page that loaded but could not be used."""
    if CAPTURES is None:return []
    base=re.sub(r"[^A-Za-z0-9._-]+","_",f"system2-{label}")[:90]
    try:
        CAPTURES.mkdir(parents=True,exist_ok=True)
        title=clean(s.title.get_text()) if s.title else ""
        (CAPTURES/f"{base}.txt").write_text(P.REVIEWER_NOTE+f"\n\nHTTP {r.status_code}\naddress: {P.strip_query(r.url)}\ntitle: {title}\n"
                                            f"error: {why}\n\n"+P.strip_queries(clean(s.get_text(" ",strip=True)))[:100000],encoding="utf-8")
        (CAPTURES/f"{base}.html").write_text(P.html_note(P.strip_queries(r.text)[:3000000]),encoding="utf-8")
        return [f"{base}.txt",f"{base}.html"]
    except Exception:
        return []

def payload(e):
    text=clean(e.get_text(" ",strip=True)); ta=e.find("textarea")
    if ta:text=clean(ta.get_text(" ",strip=True) or ta.get("value"))
    d={"value":text}; links=[{"text":clean(a.get_text(" ",strip=True)),"url":absolute(a.get("href"))} for a in e.find_all("a",href=True)]
    if links:d["links"]=links
    if e.select_one("i.fa-check-circle-o,i.fa-check,input[type=checkbox][checked]"):d["checked"]=True
    return d
def parse_table(t):
    rows=t.find_all("tr"); headers=rows[0].find_all("th") if rows else []
    if headers:
        cols=[clean(x.get_text(" ",strip=True)) for x in headers];out=[]
        for r in rows[1:]:
            cells=r.find_all("td",recursive=False)
            if cells:out.append({(cols[i] if i<len(cols) and cols[i] else f"Column {i+1}"):payload(c) for i,c in enumerate(cells)})
        return {"columns":cols,"rows":out}
    fields={}
    for i,r in enumerate(rows):
        cells=r.find_all(["td","th"],recursive=False)
        if cells:
            ps=[payload(x) for x in cells];fields[label(ps[0]["value"]) or f"Row {i+1}"]=ps[1] if len(ps)>1 else ps[0]
    return {"fields":fields}
def defs(root):
    d={}
    for dl in root.find_all("dl"):
        for a,b in zip(dl.find_all("dt",recursive=False),dl.find_all("dd",recursive=False)):
            k=label(a.get_text(" ",strip=True));
            if k:d[k]=payload(b)
    return d
def account_page(s):
    head=s.select_one("div.account-details")
    if not head:raise ValueError("Account header not found")
    h=head.find("h1");sections={"Account header":{"Account Name":clean(h.get_text(" ",strip=True)) if h else "",**defs(head)}}
    root=s.select_one("#headerid") or s
    for p in root.select("div.panel.panel-default"):
        if p.find_parent("div",class_=lambda x:x and "panel" in x.split()):continue
        hd=p.select_one(".panel-heading");title=clean(hd.get_text(" ",strip=True)).rstrip(":") if hd else "Untitled section";body=p.select_one(".panel-body");sec={}
        if body:
            tables=body.find_all("table")
            if tables:sec.update(parse_table(tables[0]) if len(tables)==1 else {"tables":[parse_table(x) for x in tables]})
            else:
                f=defs(body)
                if f:sec["fields"]=f
                text=clean(body.get_text(" ",strip=True))
                if text:sec["text"]=text
        sections[title]=sec
    return sections
def party_descriptors(sections):
    out=[];seen=set()
    for row in sections.get("Interested Parties",{}).get("rows",[]):
        cell=row.get("Full Name",{});name=clean(cell.get("value"))
        for l in cell.get("links",[]):
            u=l.get("url")
            if u and PARTY_MARKER and PARTY_MARKER in u.lower() and u not in seen:
                seen.add(u);q=parse_qs(urlparse(u).query);pid=(q.get("InterestedPartyid") or q.get("interestedpartyid") or [""])[0];out.append({"interestedPartyId":pid,"nameFromAccount":name,"url":u})
    return out
def fetch_account(a,rows):
    r,s=soup_get(ACCOUNT_PAGE.replace("{account}",quote(a,safe="")))
    try:
        sections=account_page(s)
    except Exception as e:
        e.page=(r,s);raise
    returned=sections["Account header"].get("Account Code",{}).get("value","").upper()
    if returned and returned!=a:raise RuntimeError(f"Requested {a}, account-platform returned {returned}")
    matches=[{k:v for k,v in x.items() if not k.startswith("_")} for x in rows if x["_account"]==a]
    return {"metadata":{"requestedAccount":a,"retrievedUtc":datetime.now(timezone.utc).isoformat(),"sourceUrl":r.url,"httpStatus":r.status_code,"retrievalMethod":"parallel authenticated requests","pageTitle":clean(s.title.get_text()) if s.title else "","csvMatches":matches},"sections":sections,"_interestedPartyLinks":party_descriptors(sections)}
def root(s):return s.select_one("#interestPartyContainer") or s.select_one("form#frmmain") or s.body or s
def section(root,name):
    h=next((x for x in root.find_all(["h1","h2","h3","h4"]) if clean(x.get_text(" ",strip=True)).casefold()==name.casefold()),None)
    if not h:return None
    w=BeautifulSoup("<div></div>","html.parser").div
    for x in h.next_siblings:
        if isinstance(x,Tag) and x.name in {"h1","h2","h3","h4"}:break
        if isinstance(x,Tag):w.append(BeautifulSoup(str(x),"html.parser"))
    return w
def ctl(c):
    if not c:return ""
    if c.name=="select":
        o=c.find("option",selected=True);return clean(o.get_text(" ",strip=True) if o else "")
    if c.name=="textarea":return clean(c.get_text(" ",strip=True) or c.get("value"))
    t=clean(c.get("type")).lower()
    if t in {"checkbox","radio"}:return c.has_attr("checked")
    return "" if t in {"hidden","password","file"} else clean(c.get("value"))
MISSING_SECTION="Not available in Register snapshot (this section was not found on the page)"
BLANK_TABLE="Blank on Register (the table is on the page and has no entries)"
NOT_CAPTURED="Not captured"      # the contract's phrase for 'could not be read': Copilot then cannot compare (never blank)
# Distinct outcomes in every section's "completeness" block - never collapsed into an empty string or list.
EXTRACTED,EMPTY,NOT_FOUND,FAILED,DISAGREE="Extracted successfully","Confirmed empty","Not found","Extraction failed","Discrepancy"

# ---------------------------------------------------------------------------------------------- party profile
# What the Interested Party profile holds and where (System_2_Claude_Handover.md; Copilot's answer of 2026-11-03 from
# the captured page, sanitised).  The email / phone / address records are NOT rows in the page System 2 sends: the
# page's own setup functions AddExistingCustomerInterestedParty... build those rows in the browser from object
# literals written in its script.  This part never runs a script: it READS those literals as text (`setup_records`),
# also reads any finished rows a page carries (`table_records`), and compares the two.  A table that shows only its
# 'NoEmail' / 'NoPhone' / 'NoAddress' placeholder is never taken as proof of no records.

# snapshot key: (table id, saved-row id prefix, placeholder row id, setup function, heading (older layouts),
#                [(column, setup-script property, cell id prefix)])
CONTACTS={
 "Emails":("EmailContacts","EmailRow","NoEmail","AddExistingCustomerInterestedPartyEmails","Email",
           [("Email Type","EmailType","rowEditEmailType"),("Email Address","EmailAddress","rowEditEmailAddress")]),
 "Phone numbers":("PhoneContacts","PhoneRow","NoPhone","AddExistingCustomerInterestedPartyPhoneNumbers","Phone numbers",
           [("Phone Type","PhoneType","PhoneType"),("Country","PhoneCountry","rowEditPhoneCountry"),
            ("Area Code","PhoneAreaCode","rowEditPhoneAreaCode"),("Number","PhoneNumber","rowEditPhoneNumber")]),
 "Addresses":("addressList","AddressRow","NoAddress","AddExistingCustomerInterestedPartyAddresses","Address",
           [("Address Type","AddressType","rowEditAddressType"),("Country","AddressCountry","rowEditAddressCountry")]
           +[(f"Address Line {n}",f"AddressLine{n}",f"rowEditAddressLine{n}") for n in range(1,7)]
           +[("Postcode","Postcode","rowEditAddressPostcode")]),
}
# Hidden database references inside a saved row: never shown, never written (user, 2026-11-03).  Listed so that e.g.
# 'rowEditAddressTypeId7' is never read as the address type 'rowEditAddressType' + '7d...'.
HIDDEN_PREFIXES=("rowEditAddressTypeId","rowEditAddressDbCountryId","rowEditPhoneDbCountryId","tableEmailEntry",
                 "tablePhoneEntry","tableAddressEntry")
COMPLIANCE=(("AML","AML"),("PEP","PEP"),("Financial Sanctions","FS"))    # section heading, control id code
DIALOG=re.compile(r"dialog|modal|popup",re.I)
ENTITY=re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")


class ScriptError(ValueError):
    """The page's setup script is not in the shape this reader knows: the section is 'Not captured', never guessed."""


COUNTER=object()                 # a generated display counter (Id: ++LastEmailId): not a saved value, never written
JS_ESC={"n":"\n","t":"\t","r":"\r","b":"\b","f":"\f","v":"\v","0":"\0"}
JS_IDENT=re.compile(r"[A-Za-z_$][\w$]*")
JS_COUNTER=re.compile(r"(\+\+|--)\s*[A-Za-z_$][\w$]*|[A-Za-z_$][\w$]*\s*(\+\+|--)")
JS_NUMBER=re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
def js_skip(src,i):
    while i<len(src):
        if src[i].isspace():i+=1
        elif src.startswith("//",i):j=src.find("\n",i);i=len(src) if j<0 else j+1
        elif src.startswith("/*",i):j=src.find("*/",i+2);i=len(src) if j<0 else j+2
        else:break
    return i
def js_string(src,i):
    """The quoted text at src[i] -> (text, end), escapes decoded as JavaScript does."""
    q,out,i=src[i],[],i+1
    while i<len(src):
        c=src[i]
        if c==q:return "".join(out),i+1
        if c in "\r\n":raise ScriptError("a text value runs past the end of its line")
        if c=="\\":
            n=src[i+1:i+2]
            if n=="u" and re.fullmatch(r"[0-9a-fA-F]{4}",src[i+2:i+6]):out.append(chr(int(src[i+2:i+6],16)));i+=6;continue
            if n=="x" and re.fullmatch(r"[0-9a-fA-F]{2}",src[i+2:i+4]):out.append(chr(int(src[i+2:i+4],16)));i+=4;continue
            if n in ("\r","\n"):i+=2;continue
            out.append(JS_ESC.get(n,n));i+=2;continue
        out.append(c);i+=1
    raise ScriptError("a text value is not closed")
def js_value(src,i):
    """Text as written; a number / true / false / null as its source text (a string: a leading zero is never lost)."""
    if src[i:i+1] in ("'",'"'):return js_string(src,i)
    m=JS_COUNTER.match(src,i)
    if m:return COUNTER,m.end()
    m=JS_NUMBER.match(src,i)
    if m:return m.group(0),m.end()
    m=JS_IDENT.match(src,i)
    if m and m.group(0) in ("true","false","null"):return m.group(0),m.end()
    raise ScriptError("a value this reader does not know (an expression, not plain text)")
def js_object(src,i):
    """The object literal starting at src[i]=='{' -> ({property: value}, end).  Strict: anything else is an error."""
    out,i={},js_skip(src,i+1)
    while True:
        if i>=len(src):raise ScriptError("a record is not closed")
        if src[i]=="}":return out,i+1
        if src[i] in "'\"":key,i=js_string(src,i)
        else:
            m=JS_IDENT.match(src,i)
            if not m:raise ScriptError("a record has something other than a property name")
            key,i=m.group(0),m.end()
        i=js_skip(src,i)
        if src[i:i+1]!=":":raise ScriptError(f"no ':' after the property {key}")
        value,i=js_value(src,js_skip(src,i+1))
        if key in out:raise ScriptError(f"the property {key} appears twice in one record")
        out[key]=value;i=js_skip(src,i)
        if src[i:i+1]==",":i=js_skip(src,i+1);continue
        if src[i:i+1]!="}":raise ScriptError(f"something follows the value of {key} that is not ',' or '}}'")
def js_function_body(text,name):
    """The body of `function name(...) {...}` in the page's scripts; None when the page has no such function."""
    m=re.search(r"\bfunction\s+"+re.escape(name)+r"\s*\([^)]*\)\s*\{",text)
    if not m:return None
    i=start=m.end();depth=1
    while i<len(text):
        c=text[i]
        if c in "'\"":_,i=js_string(text,i);continue
        if text.startswith(("//","/*"),i):i=js_skip(text,i);continue
        depth+=(c=="{")-(c=="}")
        if depth==0:return text[start:i]
        i+=1
    raise ScriptError(f"the function {name} is not closed")
def scripts_text(s):
    """Every script of the page that runs (not the text/html row templates)."""
    return "\n".join(x.string or x.get_text() for x in s.find_all("script")
                     if clean(x.get("type") or "text/javascript").lower() in ("text/javascript","application/javascript","module"))
def setup_records(text,name):
    """[{property: text}] in source order from every call(...{...}) in the setup function; None = no such function."""
    body=js_function_body(text,name)
    if body is None:return None
    out,i,call=[],0,re.compile(r"\(\s*\{")
    while (m:=call.search(body,i)):
        obj,i=js_object(body,m.end()-1)
        out.append({k:v for k,v in obj.items() if v is not COUNTER})
    return out

def visible(c):return clean(c.get("type")).lower()!="hidden"
def in_dialog(e):
    """Inside a hidden Add / Edit / Upload dialog: its blank defaults are not the saved profile."""
    for p in [e,*e.parents]:
        if not isinstance(p,Tag):continue
        if p.get("role")=="dialog" or DIALOG.search(p.get("id") or "") or any(DIALOG.search(c) for c in p.get("class") or []):return True
    return False
def control_facts(c,shown):
    """One control as the page shows it: value, selected label AND value, ticked or not, disabled / read-only."""
    kind=c.name if c.name!="input" else (clean(c.get("type")).lower() or "text")
    d={"label":shown,"control":kind}
    if c.get("id"):d["id"]=c.get("id")
    if c.name=="select":
        opts=c.find_all("option");o=next((x for x in opts if x.has_attr("selected")),None)
        d["selectedMarked"]=o is not None       # none marked: the browser shows the first option
        o=o or (opts[0] if opts else None)
        d["selectedLabel"]=clean(o.get_text(" ",strip=True)) if o else ""
        d["selectedValue"]=clean(o.get("value") if o is not None and o.has_attr("value") else d["selectedLabel"])
    elif kind in ("checkbox","radio"):d["checked"]=c.has_attr("checked")
    else:d["value"]=ctl(c)
    if c.has_attr("disabled"):d["disabled"]=True
    if c.has_attr("readonly"):d["readOnly"]=True
    return d
def fields(sec,skip_ids=()):
    """label -> value of a form section (as before), + 'controls' (the detail of each) + 'completeness'.  Repeated
    labels never overwrite each other; hidden dialog defaults are left out."""
    out,controls,dialog={},{},0
    if not sec:return {"captureStatus":MISSING_SECTION,"completeness":{"outcome":NOT_FOUND,"sectionFound":False}}
    for g in sec.select(".form-group,.control-group"):
        if in_dialog(g):dialog+=1;continue
        l=g.select_one("label.control-label") or g.find("label")
        if not l:continue
        k=label(l.get_text(" ",strip=True));c=next((x for x in g.find_all(["input","select","textarea"]) if visible(x)),None)
        if not k or (c is not None and c.get("id") in skip_ids):continue
        key,n=k,2
        while key in out:key,n=f"{k} ({n})",n+1
        # no visible control: the text the page shows (e.g. a displayed value backed by a hidden input)
        out[key]=ctl(c) if c is not None else clean(g.get_text(" ",strip=True))[len(clean(l.get_text(" ",strip=True))):].strip()
        if c is not None:controls[key]=control_facts(c,k)
    comp={"outcome":EXTRACTED,"sectionFound":True,"fieldsRead":len(out),"dialogFieldsLeftOut":dialog}
    if not out and not skip_ids:                   # found, but nothing readable: never shown as blank values
        comp["outcome"]=FAILED
        return {"captureStatus":f"{NOT_CAPTURED} (no labelled field was found in this section)","completeness":comp}
    if controls:out["controls"]=controls
    out["completeness"]=comp
    return out
def compliance(s,rt,name,code):
    """AML / PEP / Financial Sanctions keyed by their control IDs: the comment label repeats 'Financial Sanctions
    Check Comment' in all three sections, so labels never decide which field a value belongs to."""
    sec=section(rt,name);ids=[f"entryForm{code}Check{p}" for p in ("Status","Result","Comment")]
    found=[s.find(id=x) for x in ids]
    if not sec and not any(found):return fields(None)
    out=fields(sec,skip_ids=ids) if sec else {"completeness":{"outcome":EXTRACTED,"sectionFound":False,"fieldsRead":0}}
    comp=out.pop("completeness");controls=out.pop("controls",{})
    for part,c in zip(("Status","Result","Comment"),found):
        key=f"{name} Check {part}"
        if c is None:out[key]="Not available in Register snapshot (the control was not found on the page)";continue
        g=c.find_parent(class_=lambda x:x and ("form-group" in x.split() or "control-group" in x.split()))
        l=g and (g.select_one("label.control-label") or g.find("label"))
        out[key]=ctl(c);controls[key]=control_facts(c,label(l.get_text(" ",strip=True)) if l else "")
    if controls:out["controls"]=controls
    comp.update(fieldsRead=len(out)-("controls" in out),controlsMissing=[f"{name} Check {p}" for p,c in zip(("Status","Result","Comment"),found) if c is None])
    out["completeness"]=comp
    return out
def signature(s):
    """The SAVED signature (img#imgSignature, never the upload preview img#imgUploadedSignature): present and readable
    or not.  Nothing of the image itself is kept or logged."""
    import base64
    img=s.find("img",id="imgSignature");facts={"savedSignature":"absent","uploadPreviewLeftOut":s.find(id="imgUploadedSignature") is not None}
    if img is None:return "No signature found in the profile",facts
    src=(img.get("src") or "").strip();facts["savedSignature"]="present"
    if not src:facts["imageData"]="none (the image has no source)"
    elif src[:5].lower()=="data:":
        try:
            head=base64.b64decode(src.split(",",1)[1][:64]+"==",validate=False)[:12]
            kind="PNG" if head.startswith(b"\x89PNG") else "JPEG" if head.startswith(b"\xff\xd8\xff") else "GIF" if head.startswith(b"GIF8") else \
                 "BMP" if head.startswith(b"BM") else "WEBP" if head[:4]==b"RIFF" and head[8:12]==b"WEBP" else ""
        except Exception:
            kind=""
        facts["imageData"]=f"readable {kind} image" if kind else "unreadable (not a known image format)"
    else:facts["imageData"]="a separate image address (not loaded by this snapshot)"
    ok=facts["imageData"].startswith("readable") or facts["imageData"].startswith("a separate")
    return ("Signature present in the profile" if ok else "Signature present in the profile, but its image could not be read"),facts

def header_row(r):
    cells=r.find_all(["th","td"],recursive=False)
    return bool(cells) and all(c.name=="th" or "TableHead" in (c.get("class") or []) for c in cells)
def row_values(r,cols,spec):
    """One saved row -> {column: {"value": text}}: each cell found by its id prefix WITHIN this row (generated
    suffixes never assumed); a cell without its id falls back to its column position, blank cells kept in place."""
    by_prefix,prefixes={},[p for _,_,p in spec]+list(HIDDEN_PREFIXES)
    for e in r.find_all(id=True):
        p=max((x for x in prefixes if e["id"].startswith(x)),key=len,default=None)
        if p and p not in HIDDEN_PREFIXES and p not in by_prefix:by_prefix[p]=e
    cells=r.find_all("td",recursive=False);out={};by_position=0
    for col,_,p in spec:
        e=by_prefix.get(p)
        if e is not None:v=clean(e.get("value")) if e.name in ("input","select","textarea") else clean(e.get_text(" ",strip=True))
        else:
            i=next((n for n,c in enumerate(cols) if normhead(c)==normhead(col)),None)
            v=clean(cells[i].get_text(" ",strip=True)) if i is not None and i<len(cells) else "";by_position+=1
        out[col]={"value":v}
    return out,by_position
def table_records(t,spec,prefix,placeholder):
    """Header, placeholder and saved rows told apart by structure: no thead needed, td.TableHead headers too."""
    cols,recs,placeholders,other,by_position=[],[],0,0,0
    for r in t.find_all("tr"):
        if r.find_parent("table") is not t:continue
        rid=r.get("id") or ""
        if header_row(r):
            cols=cols or [clean(c.get_text(" ",strip=True)) for c in r.find_all(["th","td"],recursive=False)];continue
        if rid==placeholder:placeholders+=1;continue
        if rid.startswith(prefix):
            v,n=row_values(r,cols,spec);recs.append(v);by_position+=n;continue
        other+=1                                   # not a header, the placeholder or a saved record: counted, not used
    return cols,recs,placeholders,other,by_position
def contact_section(s,rt,key):
    """One contact table of the profile, with its completeness block.  Every saved record, in page order."""
    table_id,prefix,placeholder,fn,heading,spec=CONTACTS[key]
    t=s.find("table",id=table_id)
    if t is None:
        sec=section(rt,heading);t=sec.find("table") if sec else None
    cols,rows,placeholders,other,by_position=table_records(t,spec,prefix,placeholder) if t is not None else ([],[],0,0,0)
    notes=[];error=""
    try:
        script=setup_records(scripts_text(s),fn)
    except ScriptError as e:
        script,error=None,str(e)
    decoded=0
    if script is not None:
        recs=[]
        for obj in script:
            row={}
            for col,prop,_ in spec:
                v=obj.get(prop,"")
                if ENTITY.search(v):v=html_unescape(v);decoded+=1      # as the browser shows it
                row[col]={"value":v}
            recs.append(row)
            unknown=sorted(set(obj)-{p for _,p,_ in spec}-{"Id","EmailEntry","PhoneEntry","AddressEntry","PhoneDbCountryId","AddressTypeId","AddressDbCountryId"})
            if unknown:notes.append("the page setup script has properties this snapshot does not map: "+", ".join(unknown))
        script=recs
    columns=[c for c in cols if normhead(c) not in ("action","edit")] or [c for c,_,_ in spec]
    comp={"sectionFound":t is not None,"tableRows":len(rows),"setupScriptRecords":None if script is None else len(script),
          "placeholdersExcluded":placeholders,"otherRowsIgnored":other}
    if error:notes.append(f"the page setup script could not be read: {error}")
    if decoded:notes.append(f"{decoded} value(s) held HTML character references, written as the page shows them")
    if by_position:notes.append(f"{by_position} cell(s) read by column position (no cell id)")
    status,out_rows=None,rows
    if rows and script and rows!=script:
        status=f"{NOT_CAPTURED} (the page's table rows and its setup script disagree: {len(rows)} row(s) against {len(script)} record(s))"
        comp.update(outcome=DISAGREE,source="table rows (the setup script's records are under setupScriptRows)")
    elif rows:
        comp.update(outcome=EXTRACTED,source="table rows"+(" (same as the page setup script)" if script else ""))
    elif script:
        out_rows=script;comp.update(outcome=EXTRACTED,source="page setup script (read as text, never run)")
    elif script==[]:
        status=BLANK_TABLE;comp.update(outcome=EMPTY,source="page setup script lists no records")
    elif error:
        status=f"{NOT_CAPTURED} (the records could not be read: {error})";comp.update(outcome=FAILED,source="none")
    elif t is None:
        status=MISSING_SECTION;comp.update(outcome=NOT_FOUND,source="none")
    else:
        status=f"{NOT_CAPTURED} (no saved row in the table and no setup script for it on the page)";comp.update(outcome=FAILED,source="none")
    comp.update(recordsDetected=len(out_rows) if comp["outcome"] in (EXTRACTED,DISAGREE) else 0,notes=list(dict.fromkeys(notes)))
    out={"captureStatus":status or "Captured","columns":columns,"rows":out_rows if comp["outcome"]!=EMPTY else []}
    if comp["outcome"]==DISAGREE:out["setupScriptRows"]=script
    comp["recordsWritten"]=len(out["rows"])
    out["completeness"]=comp
    return out
def fetch_party(d):
    r,s=soup_get(d["url"]);rt=root(s)
    sig,sig_facts=signature(s)
    return {"interestedPartyId":d.get("interestedPartyId",""),"nameFromAccount":d.get("nameFromAccount",""),
            "metadata":{"sourceUrl":r.url,"httpStatus":r.status_code,"retrievedUtc":datetime.now(timezone.utc).isoformat(),"pageTitle":clean(s.title.get_text()) if s.title else ""},
            "Personal Details":fields(section(rt,"Personal Details")),
            **{k:contact_section(s,rt,k) for k in CONTACTS},
            "Security checks":{**{name:compliance(s,rt,name,code) for name,code in COMPLIANCE},"Signature":sig,"Signature capture":sig_facts}}
def contact_counts(parties):
    """[(saved records detected, records in the snapshot)] per party and contact section."""
    return [((p.get(k) or {}).get("completeness",{}).get("recordsDetected",0),len((p.get(k) or {}).get("rows") or []))
            for p in parties for k in CONTACTS]
def link_url(d):
    pid=clean(d.get("interestedPartyId")) or (parse_qs(urlparse(d.get("url","")).query).get("InterestedPartyid") or [""])[0]
    if not pid:raise ValueError("Interested Party ID missing")
    return LINKS_PAGE.replace("{party}",quote(pid,safe=""))
def grouped(records,key):
    g={}
    for r in records:
        status=clean(r.get("Status")) or "Status not provided";role=clean(r.get("Role")) or "Role not provided";num=clean(r.get(key))
        if num and num not in g.setdefault(status,{}).setdefault(role,[]):g[status][role].append(num)
    nk="Customer numbers" if key=="Customer number" else "Account numbers"
    return [{"Status":s,"Roles":[{"Role":r,nk:n} for r,n in roles.items()]} for s,roles in g.items()]
def rendered_links(s,table_id,entity):
    t=s.find("table",id=table_id)
    if not t:return []
    hr=t.find("tr");hs=[clean(x.get_text(" ",strip=True)) for x in hr.find_all(["th","td"],recursive=False)] if hr else [];ix={x.casefold():i for i,x in enumerate(hs)};out=[]
    for row in t.find_all("tr"):
        cs=row.find_all("td",recursive=False)
        if not cs:continue
        ei=ix.get(entity.casefold(),0);ri=ix.get("role",1);si=ix.get("status",4 if entity=="Customer" else 3)
        if max(ei,ri,si)>=len(cs):continue
        text=clean(cs[ei].get_text(" ",strip=True));m=re.search(r"\(([^()]*)\)\s*$",text)
        if m:out.append({f"{entity} number":clean(m.group(1)),"Role":clean(cs[ri].get_text(" ",strip=True)),"Status":clean(cs[si].get_text(" ",strip=True))})
    return out
def fetch_links(d):
    r,s=soup_get(link_url(d));cu=rendered_links(s,"CustomerInterestedParties","Customer");ac=rendered_links(s,"AccountInterestedParties","Account")
    seen=lambda tid,rows:f"table found, {len(rows)} link(s) read" if s.find("table",id=tid) else "table not found on the page - nothing read from it"
    return {"interestedPartyId":d.get("interestedPartyId",""),"nameFromAccount":d.get("nameFromAccount",""),"metadata":{"sourceUrl":r.url,"httpStatus":r.status_code,"retrievedUtc":datetime.now(timezone.utc).isoformat()},"Customer Interested Parties":grouped(cu,"Customer number"),"Account Interested Parties":grouped(ac,"Account number"),
            "coverage":{"Customer Interested Parties":seen("CustomerInterestedParties",cu),"Account Interested Parties":seen("AccountInterestedParties",ac)}}
# ---------------------------------------------------------------------------------------------- account-detail pages
# The nine account-detail pages (P.DETAIL_SECTIONS; addresses pasted in setup, never in this code).  The readers follow
# the sanitised parser reference the user supplied (2026-11-03: element ids, headings and wording of the real pages),
# made strict: a page counts as read only when its own landmarks are on it, an empty result is never success, and a
# page is 'Confirmed empty' only on positive proof (FDT: the page says there are no instructions; Banking: no BANK
# ACCOUNT row on a fully loaded page - main form, account header and 'Banking Details' title all there).
UNEXPECTED,ACCOUNT_FAILED,REQUEST_FAILED="Unexpected page layout","Account validation failed","Request failed"
ACCEPTED=(EXTRACTED,EMPTY)      # any other outcome on any of the nine pages fails the account (its cases are held)
DETAIL_ACTIONS={"edit","save","cancel","remove","delete","add","add new","action","actions"}
HIDE_CLASSES={"hidden","hide","d-none"}         # a folded 'collapse' section is NOT hidden: its saved data is captured
NO_VALUE_INPUTS=("hidden","password","file","button","submit","reset","image")
STATIC="[id$='Static'], .form-control-static"
BOLD_HEADINGS={"Preferences","Post Settings","Email Settings","Portal Settings","Document Reporting Oversight Emails"}
INTRODUCING_FIRM="Details of Introducing Firm"
APPROPRIATENESS="Appropriateness Test completed for Complex Direct Investments."
NO_FDT="No FDT Instructions linked to Account."
BANK_LABEL=re.compile(r"BANK ACCOUNT\s+\d+",re.I)
FLAG_PANELS=("General","CS Request","Sub-type")
FLAG_ON,FLAG_OFF={"fa-check-circle-o","fa-check-circle","fa-check"},{"fa-circle-o"}
BLOCKS=("Account Summary","Interested Parties linked to the account","Interested Party links to other account numbers",
        "Nine Account Detail Sections")


class WrongAccount(RuntimeError):
    """A detail page answered for another account: never used."""


class WrongPage(RuntimeError):
    """A detail request ended on another page than the configured one (a redirect)."""


def detail_hidden(node,stop=None):
    """Hidden on the page (the hidden attribute, a hiding class, inline display:none / visibility:hidden) - the node or
    an ancestor below `stop`."""
    while isinstance(node,Tag) and node is not stop:
        style=re.sub(r"\s+","",node.get("style") or "").lower()
        if node.has_attr("hidden") or HIDE_CLASSES&set(node.get("class") or []) or "display:none" in style or "visibility:hidden" in style:return True
        node=node.parent
    return False
def detail_value(node):
    """The value shown now: a select's chosen option (never its option list), an input's value, a tick box's state, a
    textarea's text; in a container a visible static display wins (even when explicitly empty), else its text."""
    if node is None:return ""
    if node.name=="select":
        o=node.find("option",selected=True)
        if o is None and not node.has_attr("multiple"):o=node.find("option")
        return clean(o.get_text(" ",strip=True)) if o else ""
    if node.name=="input":
        t=clean(node.get("type") or "text").lower()
        if t in NO_VALUE_INPUTS:return ""
        return node.has_attr("checked") if t in ("checkbox","radio") else clean(node.get("value"))
    if node.name=="textarea":return clean(node.get_text(" ",strip=True))
    st=node.select_one(STATIC)
    if st is not None and not detail_hidden(st,node):return detail_value_text(st)
    return detail_value_text(node)
def detail_value_text(node):
    """Visible text: hidden parts, scripts, styles, buttons, icons, SVG, noscript, action / button links and soft-close
    controls left out; a control inside gives its current value (a tick box: Yes / No)."""
    parts=[]
    for ch in node.children:
        if isinstance(ch,NavigableString):
            if type(ch) is NavigableString:parts.append(str(ch))     # never a comment or other markup string
            continue
        if not isinstance(ch,Tag) or ch.name in ("script","style","button","svg","noscript","i") or detail_hidden(ch,node):continue
        cls=ch.get("class") or []
        if ch.get("role")=="button" or any(c.startswith(("fa-","glyphicon")) for c in cls) or "softclose" in (ch.get("id") or "").lower():continue
        if ch.name=="a" and (any(c=="btn" or c.startswith("btn-") for c in cls) or clean(ch.get_text(" ",strip=True)).casefold() in DETAIL_ACTIONS):continue
        if ch.name in ("input","select","textarea"):
            v=detail_value(ch);parts.append(("Yes" if v else "No") if isinstance(v,bool) else v)
        else:parts.append(detail_value_text(ch))
    return clean(" ".join(parts))
def detail_put(target,key,value):
    """A labelled value; the same label again with another value becomes a list (source order, each value once).
    Blank and action labels are ignored."""
    key=label(key)
    if not key or key.casefold() in DETAIL_ACTIONS:return
    if key not in target:target[key]=value
    elif target[key]!=value:
        if not isinstance(target[key],list):target[key]=[target[key]]
        if value not in target[key]:target[key].append(value)
def detail_own_rows(table):
    return [] if table is None else [r for r in table.find_all("tr") if r.find_parent("table") is table]
def detail_classes(e):return " ".join(e.get("class") or []).lower()
def detail_table_is_grid(table):
    """A table with a column-header row (th cells or TableSubHead), as opposed to label/value rows."""
    for r in detail_own_rows(table):
        cells=r.find_all(["th","td"],recursive=False)
        if len(cells)>=2 and (any(c.name=="th" for c in cells) or "tablesubhead" in detail_classes(r)
                              or sum("tablesubhead" in detail_classes(c) for c in cells)>=2):return True
    return False
def detail_is_head(r):
    cells=r.find_all(["th","td"],recursive=False)
    if not cells or (len(cells)==1 and cells[0].has_attr("colspan")):return False     # a caption row, not column names
    return any(c.name=="th" for c in cells) or "tablesubhead" in detail_classes(r) or any("tablesubhead" in detail_classes(c) for c in cells)
def detail_grid(table):
    """{"columns", "rows", "message"}: column and row order kept, action columns, hidden and soft-close rows left out,
    blank cells kept in place; a no-records / explanatory row goes to "message", never into the rows."""
    out={"columns":[],"rows":[],"message":""}
    if table is None:return out
    rows=detail_own_rows(table);head=next((i for i,r in enumerate(rows) if detail_is_head(r)),None);msgs=[]
    if head is None:
        msgs=[v for v in (detail_value(r) for r in rows if not detail_hidden(r,table)) if v]
    else:
        keep=[]
        for i,c in enumerate(rows[head].find_all(["th","td"],recursive=False)):
            k=label(detail_value(c))
            if detail_hidden(c,table) or k.casefold() in DETAIL_ACTIONS:continue
            k,n=k or f"Column {i+1}",2
            while k in [x for _,x in keep]:k,n=f"{label(detail_value(c)) or f'Column {i+1}'} ({n})",n+1
            keep.append((i,k))
        out["columns"]=[k for _,k in keep]
        for r in rows[head+1:]:
            if detail_hidden(r,table) or "softclose" in (r.get("id") or "").lower():continue
            tds=r.find_all("td",recursive=False)
            if not tds:continue
            if len(tds)==1 and (len(keep)>1 or tds[0].has_attr("colspan") or tds[0].select_one(".alert")):
                v=detail_value(r)
                if v:msgs.append(v)
                continue
            item={k:(detail_value(tds[i]) if i<len(tds) and not detail_hidden(tds[i],table) else "") for i,k in keep}
            if any(v not in ("",None) for v in item.values()):out["rows"].append(item)
    out["message"]=" ".join(dict.fromkeys(msgs))
    return out
def detail_group_value(g,lab):
    """A form / control group's value: its tick box, else a visible non-empty static display, else its visible
    controls' current values, else its visible text."""
    box=g.select_one("input[type=checkbox]")
    if box is not None and not detail_hidden(box,g):return box.has_attr("checked")
    shown=[detail_value_text(x) for x in g.select(STATIC) if not detail_hidden(x,g) and x is not lab and lab not in x.parents]
    if any(shown):return clean(" ".join(shown))
    ctls=[x for x in g.find_all(["select","textarea","input"]) if not detail_hidden(x,g)
          and not (x.name=="input" and clean(x.get("type")).lower() in NO_VALUE_INPUTS)]
    if ctls:return clean(" ".join(str(detail_value(x)) for x in ctls))
    return clean(" ".join(str(x) if type(x) is NavigableString else detail_value_text(x) for x in g.children
                          if x is not lab and (type(x) is NavigableString or (isinstance(x,Tag) and not detail_hidden(x,g) and lab not in x.descendants))))
def detail_fields(root):
    """Visible labelled values: label/value table rows (never a grid's rows, never a nested table's twice), form /
    control groups and definition lists."""
    out={}
    if root is None:return out
    for r in root.find_all("tr"):
        if detail_hidden(r,root):continue
        cells=r.find_all(["td","th"],recursive=False)
        if len(cells)<2 or any(c.find("table") for c in cells) or detail_table_is_grid(r.find_parent("table")):continue
        for i in range(0,len(cells)-1,2):
            if detail_hidden(cells[i],root) or detail_hidden(cells[i+1],root):continue
            v=detail_value(cells[i+1])
            if isinstance(v,str) and v.casefold() in DETAIL_ACTIONS:continue
            detail_put(out,detail_value(cells[i]),v)
    for g in root.select(".form-group, .control-group"):
        if detail_hidden(g,root) or g.find("table"):continue
        lab=g.select_one("label.control-label")
        if lab is not None:detail_put(out,detail_value(lab),detail_group_value(g,lab))
    for dt in root.find_all("dt"):
        dd=dt.find_next_sibling("dd")
        if dd is not None and not detail_hidden(dt,root) and not detail_hidden(dd,root):detail_put(out,detail_value(dt),detail_value(dd))
    return out
def detail_heading_text(node):
    """The section name when `node` is a heading (h1-h4, legend, panel heading, expandable or TableHead/TableSubHead
    table heading cell, bold subsection label), else ''."""
    if node.name in ("h1","h2","h3","h4","legend") or "panel-heading" in (node.get("class") or []):return label(detail_value(node))
    if node.name in ("td","th"):
        if clean(node.get_text(" ",strip=True))==INTRODUCING_FIRM:return INTRODUCING_FIRM
        if not node.find("table") and node.select_one("a.js-btn-expand-collapse"):return label(detail_value(node))
        if node.has_attr("colspan") and any(x.lower() in ("tablehead","tablesubhead") for x in node.get("class") or []):return label(detail_value(node))
    if node.name=="b" and clean(node.get_text()) in BOLD_HEADINGS:return label(detail_value(node))
    return ""
def detail_headings(root):
    """Every visible heading of the page, casefolded."""
    out=set()
    for n in root.find_all(True):
        h=detail_heading_text(n)
        if h and not detail_hidden(n,root):out.add(h.casefold())
    return out
def detail_named_region(root,name):
    """The part of the page under the visible heading `name`, up to the next other heading: its label/value rows (a
    grid's rows excepted) and form groups, plus each grid table whole (read separately, never as field pairs)."""
    target=next((n for n in root.find_all(True) if detail_heading_text(n).casefold()==name.casefold() and not detail_hidden(n,root)),None)
    w=BeautifulSoup("<div><table></table></div>","html.parser").div
    if target is None:return w
    started,grids=False,[]
    for node in root.descendants:
        if node is target:started=True;continue
        if not started or not isinstance(node,Tag) or target in node.parents:continue
        h=detail_heading_text(node)
        if h and h.casefold()!=name.casefold():break
        if detail_hidden(node,root) or any(g in node.parents for g in grids):continue
        if node.name=="table" and not node.find("table") and detail_table_is_grid(node):
            grids.append(node);w.append(BeautifulSoup(str(node),"html.parser"));continue
        if node.name=="tr" and not node.find("table"):
            t=node.find_parent("table")
            if (t is not None and detail_table_is_grid(t)) or any(detail_heading_text(c) for c in node.find_all(True)):continue
            w.table.append(BeautifulSoup(str(node),"html.parser"))
        elif "form-group" in (node.get("class") or []) and not node.find("table"):
            w.append(BeautifulSoup(str(node),"html.parser"))
    return w
def detail_section_fields(root,name,skip=()):
    """The labelled values under heading `name`; grids found there go under "tables" (except those in `skip`)."""
    w=detail_named_region(root,name);out=detail_fields(w)
    grids=[g for g in (detail_grid(t) for t in w.find_all("table",recursive=False)[1:]) if g not in skip]
    if grids:out["tables"]=grids
    return out
def detail_main_root(s):
    return s.select_one("form#frmmain") or s.select_one("#headerid") or s.body or s
def detail_find_table(root,required):
    """The first visible table (no table inside) whose first row names every column in `required`."""
    want=[x.casefold() for x in required]
    for t in root.find_all("table"):
        if t.find("table") or detail_hidden(t,root):continue
        first=next((r for r in detail_own_rows(t) if r.find_all(["th","td"],recursive=False)),None)
        names=[label(detail_value(c)).casefold() for c in first.find_all(["th","td"],recursive=False)] if first is not None else []
        if names and all(x in names for x in want):return t
    return None
def detail_static_or_selected(root,static_id,select_id):
    """The visible static display; the control's selected value only when the display is absent or empty."""
    d=root.find(id=static_id)
    if d is not None and not detail_hidden(d,root):
        v=detail_value(d)
        if v!="":return v
    c=root.find(id=select_id)
    return detail_value(c) if c is not None else ""
def detail_checked(node):
    """True ticked / False not ticked / None the control is not on the page (never guessed)."""
    return node.has_attr("checked") if node is not None else None
def detail_title_shown(s,text):
    """`text` is this page's own title: a heading, the browser title or the selected tab (not just one tab of nine)."""
    want=text.casefold()
    for e in s.find_all(["title","h1","h2","h3","h4","legend"])+s.select(".panel-heading, li.active, a.active, [aria-selected=true], [aria-current]"):
        if want in clean(e.get_text(" ",strip=True)).casefold() and not detail_hidden(e):return True
    return False
def page_account(s):
    """The account code the page's account header shows ('' = the page shows none)."""
    head=s.select_one("div.account-details")
    return clean((defs(head).get("Account Code") or {}).get("value")) if head else ""

# One reader per page: soup -> (data, facts).  facts: "seen" = this page's landmarks found (none = unexpected layout),
# "empty" + "emptyProof" = positively confirmed empty, "problem" = landmarks found but not readable, "absent" = optional
# parts not shown on the page, "counts".
def detail_parse_personal(s):
    rt=detail_main_root(s);heads=detail_headings(rt);t=detail_find_table(rt,["Product","Status","Account Number"]);product=detail_grid(t)
    data={"Account Details":detail_section_fields(rt,"Account Details",[product]),"Product Details":product,
          "Customer Details":detail_section_fields(rt,"Customer Details",[product])}
    return data,{"seen":[n for n in ("Account Details","Customer Details") if n.casefold() in heads]+(["Product table"] if t is not None else [])}
def detail_parse_contact(s):
    rt=detail_main_root(s);heads=detail_headings(rt);names=("Addresses","Contact Information","Preferred Contact Method")
    return {n:detail_section_fields(rt,n) for n in names},{"seen":[n for n in names if n.casefold() in heads],
                                                           "absent":[n for n in names if n.casefold() not in heads]}
def detail_parse_correspondence(s):
    rt=detail_main_root(s)
    pref=detail_static_or_selected(rt,"documentReportingPreferenceStatic","documentReportingPreference")
    data={"Preferences":{"Document Reporting Preference":pref,"Statement Frequency":detail_static_or_selected(rt,"stateFrequencyStatic","statementFrequency")}}
    kind=pref.casefold()
    if kind=="post":data["Post Settings"]={"Post Address":detail_static_or_selected(rt,"documentPostOptionStatic","documentPostOption")}
    elif kind=="email":data["Email Settings"]=detail_grid(rt.find(id="tblCorrespondenceSummary"))
    elif kind=="portal":data["Portal Settings"]={"Portal Address":detail_static_or_selected(rt,"documentPortalOptionStatic","documentPortalOption")}
    data["Document Reporting Oversight Emails"]=detail_grid(rt.find(id="tblOversightEmails"))
    facts={"seen":["Document Reporting Preference"] if rt.find(id=re.compile(r"^documentReportingPreference(Static)?$")) else []}
    if facts["seen"] and not pref:facts["problem"]="the Document Reporting Preference shows no value"
    elif pref and kind not in ("post","email","portal"):facts["notes"]=["no delivery-setting block is known for this preference"]
    return data,facts
def detail_parse_trading(s):
    rt=detail_main_root(s);data={};seen=[];absent=[]
    for name in ("Income Distribution Options","Interest Distribution Options"):
        if any(detail_heading_text(h).casefold()==name.casefold() and not detail_hidden(h,rt) for h in rt.find_all(["h2","h3"])):
            data[name]=detail_section_fields(rt,name);seen.append(name)
        else:absent.append(name)
    transfer=rt.find(id="incomeDistributionGroup")
    if transfer is not None and not detail_hidden(transfer,rt):data["Distribution Transfer"]=detail_fields(transfer);seen.append("Distribution Transfer")
    else:absent.append("Distribution Transfer")
    fees={}
    for name,tid in (("Fund-specific Rates","fundRebatesTable"),("Fund-specific Initial Charge Discount Rates","fundDiscountsTable")):
        t=rt.find(id=tid)
        if t is not None and not detail_hidden(t,rt):fees[name]=detail_grid(t);seen.append(name)
        else:absent.append(name)
    if fees:data["Fees and Commissions"]=fees
    if "trade settlement" in detail_headings(rt):data["Trade Settlement"]=detail_section_fields(rt,"Trade Settlement");seen.append("Trade Settlement")
    else:absent.append("Trade Settlement")
    return data,{"seen":seen,"absent":absent}
def detail_parse_banking(s):
    rt=detail_main_root(s);accounts=[];current=None;handled=set()
    for row in rt.find_all("tr"):
        if detail_hidden(row,rt) or any(id(t) in handled for t in row.parents if isinstance(t,Tag) and t.name=="table"):continue
        cells=row.find_all(["td","th"],recursive=False)
        if not cells:continue
        if any(c.find("table") for c in cells):
            if current is not None and len(cells)>=2 and not cells[0].find("table"):
                key=label(detail_value(cells[0]));nested=cells[1].find("table")
                if nested is not None and key:     # a composite value (e.g. a sort code in parts), in displayed order
                    parts=[detail_value(c) for c in nested.find_all("td") if not c.find("table") and not detail_hidden(c,nested)]
                    detail_put(current,key,"-".join(parts) if "sort" in key.lower() else clean(" ".join(parts)));handled.add(id(nested))
            continue
        first=detail_value(cells[0])
        if BANK_LABEL.fullmatch(first):current={"Bank Account Label":first};accounts.append(current)
        elif current is not None and len(cells)>=2:
            v=detail_value(cells[1])
            if not (isinstance(v,str) and v.casefold() in DETAIL_ACTIONS):detail_put(current,first,v)
    facts={"seen":["BANK ACCOUNT rows"] if accounts else [],"counts":{"bankAccounts":len(accounts)}}
    if not accounts:
        proof={"main form":s.select_one("form#frmmain") is not None,"account header":bool(page_account(s)),
               "Banking Details title":detail_title_shown(s,"Banking Details")}
        if all(proof.values()):
            facts.update(seen=list(proof),empty=True,emptyProof="no BANK ACCOUNT row on a fully loaded Banking Details page")
        else:facts["notes"]=["no BANK ACCOUNT row, and the page is not proven fully loaded (missing: "+", ".join(k for k,v in proof.items() if not v)+")"]
    return {"Bank Accounts":accounts},facts
def detail_parse_other(s):
    rt=detail_main_root(s);heads=detail_headings(rt)
    confirmation=detail_section_fields(rt,"Confirmation");box=rt.find(id="ML-Confirmation");confirmation["Confirmed"]=detail_checked(box)
    advised=rt.find(id="launderingreg1");test=detail_section_fields(rt,"Appropriateness Test")
    statement=rt.find(string=lambda x:x and clean(x)==APPROPRIATENESS)
    if statement is not None and not detail_hidden(statement.parent,rt):test["Description"]=clean(statement)
    data={"Money Laundering":{"Identity verification advised":detail_checked(advised),"Details of Individual":detail_section_fields(rt,"Details of Individual"),
                              "Confirmation":confirmation,"Details of Introducing Firm":detail_section_fields(rt,INTRODUCING_FIRM)},
          "Documentation":detail_section_fields(rt,"Documentation"),"Appropriateness Test":test}
    names=("Details of Individual","Confirmation",INTRODUCING_FIRM,"Documentation","Appropriateness Test")
    seen=[n for n,x in (("Identity verification advised",advised),("Confirmed",box)) if x is not None]+[n for n in names if n.casefold() in heads]
    return data,{"seen":seen,"absent":[n for n in names if n.casefold() not in heads]}
def detail_parse_suspended(s):
    rt=detail_main_root(s);box=lambda name:rt.find("input",attrs={"name":name,"type":"checkbox"})
    names={"Withdrawals":"Withdrawals","Deposits":"Deposits","Buys":"Buys","Sells":"Sells","Inter-Account Transfer":"InterAccountTransfer"}
    applies=rt.find(id="SubAccountList")
    data={"Applies to":detail_value(applies),"Payments":{k:detail_checked(box(names[k])) for k in ("Withdrawals","Deposits")},
          "Transactional":{k:detail_checked(box(names[k])) for k in ("Buys","Sells","Inter-Account Transfer")}}
    seen=(["Applies to"] if applies is not None else [])+[k for k,n in names.items() if box(n) is not None]
    return data,{"seen":seen,"absent":[k for k,n in names.items() if box(n) is None]}
def detail_parse_fdt(s):
    rt=detail_main_root(s)
    tables=[detail_grid(t) for t in rt.find_all("table") if not t.find("table") and not detail_hidden(t,rt) and detail_table_is_grid(t)]
    said=NO_FDT.casefold() in detail_value_text(rt).casefold();rows=sum(len(t["rows"]) for t in tables)
    block=(tables[0] if len(tables)==1 else {"tables":tables}) if tables else {"columns":[],"rows":[],"message":NO_FDT if said else ""}
    facts={"seen":(["instruction table"] if tables else [])+(["no-instructions message"] if said else []),"counts":{"instructionRows":rows}}
    if not rows and said:facts.update(empty=True,emptyProof="the page says: "+NO_FDT)
    elif tables and not rows:facts["problem"]="the instruction table has no rows and the page does not say there are no instructions"
    return {"FDT Instructions":block},facts
def detail_parse_flags(s):
    rt=detail_main_root(s);found={}
    for p in rt.select("div.panel"):
        head=p.select_one(".panel-heading");name=label(detail_value(head)) if head is not None else ""
        if name not in FLAG_PANELS or name in found or detail_hidden(p,rt):continue
        opts={}
        for g in p.select(".form-group"):
            lab=g.find("label")
            if lab is None or detail_hidden(g,p):continue
            icon=g.find("i");cls=set(icon.get("class") or []) if icon is not None else set()
            hist=g.select_one("div.col-xs")
            # a missing or unknown icon is None (unknown), never 'inactive'
            detail_put(opts,detail_value(lab),{"Active":True if cls&FLAG_ON else False if cls&FLAG_OFF else None,
                                               "History":detail_value(hist) if hist is not None else ""})
        found[name]=opts
    data={n:found[n] for n in FLAG_PANELS if n in found};t=rt.select_one("table.table-small-cell-padding")
    custom=t is not None and not detail_hidden(t,rt);data["Custom Details"]=detail_fields(t) if custom else {}
    return data,{"seen":list(data)[:-1]+(["Custom Details table"] if custom else []),
                 "absent":[n for n in FLAG_PANELS if n not in found]+([] if custom else ["Custom Details"])}
DETAIL_READERS=dict(zip(P.DETAIL_SECTIONS,(detail_parse_personal,detail_parse_contact,detail_parse_correspondence,
                                           detail_parse_trading,detail_parse_banking,detail_parse_other,detail_parse_suspended,
                                           detail_parse_fdt,detail_parse_flags)))
def detail_count(x):
    """How many values a reader found: blank text and unknown (None) states do not count, a grid counts its rows, a
    flag counts by its name."""
    if isinstance(x,dict):
        if "Active" in x:return 1
        if "rows" in x and "columns" in x:return len(x["rows"])+sum(detail_count(v) for k,v in x.items() if k=="tables")
        return sum(detail_count(v) for v in x.values())
    if isinstance(x,list):return sum(detail_count(v) for v in x)
    return 0 if x is None or x=="" else 1
def parse_account_detail_section(name,response,soup):
    """One retrieved page -> {"captureStatus", "data", "completeness"}.  An empty result never means success by itself."""
    reader=DETAIL_READERS.get(name)
    if reader is None:raise KeyError(f"unknown account-detail section: {name}")
    crash=""
    try:
        data,facts=reader(soup)
    except Exception as e:
        data,facts,crash={},{},f"the page could not be read ({type(e).__name__})"
    seen=facts.get("seen") or [];n=detail_count(data)
    if crash:outcome,why=FAILED,crash
    elif not seen:outcome,why=UNEXPECTED,"none of this page's landmarks was found"
    elif facts.get("problem"):outcome,why=FAILED,facts["problem"]
    elif facts.get("empty"):outcome,why=EMPTY,facts.get("emptyProof","")
    elif not n:outcome,why=FAILED,"the page's landmarks were found but no value could be read"
    else:outcome,why=EXTRACTED,""
    comp={"outcome":outcome,"sectionFound":bool(seen),"landmarksFound":seen,"valuesRead":n}
    if why:comp["reason"]=why
    comp.update(facts.get("counts") or {})
    if facts.get("absent") and outcome in ACCEPTED:comp["optionalSectionsNotShown"]=facts["absent"]   # only once inspected
    if facts.get("notes"):comp["notes"]=facts["notes"]
    if response is not None and getattr(response,"status_code",None) is not None:comp["httpStatus"]=response.status_code
    status="Captured" if outcome==EXTRACTED else f"Blank on Register ({why})" if outcome==EMPTY else \
           f"{NOT_CAPTURED} (unexpected page layout: {why})" if outcome==UNEXPECTED else f"{NOT_CAPTURED} ({why})"
    return {"captureStatus":status,"data":data,"completeness":comp}
def same_route(final,wanted):
    f,w=urlparse(final or ""),urlparse(wanted or "")
    return f.netloc.lower()==w.netloc.lower() and f.path.rstrip("/").casefold()==w.path.rstrip("/").casefold()
def fetch_account_section(a,name,route):
    """One account-detail page through soup_get (same session, retry, pause, stop): on its configured page, for this
    account (when the page shows one), then read.  A page for another account or another page raises (retried once)."""
    GATE.check()
    if not route:
        return {"captureStatus":f"{NOT_CAPTURED} (no page address is configured for it)","data":{},
                "completeness":{"outcome":NOT_FOUND,"sectionFound":False,"reason":"no page address is configured"}}
    url=route.replace("{account}",quote(a,safe=""));r,s=soup_get(url)
    if not same_route(r.url,url):
        e=WrongPage(f"{name}: System 2 answered from another page (redirected)");e.page=(r,s);raise e
    shown=page_account(s)
    if shown and P.norm_account(shown)!=P.norm_account(a):
        e=WrongAccount(f"{name}: the page shows another account");e.page=(r,s);raise e
    out=parse_account_detail_section(name,r,s);comp=out["completeness"]
    comp["accountCheck"]="matched" if shown else "account not shown on this page"
    if comp["outcome"] not in ACCEPTED:
        copies=capture_page(f"{a}-{name}",r,s,f"{comp['outcome']}: {comp.get('reason','')}")
        if copies:comp["pageCopies"]=copies
    return out
def section_failure(a,name,exc):
    """A page that could not be retrieved (after its retry) as a section entry - never blank data."""
    outcome=ACCOUNT_FAILED if isinstance(exc,WrongAccount) else REQUEST_FAILED
    page=getattr(exc,"page",None)
    why={WrongAccount:"the page was for another account",WrongPage:"System 2 redirected to another page",
         AccessDenied:"System 2 refused access",SessionExpired:"the System 2 sign-in expired",RunStopped:"the run was stopping",
         CancelledError:"cancelled (the run was stopping)"}.get(type(exc)) or type(exc).__name__+(f" (HTTP {page[0].status_code})" if page else "")
    comp={"outcome":outcome,"sectionFound":False,"reason":why}
    copies=[] if isinstance(exc,(RunStopped,CancelledError)) else capture(f"{a}-{name}",exc)
    if copies:comp["pageCopies"]=copies
    return {"captureStatus":f"{NOT_CAPTURED} ({why})","data":{},"completeness":comp}
def submit_account_sections(pool,a):
    return {n:pool.submit(with_retry,fetch_account_section,a,n,DETAIL_PAGES.get(n,"")) for n in P.DETAIL_SECTIONS}
def collect_account_sections(futures,a):
    """The nine entries in P.DETAIL_SECTIONS order (never completion order)."""
    out={}
    for n in P.DETAIL_SECTIONS:
        f=futures.get(n)
        try:
            out[n]=f.result() if f is not None else section_failure(a,n,RuntimeError("not requested"))
        except Exception as e:
            out[n]=section_failure(a,n,e)
    return out
def fetch_all_account_sections(a,pool=None):
    """All nine pages of one account, in order.  Pass the run's shared pool; without one a single small pool is used
    (never one per page, never a pool inside a pool)."""
    own=pool is None;pool=pool or ThreadPoolExecutor(max_workers=DETAIL_WORKERS)
    try:
        return collect_account_sections(submit_account_sections(pool,a),a)
    finally:
        if own:pool.shutdown(wait=True,cancel_futures=True)
def detail_tally(details):
    """Aggregate outcome counts and the names of the pages not captured: safe for logs (no values)."""
    outs=[(n,((e or {}).get("completeness") or {}).get("outcome")) for n,e in details.items()]
    return {"extracted":sum(o==EXTRACTED for _,o in outs),"confirmed_empty":sum(o==EMPTY for _,o in outs),
            "not_found":sum(o==NOT_FOUND for _,o in outs),"failed":sum(o not in ACCEPTED and o!=NOT_FOUND for _,o in outs),
            "failed_sections":[n for n,o in outs if o not in ACCEPTED]}
def detail_rows(details):
    """Every table row kept in the detail sections (grids anywhere in their data)."""
    def walk(x):
        if isinstance(x,dict):return (len(x["rows"]) if "rows" in x and "columns" in x else 0)+sum(walk(v) for k,v in x.items() if k!="rows")
        return sum(walk(v) for v in x) if isinstance(x,list) else 0
    return walk([(e or {}).get("data") for e in details.values()])
def bank_count(details):
    return len(((details.get("05 Banking Details") or {}).get("data") or {}).get("Bank Accounts") or [])
def fdt_rows(details):
    return detail_rows({n:e for n,e in details.items() if n=="08 Future Dated Instructions"})


class SnapshotIncomplete(RuntimeError):
    """The written snapshot does not read back complete: never used, the account counts as failed."""


class DetailsIncomplete(SnapshotIncomplete):
    """An account-detail page was not captured: no snapshot is written, the account counts as failed."""


def snapshot_problems(obj,a,expect=None):
    """Why a snapshot cannot be used for account `a` ([] = usable).  `expect` = the counts read this run, which the
    written file must still hold.  Plain words only: never a value from the file."""
    if not isinstance(obj,dict) or list(obj)!=list(BLOCKS):return ["it does not hold exactly the four snapshot blocks"]
    out=[];summary=obj[BLOCKS[0]] if isinstance(obj[BLOCKS[0]],dict) else {}
    if not P.norm_account(a) or P.norm_account((summary.get("metadata") or {}).get("requestedAccount"))!=P.norm_account(a):
        out.append("it is not for the requested account")
    details=obj[BLOCKS[3]]
    if not isinstance(details,dict) or list(details)!=list(P.DETAIL_SECTIONS):
        out.append("its nine account-detail sections are missing or out of order");details={}
    for n,e in details.items():
        c=e.get("completeness") if isinstance(e,dict) else None
        if not isinstance(c,dict) or c.get("outcome") not in ACCEPTED or not isinstance(e.get("captureStatus"),str) or not isinstance(e.get("data"),dict):
            out.append(f"section '{n}' has no valid capture result")
    if not isinstance(obj[BLOCKS[1]],list) or not isinstance(obj[BLOCKS[2]],list):out.append("its Interested Party blocks are not lists")
    if expect and not out:
        parties,links=obj[BLOCKS[1]],obj[BLOCKS[2]]
        if len(parties)!=expect["parties"]:out.append(f"the snapshot file holds {len(parties)} Interested Party record(s) but {expect['parties']} were read")
        if len(links)!=expect["links"]:out.append(f"the snapshot file holds {len(links)} link result(s) but {expect['links']} were read")
        written=[w for _,w in contact_counts(parties)]
        if written!=expect["contacts"]:out.append(f"the snapshot file holds {sum(written)} contact record(s) but {sum(expect['contacts'])} were read")
        for what,count,key in (("bank account(s)",bank_count,"banks"),("future-dated instruction row(s)",fdt_rows,"fdt"),
                               ("table row(s) in the detail sections",detail_rows,"tables")):
            if count(details)!=expect[key]:out.append(f"the snapshot file holds {count(details)} {what} but {expect[key]} were read")
    return out
def write_result(target,a,data,parties,links,details):
    """One snapshot per account for the run's day; the coordinator copies it into each case folder of that account.
    Written under a staging name and read back first: only a complete file gets the real name (the coordinator can
    never pick up one that fails); a failing one is set aside as .incomplete.json, never reused.  Nothing is written
    unless every account-detail page was read or positively confirmed empty."""
    target=Path(target);bad=detail_tally(details)["failed_sections"]
    if bad:
        target.unlink(missing_ok=True)
        raise DetailsIncomplete("account-detail page(s) not captured: "+", ".join(bad))
    data.pop("_interestedPartyLinks",None);result=dict(zip(BLOCKS,(data,parties,links,details)))
    expect={"parties":len(parties),"links":len(links),"contacts":[d for d,_ in contact_counts(parties)],
            "banks":bank_count(details),"fdt":fdt_rows(details),"tables":detail_rows(details)}
    stage=target.with_name(f".{target.name}.staging")
    P.write_json(stage,result)
    problems=snapshot_problems(json.loads(stage.read_text("utf-8")),a,expect)
    if problems:
        os.replace(stage,target.with_name(target.stem+".incomplete.json"))     # kept for review, never reused
        target.unlink(missing_ok=True)                                         # a failed account never leaves a snapshot
        raise SnapshotIncomplete("; ".join(problems))
    os.replace(stage,target)
    print(f"SAVED {a}",flush=True)
def reusable(path,a):
    """A snapshot made earlier today is reused only if it reads back complete for this account; otherwise it is set
    aside as .incomplete.json and the account is fetched again.  -> (reuse it?, why not)"""
    if not path.exists():return False,""
    try:
        problems=snapshot_problems(json.loads(path.read_text("utf-8")),a)
    except (OSError,ValueError) as e:
        problems=[f"it could not be read ({type(e).__name__})"]
    if not problems:return True,""
    try:
        os.replace(path,path.with_name(path.stem+".incomplete.json"))
    except OSError:
        pass
    return False,problems[0]

def assemble(d,pr,pe,lr,le):
    """One account's parties and links from the pages read (pr/lr by address) and the failures (pe/le) -> (parties,
    links, pages missing).  A party page that failed keeps its error; nothing is left out silently."""
    parties=[];links=[];missing=0
    for x in d["_interestedPartyLinks"]:
        u=x["url"];lu=link_url(x);missing+=(u not in pr)+(lu not in lr)
        # the profile's separate 'Interested Party Link' tab is its own page (fetch_links): say if it was read
        tab=("Inspected - see 'Interested Party links to other account numbers'" if lu in lr
             else f"Not inspected - the link page could not be read ({le.get(lu,'Unknown')})")
        parties.append({**pr.get(u,{"interestedPartyId":x.get("interestedPartyId",""),"nameFromAccount":x.get("nameFromAccount",""),"metadata":{"sourceUrl":u,"retrievalError":pe.get(u,"Unknown")}}),"Interested Party Link tab":tab});links.append(lr.get(lu,{"interestedPartyId":x.get("interestedPartyId",""),"nameFromAccount":x.get("nameFromAccount",""),"metadata":{"sourceUrl":lu,"retrievalError":le.get(lu,"Unknown")},"Customer Interested Parties":[],"Account Interested Parties":[]}))
    return parties,links,missing

# ---------------------------------------------------------------------------------------------- pipeline

class Gate:
    """One decision point for every worker thread: go on, wait for your sign-in (pause), or stop."""
    def __init__(self,control,sender):
        self.control,self.sender=control,sender
        self.lock=threading.Lock(); self.ready=threading.Event(); self.ready.set()
        self.denied=None; self._stop=(0.0,None)
    def stopped(self):
        if self.denied:return {"reason":self.denied}
        at,val=self._stop
        if time.monotonic()-at>1.0:                # the stop file is read at most once a second, not per request
            val=self.control.stopped(); self._stop=(time.monotonic(),val)
        return val
    def check(self):
        if self.stopped():raise RunStopped("run stopping")
    def deny(self,reason):
        with self.lock:
            if not self.denied:
                self.denied=reason
                self.control.stop("part2",f"{reason} - System 2 retrieval stopped after one retry so the system is not hammered")
                self.control.event("part2",f"ACCESS DENIED: {reason} - stopping the whole run")
    def expired(self,reason):
        """Pause until you sign in again and press Enter (Part 3's console asks), then fresh cookies for every thread."""
        with self.lock:
            if self.ready.is_set():
                self.ready.clear()
                seq=self.control.pause("part2",reason)
                self.control.event("part2",f"PAUSED: {reason}")
                threading.Thread(target=self._wait_resume,args=(seq,),daemon=True).start()
        while not self.ready.wait(0.5):
            self.check()
    def _wait_resume(self,seq):
        global COOKIES,UA,COOKIE_GEN
        while not self.stopped():
            if self.control.resumed()>=seq:
                try:
                    COOKIES,UA=auth(self.sender); COOKIE_GEN+=1
                    self.control.event("part2","System 2 sign-in found again - continuing")
                    break
                except SessionExpired as exc:
                    seq=self.control.pause("part2",str(exc))
            time.sleep(0.5)
        self.ready.set()


GATE=None


def with_retry(fn,*a):
    """One retry for a failed request; an expired sign-in waits for you (not counted); Access Denied twice stops all."""
    tries=0
    while True:
        try:
            return fn(*a)
        except SessionExpired as exc:
            GATE.expired(str(exc))
        except RunStopped:
            raise
        except AccessDenied as exc:
            if tries:
                GATE.deny(str(exc))
                raise
            tries+=1; time.sleep(2)
        except Exception:
            if tries:raise
            tries+=1; time.sleep(1)


def check_system2():
    """The sign-in check: cookies in the systems Edge, and System 2's page shows 'logout'.  (ok, problem)"""
    global COOKIES,UA,COOKIE_GEN
    try:
        COOKIES,UA=auth(GATE.sender); COOKIE_GEN+=1
        soup_get(CHECK_URL)
        return True,""
    except AccessDenied as exc:
        capture("sign-in-check",exc);return False,f"{exc} - check your access to System 2"
    except SessionExpired as exc:
        capture("sign-in-check",exc);return False,f"{exc} - sign in to System 2 in the systems Edge window"
    except Exception as exc:
        capture("sign-in-check",exc);return False,f"System 2 could not be opened ({type(exc).__name__})"


def snapshot_facts(path):
    """When a snapshot file was made and how big it is (same-day reuse is decided by the day folder alone)."""
    try:
        st=path.stat();return {"file_time":datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(timespec="seconds"),"bytes":st.st_size}
    except OSError:
        return {}


def run_pipeline(run_dir):
    global GATE,COOKIES,UA,COOKIE_GEN,CAPTURES
    import copilot_case_sender as sender            # Part 3's storage rules and DevTools helpers, reused as-is
    cfg=sender.load_config()
    policy,problems=sender.validate_config(cfg)
    if problems:raise RuntimeError("configuration problems: "+"; ".join(problems))
    sender.bootstrap_storage(cfg,policy)            # TEMP, caches, write guard: nothing on the system drive
    load_libs(); configure(cfg)
    paths=P.Paths(cfg.runtime_root,cfg.cases_root,run_dir); control=P.Control(paths); CAPTURES=paths.captures
    say=lambda text,level="minimal": control.event("part2",text,level)
    plan=P.read_plan(paths); day=plan.get("day")    # the run's day, even if the run goes past midnight
    cases=[c for c in plan["cases"] if c.get("needs")=="download"]
    rows=[{**c.get("row",{}),"_account":c["account"],"_csvRowNumber":str(c.get("row_number",""))} for c in cases]
    accounts=list(dict.fromkeys(c["account"] for c in cases))
    paths.snapshots(day).mkdir(parents=True,exist_ok=True)
    todo=[]
    for a in accounts:                              # today's file is reused only if it reads back complete
        ok,why=reusable(paths.snapshot(a,day),a)
        if not ok:todo.append(a)
        if why:say(f"account {a}: today's snapshot was set aside ({why}) - fetching it again")
    GATE=Gate(control,sender)
    answered=0
    while not control.is_go():                      # answer every sign-in check until you pressed Enter and all is ready
        if control.stopped():
            control.finished("part2",saved=0,failed=0); return 0
        seq=control.check_seq()
        if seq>answered:
            ok,problem=check_system2(); control.answer_check("part2",seq,ok,problem); answered=seq
        time.sleep(0.3)
    if len(todo)<len(accounts):
        say(f"{len(accounts)-len(todo)} account snapshot(s) already made today - reused")
    failed={}; written=0; started=time.perf_counter()
    facts={a:{"state":"reused (made earlier today)",**snapshot_facts(paths.snapshot(a,day))} for a in accounts if a not in todo}
    try:
        if todo:
            COOKIES,UA=auth(sender); COOKIE_GEN+=1
            say(f"fetching {len(todo)} account snapshot(s) in bulk")
            ar={};pf={};lf={};sf={}
            with ThreadPoolExecutor(max_workers=PARTY_WORKERS) as pp:   # party, link and account-detail pages
                with ThreadPoolExecutor(max_workers=min(ACCOUNT_WORKERS,len(todo))) as ap:
                    fs={ap.submit(with_retry,fetch_account,a,rows):a for a in todo}
                    for f in as_completed(fs):
                        a=fs[f]
                        try:
                            d=f.result();ar[a]=d
                            for x in d["_interestedPartyLinks"]:
                                if x["url"] not in pf:pf[x["url"]]=pp.submit(with_retry,fetch_party,x)
                                u=link_url(x)
                                if u not in lf:lf[u]=pp.submit(with_retry,fetch_links,x)
                            sf[a]=submit_account_sections(pp,a)
                            say(f"account {a} read","detail")
                        except RunStopped:
                            pass
                        except Exception as e:
                            failed[a]=f"{type(e).__name__}: {e}"
                            facts[a]={"state":"failed","error":failed[a],"captures":capture(a,e)}
                            say(f"account {a} FAILED after one retry: {type(e).__name__}")
                        if GATE.stopped():                  # nothing still waiting is requested
                            for other in [*fs,*pf.values(),*lf.values(),*(x for s in sf.values() for x in s.values())]:other.cancel()
                pr={};pe={};lr={};le={}
                for u,f in pf.items():
                    try:pr[u]=f.result()
                    except Exception as e:pe[u]=f"{type(e).__name__}: {e}"
                for u,f in lf.items():
                    try:lr[u]=f.result()
                    except Exception as e:le[u]=f"{type(e).__name__}: {e}"
                details={a:collect_account_sections(s,a) for a,s in sf.items()}
            if not GATE.stopped():
                for a in todo:
                    d=ar.get(a)
                    if not d:continue
                    parties,links,missing=assemble(d,pr,pe,lr,le)
                    sections=[(p.get(k) or {}).get("completeness",{}) for p in parties for k in CONTACTS]
                    unread=sum(c.get("outcome") in (FAILED,DISAGREE) for c in sections)
                    dets=details.get(a) or collect_account_sections({},a);tally=detail_tally(dets)
                    # aggregate counts and page names only - never a value read from a page
                    dfacts={"detail_sections":tally,"bank_accounts_written":bank_count(dets),"future_instruction_rows_written":fdt_rows(dets)}
                    try:
                        write_result(paths.snapshot(a,day),a,d,parties,links,dets);written+=1
                        facts[a]={"state":"saved this run","parties":len(parties),"party_pages_missing":missing,
                                  "contact_records":{k:[sum(((p.get(k) or {}).get("completeness") or {}).get(n,0) for p in parties)
                                                        for n in ("recordsDetected","recordsWritten")] for k in CONTACTS},
                                  "contact_sections_not_captured":unread,**dfacts,**snapshot_facts(paths.snapshot(a,day))}
                        say(f"account {a}: detail pages - {tally['extracted']} read, {tally['confirmed_empty']} confirmed empty","detail")
                        if missing:say(f"account {a}: snapshot saved, but {missing} party detail page(s) could not be read")
                        if unread:say(f"account {a}: {unread} contact table(s) could not be read - marked 'Not captured' in the snapshot")
                    except Exception as e:
                        failed[a]=str(e) if isinstance(e,DetailsIncomplete) else f"Output: {type(e).__name__}: {e}"
                        facts[a]={"state":"failed","error":failed[a],**dfacts,
                                  "page_copies":[c for x in dets.values() for c in (x.get("completeness") or {}).get("pageCopies",[])]}
                        say(f"account {a} FAILED: {failed[a]}" if isinstance(e,DetailsIncomplete) else f"account {a} FAILED while saving: {type(e).__name__}")
        say(f"finished: {written} snapshot(s) saved, {len(failed)} failed, in {time.perf_counter()-started:.0f}s")
    finally:
        P.write_json(paths.sub("status")/"part2-failed.json",failed)
        try:
            P.write_json(paths.diagnostics("part2")/"accounts.json",{"day":day,"accounts":facts,"denied":GATE.denied or "","written":P.now()})
        except Exception:
            pass
        control.finished("part2",saved=written,failed=len(failed),denied=GATE.denied or "")
    return 1 if failed or GATE.denied else 0


def main(argv=None):
    p=argparse.ArgumentParser(description=TITLE+" (started by orchestrator/run_pipeline.py)")
    p.add_argument("--run-dir",required=True)
    args=p.parse_args(argv)
    print(TITLE,flush=True)
    try:
        return run_pipeline(args.run_dir)
    except Exception as exc:
        traceback.print_exc()
        try:
            import copilot_case_sender as sender
            cfg=sender.load_config()
            control=P.Control(P.Paths(cfg.runtime_root,cfg.cases_root,args.run_dir))
            control.event("part2",f"Part 2 stopped: {type(exc).__name__}: {exc}")
            if not control.is_finished("part2"):control.finished("part2",error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        return 1


if __name__=="__main__":
    raise SystemExit(main())
