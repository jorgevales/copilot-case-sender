#!/usr/bin/env python3
"""PART 2 - System 2 snapshots: fast bulk account-platform extractor (the account snapshot script, in the daily pipeline).

For every account of the run (unique, from the CSV), in bulk with parallel authenticated requests: the account summary,
its linked Interested Parties and their other customer/account relationships -> one JSON snapshot per account in
RUNTIME_ROOT\\pipeline\\snapshots\\<today>\\<ACCOUNT>_Account_Summary_Interested_Parties_and_Links.json.  The coordinator
copies it into each case folder of that account when Part 1 has finished the case.  A snapshot made earlier the same
day is reused.

Started by orchestrator/run_pipeline.py with --run-dir; never asks anything on the console.  Kept from the original
script: every parser, route, field label, the 24/40 worker pools and the output schema.  Changed for the pipeline:
  - the sign-in cookies are read from the systems Edge directly over its DevTools endpoint (stdlib, loopback only) -
    no second Playwright driver to start;
  - Access Denied (HTTP 401/403 or a denial page), after one retry, STOPS THE WHOLE RUN (shared stop signal), so the
    system is not hammered; an expired sign-in PAUSES this part until you sign in again and press Enter;
  - any other failed account is retried once, then reported (its cases are held, never sent without a snapshot);
  - a party/link page is requested once (the original's eager setdefault could submit duplicates).
"""
from __future__ import annotations

import argparse,json,os,re,sys,threading,time,traceback
from concurrent.futures import ThreadPoolExecutor,as_completed
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
requests=BeautifulSoup=Tag=HTTPAdapter=None

# System 2's pages are pasted once in setup and kept only in the local config - never written in this code.
PORT=9223; BASE=""; CHECK_URL=""; ACCOUNT_PAGE=""; PARTY_MARKER=""; LINKS_PAGE=""; ACCOUNT_COLUMN="Client Account Number"
ACCOUNT_WORKERS=24; PARTY_WORKERS=40; CONNECTIONS=80
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
    global requests,BeautifulSoup,Tag,HTTPAdapter
    import requests as _requests
    from bs4 import BeautifulSoup as _BS,Tag as _Tag
    from requests.adapters import HTTPAdapter as _HA
    requests,BeautifulSoup,Tag,HTTPAdapter=_requests,_BS,_Tag,_HA


def configure(cfg):
    global PORT,BASE,CHECK_URL,ACCOUNT_PAGE,PARTY_MARKER,LINKS_PAGE
    u=urlparse(cfg.system2_url)
    PORT=int(cfg.systems_cdp_port); BASE=f"{u.scheme}://{u.netloc}"; CHECK_URL=cfg.system2_url
    ACCOUNT_PAGE=cfg.system2_account_page; PARTY_MARKER=cfg.system2_party_marker.lower(); LINKS_PAGE=cfg.system2_party_links_page


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
    if CAPTURES is None or not page:return []
    r,s=page;base=re.sub(r"[^A-Za-z0-9._-]+","_",f"system2-{label}")[:90]
    try:
        CAPTURES.mkdir(parents=True,exist_ok=True)
        title=clean(s.title.get_text()) if s.title else ""
        (CAPTURES/f"{base}.txt").write_text(P.REVIEWER_NOTE+f"\n\nHTTP {r.status_code}\naddress: {P.strip_query(r.url)}\ntitle: {title}\n"
                                            f"error: {type(exc).__name__}: {exc}\n\n"+P.strip_queries(clean(s.get_text(" ",strip=True)))[:100000],encoding="utf-8")
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
class SnapshotIncomplete(RuntimeError):
    """The written snapshot holds fewer contact records than were read: never used, the account counts as failed."""
def write_result(target,a,data,parties,links):
    """One snapshot per account for the run's day; the coordinator copies it into each case folder of that account.
    Read back after writing: every contact record read from the pages must be in the file, or it is set aside."""
    data.pop("_interestedPartyLinks",None);result={"Account Summary":data,"Interested Parties linked to the account":parties,"Interested Party links to other account numbers":links}
    P.write_json(target,result)
    back=json.loads(Path(target).read_text("utf-8")).get("Interested Parties linked to the account") or []
    detected=[d for d,_ in contact_counts(parties)];written=[w for _,w in contact_counts(back)]
    if detected!=written or len(back)!=len(parties):
        os.replace(target,target.with_name(target.stem+".incomplete.json"))     # kept for review, never reused
        raise SnapshotIncomplete(f"the snapshot file holds {sum(written)} contact record(s) but {sum(detected)} were read")
    print(f"SAVED {a}",flush=True)

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
    todo=[a for a in accounts if not paths.snapshot(a,day).exists()]
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
            ar={};pf={};lf={}
            with ThreadPoolExecutor(max_workers=PARTY_WORKERS) as pp:
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
                            say(f"account {a} read","detail")
                        except RunStopped:
                            pass
                        except Exception as e:
                            failed[a]=f"{type(e).__name__}: {e}"
                            facts[a]={"state":"failed","error":failed[a],"captures":capture(a,e)}
                            say(f"account {a} FAILED after one retry: {type(e).__name__}")
                        if GATE.stopped():
                            for other in fs:other.cancel()
                pr={};pe={};lr={};le={}
                for u,f in pf.items():
                    try:pr[u]=f.result()
                    except Exception as e:pe[u]=f"{type(e).__name__}: {e}"
                for u,f in lf.items():
                    try:lr[u]=f.result()
                    except Exception as e:le[u]=f"{type(e).__name__}: {e}"
            if not GATE.stopped():
                for a in todo:
                    d=ar.get(a)
                    if not d:continue
                    parties,links,missing=assemble(d,pr,pe,lr,le)
                    sections=[(p.get(k) or {}).get("completeness",{}) for p in parties for k in CONTACTS]
                    unread=sum(c.get("outcome") in (FAILED,DISAGREE) for c in sections)
                    try:
                        write_result(paths.snapshot(a,day),a,d,parties,links);written+=1
                        facts[a]={"state":"saved this run","parties":len(parties),"party_pages_missing":missing,
                                  "contact_records":{k:[sum(((p.get(k) or {}).get("completeness") or {}).get(n,0) for p in parties)
                                                        for n in ("recordsDetected","recordsWritten")] for k in CONTACTS},
                                  "contact_sections_not_captured":unread,**snapshot_facts(paths.snapshot(a,day))}
                        if missing:say(f"account {a}: snapshot saved, but {missing} party detail page(s) could not be read")
                        if unread:say(f"account {a}: {unread} contact table(s) could not be read - marked 'Not captured' in the snapshot")
                    except Exception as e:
                        failed[a]=f"Output: {type(e).__name__}: {e}"
                        facts[a]={"state":"failed","error":failed[a]}
                        say(f"account {a} FAILED while saving: {type(e).__name__}")
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
