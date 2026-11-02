#!/usr/bin/env python3
"""PART 1 - System 1 documents (the bulk instruction documents + Audit Trail crawler, in the daily pipeline).

For each case of the run (CSV order), in the ONE System 1 tab of the systems Edge (port systems_cdp_port):
search the instruction ID -> open it -> verify the exact ID and the account on Overview (the account must be the CSV's)
-> download every Received Document + the Audit Trail into CASES_ROOT\\.work\\<id> - <ACCOUNT> -> back to My Inbox.
A finished case is announced in RUNTIME_ROOT\\pipeline\\downloads\\<id>.json; the coordinator adds the System 2 snapshot
and moves the folder into CASES_ROOT, where the Copilot sender (Part 3) picks it up while this part goes on.

Started by orchestrator/run_pipeline.py with --run-dir; never asks anything on the console (its output goes to the run
folder's logs\\part1.log, progress lines to the one console through events\\part1.jsonl).  Rules kept from the
original script: the UI steps, selectors, waits and download routes are unchanged.  Changed for the pipeline: the
host/brand come from the shared config, Edge is joined (never launched here), the work happens only in tabs on System 1's
host (your System 2 tab is never touched), Playwright starts the fast way (Part 3's driver start: drive letter, driver
env, no system-drive writes), and the sign-in check / go / stop signals of shared/pipeline.py are honoured.
"""
from __future__ import annotations

import argparse, asyncio, collections, hashlib, html, json, os, re, shutil, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Always import supporting modules from this script's own folder (embedded Python does not add it).
# Never .resolve(): it turns a mapped drive (S:) into its UNC form.
SCRIPT_DIR = Path(os.path.abspath(__file__)).parent
PROJECT = SCRIPT_DIR.parent

for _p in (SCRIPT_DIR, PROJECT, PROJECT / "3_copilot_sender"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

VISUAL_CURSOR_MODULE = SCRIPT_DIR / "studio_visual_cursor.py"

if not VISUAL_CURSOR_MODULE.is_file():
    matching_files = sorted(
        item.name
        for item in SCRIPT_DIR.iterdir()
        if "visual" in item.name.lower() or "cursor" in item.name.lower()
    )

    raise FileNotFoundError(
        "Required module was not found.\n"
        f"Expected file: {VISUAL_CURSOR_MODULE}\n"
        f"Script directory: {SCRIPT_DIR}\n"
        f"Similar files found: {matching_files or 'None'}"
    )

from shared import pipeline as P
from studio_visual_cursor import (
    CursorConfig,
    VisualCursor,
    visual_click,
    visual_screenshot,
)
# Pillow is imported only by the screenshot/PDF helpers (capture, details_pdf, knowledge) - the batch never calls them.
# Playwright is imported after the storage bootstrap (it must find the private dependency folder on S:).

TITLE = "Part 1 - System 1 documents"
# System 1's pages are pasted once in setup and kept only in the local config - never written in this code.
ENTRY = ""            # System 1 inbox page (My Inbox) - set from the config by configure()
ORIGIN = ""           # scheme://host of ENTRY: the only host this part works in
APP_BASE = ""         # ENTRY's folder: the inbox and case pages live under it
VIEWER_MARKER = ""    # text that marks a document's viewer link ("" = no direct downloads: one by one through the UI)
BRAND_TEXT = ""       # regex of the brand control's text (back to My Inbox)
BRAND_HTML = ""       # regex of its logo markup
DEFAULT_ID = "00000000000000000000000000000000"
WORK_ROOT = Path(".")  # CASES_ROOT\.work - set by configure()
GUIDANCE_DIR = ""      # one <Sub Worktype>.md per type: no file = the case is held before anything is downloaded
CONTRACT = ""          # the result contract (never taken for a Sub Worktype's instructions)
CAPTURES = None        # RUNTIME_ROOT\logs\captures\<run>: screenshot + page copy when a case or a document fails
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
POLL = 0.025


class CaseHeld(Exception):
    """This case must not be sent (e.g. System 1 shows another account than the CSV): no retry, reported as held."""


# A mistake in this program, not a page problem: it fails the same way every time, so it is never retried, its full
# trace goes to the report, and the same one on two cases in a row stops Part 1 (VDI 2026-11-02: one TypeError failed
# every case twice).
PROGRAM_ERRORS = (TypeError, AttributeError, NameError, KeyError, IndexError, ImportError)


def configure(cfg):
    global ENTRY, ORIGIN, APP_BASE, VIEWER_MARKER, BRAND_TEXT, BRAND_HTML, WORK_ROOT, GUIDANCE_DIR, CONTRACT
    ENTRY = cfg.system1_url
    u = urlparse(ENTRY)
    ORIGIN = f"{u.scheme}://{u.netloc}"
    APP_BASE = ORIGIN + u.path.rsplit("/", 1)[0] + "/"
    VIEWER_MARKER = getattr(cfg, "system1_viewer_marker", "") or ""
    BRAND_TEXT, BRAND_HTML = cfg.system1_brand_text or "(?!)", cfg.system1_brand_html or "(?!)"   # (?!) never matches
    WORK_ROOT = Path(cfg.cases_root) / P.WORK_DIR
    GUIDANCE_DIR, CONTRACT = getattr(cfg, "guidance_dir", "") or "", getattr(cfg, "result_contract_path", "") or ""


def guidance_problem(sub_system1, sub_csv):
    """Why this case cannot be reviewed before anything is downloaded ('' = it can): no instructions file for its
    Sub Worktype (System 1's value wins over the CSV's, as in the hand-over)."""
    chosen,_=choose_sub_worktype(sub_system1,sub_csv)
    if GUIDANCE_DIR and P.guidance_for(chosen,GUIDANCE_DIR,exclude=[CONTRACT]) is None:
        return f"no instructions file for Sub Worktype '{chosen}' in the guidance folder - documents not downloaded"
    return ''


async def capture_page(page, label):
    """For the reviewing Copilot, when something fails: a screenshot, the visible text and the page file of `page`
    into CAPTURES (web addresses cut before '?').  Returns the file names; never raises."""
    if CAPTURES is None or page is None:
        return []
    names=[]
    try:
        CAPTURES.mkdir(parents=True,exist_ok=True)
        base=safe(label,'capture',max_length=90)
        try:
            await visual_screenshot(page,path=str(CAPTURES/f'{base}.png'),full_page=False,animations='disabled',caret='hide',timeout=10000)
            names.append(f'{base}.png')
        except Exception:
            pass
        title=await asyncio.wait_for(page.title(),5)
        text=await asyncio.wait_for(page.evaluate("()=>document.body?document.body.innerText:''"),5)
        (CAPTURES/f'{base}.txt').write_text(f'address: {P.strip_query(page.url)}\ntitle: {title}\n\n'+P.strip_queries(text)[:200000],encoding='utf-8')
        names.append(f'{base}.txt')
        (CAPTURES/f'{base}.html').write_text(P.strip_queries(await asyncio.wait_for(page.content(),10))[:3000000],encoding='utf-8')
        names.append(f'{base}.html')
    except Exception:
        pass
    return names


def on_system1(page) -> bool:
    try:
        return bool(ORIGIN) and page.url.startswith(ORIGIN)
    except Exception:
        return False


def in_app(url) -> bool:
    """On one of System 1's inbox / case pages (under the inbox page's folder)."""
    return bool(APP_BASE) and str(url or "").startswith(APP_BASE)


def now(): return datetime.now(timezone.utc).isoformat()
def safe(v, fallback="Unknown", max_length=96):
    raw = re.sub(r"\s+", " ", BAD.sub("_", str(v or ""))).strip().rstrip(". ")
    if not raw: return fallback
    if len(raw) <= max_length: return raw
    suffix = Path(raw).suffix
    stem = raw[:-len(suffix)] if suffix else raw
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    keep = max(12, max_length-len(suffix)-len(digest)-1)
    return f"{stem[:keep].rstrip()}_{digest}{suffix}"

def event(items, name, /, **details):
    """Positional-only: a detail may itself be called `name` or `items` (VDI 2026-11-02: name=... crashed every case)."""
    items.append({"at": now(), "event": name, **details})
    print(name, flush=True)
def unique(folder, name):
    path = folder / safe(name, "download")
    i = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    return path


async def connect_systems_edge(playwright, sender, port):
    """Join the systems Edge (System 1 + 2) that the coordinator opened on `port`.  Never launches one: a second Edge
    on the same profile is impossible, and on another profile you would not be signed in."""
    payload = await sender.cdp_version(port, 0.75)
    if not sender.endpoint_valid(payload, port):
        raise RuntimeError(f"no Edge debugging endpoint on port {port} (the systems Edge window is not open)")
    browser = await asyncio.wait_for(playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000), 15)
    print(f"Connected to the systems Edge on port {port}", flush=True)
    return browser


async def page_is_usable(page):
    try:
        if page.is_closed():
            return False
        await page.evaluate("1")
        return True
    except Exception:
        return False


async def live(context, preferred=None):
    # Never select about:blank or create random tabs during normal processing.
    if preferred is not None and await page_is_usable(preferred):
        try:
            if preferred.url and preferred.url not in ('about:blank','edge://newtab/',''):
                return preferred
        except Exception: pass
    pages=[p for p in context.pages if on_system1(p)]   # pipeline: only System 1 tabs (never your System 2 tab)
    for page in reversed(pages):
        if not await page_is_usable(page): continue
        try:
            if in_app(page.url): return page
        except Exception: pass
    for page in reversed(pages):
        if not await page_is_usable(page): continue
        try:
            if page.url and page.url not in ('about:blank','edge://newtab/',''): return page
        except Exception: pass
    # Only recovery is allowed to create one page, and it navigates it immediately.
    page=await context.new_page()
    await page.goto(ENTRY,wait_until='commit',timeout=30000)
    return page

async def open_or_recover_inbox(context, page=None):
    # Reuse a healthy My Inbox tab first. Only navigate when no usable inbox exists.
    for candidate in reversed([p for p in context.pages if on_system1(p)]):
        if not await page_is_usable(candidate):
            continue
        try:
            text = await candidate.locator("body").inner_text(timeout=750)
        except Exception:
            text = ""
        if in_app(candidate.url) and "My Inbox" in text:
            return candidate

    candidate = await live(context, page)
    last_error = None
    for _ in range(3):
        if not await page_is_usable(candidate):
            candidate = await context.new_page()
        try:
            await candidate.goto(ENTRY, wait_until="commit", timeout=30000)
            return await wait_inbox(context, candidate)
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if "frame has been detached" not in message and "target page" not in message and "context" not in message:
                raise
            try:
                if not candidate.is_closed():
                    await candidate.close()
            except Exception:
                pass
            candidate = await context.new_page()
    raise RuntimeError(f"Unable to open My Inbox after replacing detached pages: {last_error}")


async def wait_inbox(context, page):
    end = time.monotonic() + 200
    while time.monotonic() < end:
        page = await live(context, page)
        try:
            if "My Inbox" in await page.locator("body").inner_text(timeout=1500): return page
        except Exception: pass
        await asyncio.sleep(0.05)
    raise TimeoutError("My Inbox not detected")


async def return_to_my_inbox(context, page, events):
    """Click the top-left brand control and wait through any transient error page.  The brand's text and logo
    patterns are the private config values system1_brand_text / system1_brand_html (case-insensitive regexes)."""
    page = await live(context, page)
    clicked = await page.evaluate(r"""async ([brandText, brandHtml]) => {
      const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
      const candidates=[...document.querySelectorAll('a,button,[role=button],img')].filter(visible);
      const text=new RegExp(brandText,'i'), logo=new RegExp(brandHtml,'i');
      let target=candidates.find(e=>text.test([
        e.innerText,e.textContent,e.title,e.getAttribute('aria-label'),e.getAttribute('alt'),e.outerHTML
      ].join(' ')));
      if(!target) target=candidates.find(e=>logo.test(e.outerHTML));
      if(!target) return false;
      const clickable=target.closest('a,button,[role=button],[onclick]')||target;
      clickable.setAttribute('data-s4-example-home','1');
      try{await window.__studio_visual_cursor_v1__?.previewElement(clickable)}catch{};clickable.click();
      return true;
    }""", [BRAND_TEXT, BRAND_HTML])
    if not clicked:
        raise RuntimeError('Top-left brand logo/button was not found (check system1_brand_text / system1_brand_html in setup)')
    event(events,'example-home-clicked')

    # The brand action can briefly open an Error page. As in Script 1, wait up to
    # 200 seconds and continue immediately when System 1 returns to its inbox pages.
    deadline=time.monotonic()+200
    last_url=''
    saw_error=False
    while time.monotonic()<deadline:
        page=await live(context,page)
        current=page.url
        if current!=last_url:
            event(events,'return-navigation-url',url=current)
            last_url=current
        try:
            text=await page.locator('body').inner_text(timeout=1000)
        except Exception:
            text=''
        if re.search(r'\berror\b',text,re.I):
            saw_error=True
        if in_app(current) and 'My Inbox' in text:
            event(events,'returned-to-my-inbox',url=current,errorPageObserved=saw_error)
            return page
        await asyncio.sleep(.05)
    raise TimeoutError(f'My Inbox did not return within 200 seconds after clicking the example organisation logo. Last URL: {last_url}')

async def notes_content_is_open(page):
    """Notes is open when Notes and Add a note are visible outside the sidebar."""
    return await page.evaluate(r"""()=>{
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim().toLowerCase();
      const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
      const sidebar=document.querySelector('#aua-hub-sidebar-sections');
      const nodes=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,span,div,p,button,a,label')]
        .filter(e=>visible(e)&&(!sidebar||!sidebar.contains(e)));
      const hasNotes=nodes.some(e=>clean(e.innerText||e.textContent)==='notes');
      const hasAddNote=nodes.some(e=>clean(e.innerText||e.textContent)==='add a note');
      return hasNotes&&hasAddNote;
    }""")

async def detect_open_section(page):
    # Check Notes first because its content can coexist with all four sidebar labels.
    if await notes_content_is_open(page):return 'Notes'
    return await page.evaluate(r"""()=>{
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
      const sidebar=document.querySelector('#aua-hub-sidebar-sections');
      const outsideText=[...document.querySelectorAll('main,[role=main],[class*=HubMainContainer],[class*=ContentNextToSidebar]')]
        .filter(e=>!sidebar||!sidebar.contains(e)).map(e=>clean(e.innerText||e.textContent)).join(' ');
      if(/Received Documents|Document Name/.test(outsideText))return 'Documents';
      if(/Audit Trail|Audit Trial|Download Audit Trail|View Process/.test(outsideText))return 'History';
      if(/Customer Details/.test(outsideText)||(/Client Name/.test(outsideText)&&/Instruction ID/.test(outsideText)))return 'Overview';
      return '';
    }""")

async def search_open(context,page,instruction_id,events,check_sub=None):
    """
    Submit the new instruction ID, wait a fixed 1.5 seconds for My Inbox to
    replace the previous filtered result, then click the only visible View.
    No result-ID matching is performed.  `check_sub(sub)` may hold the case
    (CaseHeld) on the result row's Sub WorkType BEFORE View is clicked.
    """
    page=await ensure_clean_inbox_search(page,timeout=60)
    field=page.locator('[data-bulk-search="1"]')
    await field.fill(instruction_id)
    if await field.input_value()!=instruction_id:
        raise RuntimeError('Instruction ID was not written into the search field')
    await field.press('Enter')
    event(events,'search-submitted',instructionId=instruction_id)

    # Required fixed delay: the old filtered View can remain visible briefly.
    await asyncio.sleep(1.5)
    page=await live(context,page)

    result=await page.evaluate(r"""() => {
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
      const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
      const views=[...document.querySelectorAll('button,a,[role=button]')]
        .filter(e=>visible(e)&&clean(e.innerText||e.value||e.textContent).toLowerCase()==='view');
      document.querySelectorAll('[data-s4view]').forEach(e=>e.removeAttribute('data-s4view'));
      if(views.length!==1)return {count:views.length,subWorkType:''};
      views[0].setAttribute('data-s4view','1');
      let sub='';let node=views[0];
      while(node&&node!==document.body){
        const text=clean(node.innerText);
        const match=text.match(/Sub WorkType\s*:\s*(.+?)(?=\s+Show more|\s+View|\s+Reassign|$)/i);
        if(match){sub=match[1].trim();break;}
        node=node.parentElement;
      }
      return {count:1,subWorkType:sub};
    }""")
    if result.get('count')!=1:
        event(events,'search-result-not-single',views=result.get('count'),waitSeconds=1.5)
        raise RuntimeError(
            f"the ID search in My Inbox showed {result.get('count')} View button(s) 1.5 seconds after searching "
            "(exactly one expected) - is the case in My Inbox? (Expected exactly one visible View; "
            f"found {result.get('count')})"
        )

    sub=result.get('subWorkType') or 'Unknown Sub WorkType'
    event(events,'single-view-found-after-fixed-wait',subWorkType=sub,waitSeconds=1.5)
    reason=check_sub(sub) if check_sub else ''
    if reason:
        event(events,'held-before-opening',reason=reason)
        raise CaseHeld(reason)
    await page.eval_on_selector('[data-s4view="1"]','async e=>{ try{await window.__studio_visual_cursor_v1__?.previewElement(e)}catch{};e.click(); }')

    # Only wait for the instruction page to render. Identity validation remains
    # on Overview later, before the destination folder is created.
    deadline=time.monotonic()+60
    last=''
    while time.monotonic()<deadline:
        page=await live(context,page)
        try:
            section_name=await detect_open_section(page)
            text=await page.locator('body').inner_text(timeout=1000)
            if section_name:
                event(events,'instruction-opened',initialSection=section_name,url=page.url)
                return page,sub,section_name
            last=f'section={section_name!r}, url={page.url}, text={text[:500]}'
        except Exception as exc:
            last=f'{type(exc).__name__}: {exc}'
        await asyncio.sleep(.10)
    raise TimeoutError('Clicked instruction did not render a recognisable section within 60 seconds: '+last)

async def identity(page):
    """Read exact Overview values from the labelled field rows."""
    return await page.evaluate(r"""() => {
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
      const visible=e=>{if(!e)return false;const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
      const out={customerName:'',customerNumber:'',accountNumber:'',instructionId:''};
      const wanted={
        'Client Name':'customerName',
        'Client Number':'customerNumber',
        'Client Account Number':'accountNumber',
        'Instruction ID':'instructionId'
      };
      const rows=[...document.querySelectorAll('[data-appway-test-id^="AwFoundation_LabeledFieldTemplate-3-"]')].filter(visible);
      for(const row of rows){
        const labelNode=row.querySelector('[data-appway-test-id^="AwFoundation_LabeledFieldTemplate-5-"]');
        const valueBox=row.querySelector('[data-appway-test-id^="AwFoundation_LabeledFieldTemplate-1-"]');
        const key=clean(labelNode?.innerText||labelNode?.textContent).replace(/:$/,'');
        const target=wanted[key];
        if(!target||!valueBox)continue;
        const valueNode=valueBox.querySelector('.xText-primaryx,.awLabel')||valueBox;
        const value=clean(valueNode.innerText||valueNode.textContent);
        if(value)out[target]=value;
      }
      return {
        customerName:out.customerName,
        customerNumber:out.customerNumber,
        accountNumber:out.accountNumber.toUpperCase(),
        instructionId:out.instructionId.toLowerCase(),
        labeledRows:rows.length
      };
    }""")

async def verified_overview_identity(context,page,instruction_id,events,timeout=45):
    """Open Overview and verify its exact LabeledFieldTemplate values."""
    page=await section(context,page,'Overview',events)
    deadline=time.monotonic()+timeout
    last={}
    while time.monotonic()<deadline:
        page=await live(context,page)
        try:
            meta=await identity(page)
            last={'identity':meta,'url':page.url}
            exact=meta.get('instructionId','').casefold()==instruction_id.casefold()
            account=meta.get('accountNumber','')
            valid_account=bool(re.fullmatch(r'[A-Z]{2}[A-Z0-9]{6,20}',account))
            if exact and valid_account:
                event(events,'identity-verified',instructionId=instruction_id,accountNumber=account,labeledRows=meta.get('labeledRows',0))
                return page,meta
        except Exception as exc:
            last={'error':f'{type(exc).__name__}: {exc}','url':getattr(page,'url','')}
        await asyncio.sleep(.10)
    raise TimeoutError('Overview fields did not verify: '+json.dumps(last,ensure_ascii=False))

async def wait_instruction_ready(context,page,instruction_id,events,timeout=45):
    end=time.monotonic()+timeout;last={}
    while time.monotonic()<end:
        page=await live(context,page)
        try:
            text=await page.locator('body').inner_text(timeout=1000)
            found=[x for x in ('Overview','Documents','Notes','History') if x in text]
            meta=await identity(page);last={'sections':found,'identity':meta,'url':page.url}
            if 'Overview' in found and len(found)>=3 and (instruction_id in text or 'Instruction ID' in text) and all(meta.values()):
                event(events,'instruction-ready',sections=found,url=page.url);return page,meta
        except Exception as exc:last={'error':f'{type(exc).__name__}: {exc}','url':getattr(page,'url','')}
        await asyncio.sleep(.10)
    raise TimeoutError('Instruction Overview did not fully render: '+json.dumps(last,ensure_ascii=False))

async def notes_content_is_open(page):
    """Confirm Notes outside the navigation sidebar with exact content markers."""
    return await page.evaluate(r"""()=>{
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim().toLowerCase();
      const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
      const sidebar=document.querySelector('#aua-hub-sidebar-sections');
      const nodes=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,span,div,p,button,a,label')]
        .filter(e=>visible(e)&&(!sidebar||!sidebar.contains(e)));
      const notes=nodes.some(e=>clean(e.innerText||e.textContent)==='notes');
      const add=nodes.some(e=>clean(e.innerText||e.textContent)==='add a note');
      return notes&&add;
    }""")

async def section(context,page,name,events):
    sid={'Overview':'sectionTitle1','Documents':'sectionTitle2','Notes':'sectionTitle3','History':'sectionTitle4'}[name]
    end=time.monotonic()+45;logged=False;clicked_at=0.0
    while time.monotonic()<end:
        page=await live(context,page)
        try:
            current=await detect_open_section(page)
            if current==name:return page
            if time.monotonic()-clicked_at>=1.0:
                found=await page.evaluate(r"""async ([id,label])=>{
                  const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
                  const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
                  const candidates=[...document.querySelectorAll('#aua-hub-sidebar-sections span,#aua-hub-sidebar-sections a,#aua-hub-sidebar-sections button,#aua-hub-sidebar-sections [role=button]')].filter(visible);
                  const e=document.getElementById(id)||candidates.find(x=>clean(x.innerText||x.textContent)===label);
                  if(!e)return false;const target=e.closest('[onclick],a,button,[role=button]')||e;try{await window.__studio_visual_cursor_v1__?.previewElement(target)}catch{};target.click();return true;
                }""",[sid,name])
                if found:
                    clicked_at=time.monotonic()
                    if not logged:event(events,'section-clicked',section=name);logged=True
        except Exception:pass
        await asyncio.sleep(.10)
    raise TimeoutError(name+' section did not fully render')

async def expand_show_more(page,max_clicks=30):
    clicks=0
    while clicks<max_clicks:
        try:
            clicked=await page.evaluate(r"""async ()=>{const c=v=>(v??'').toString().replace(/\s+/g,' ').trim().toLowerCase(),visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return!!(r.width||r.height)&&s.display!='none'&&s.visibility!='hidden'},e=[...document.querySelectorAll('button,a,[role=button]')].find(x=>visible(x)&&c(x.innerText||x.textContent)==='show more');if(!e)return false;try{await window.__studio_visual_cursor_v1__?.previewElement(e)}catch{};e.click();return true}""")
            if not clicked:break
            clicks+=1;await asyncio.sleep(.20)
        except Exception:break
    return clicks

async def capture(page,name,temp,states):
    """Capture all of the inner scroll pane in high-resolution viewport pages."""
    await expand_show_more(page)
    info=await page.evaluate(r"""()=>{const candidates=[...document.querySelectorAll('.xDesignSystemx_xHubMainContainer_ScrollPanex,[class*=HubMainContainer_ScrollPane],[class*=ScrollPane]')],e=candidates.find(x=>x.scrollHeight>x.clientHeight+20)||document.scrollingElement;if(e!==document.scrollingElement)e.scrollTop=0;else window.scrollTo(0,0);return{selector:e===document.scrollingElement?'__document__':'.'+[...e.classList].map(CSS.escape).join('.'),total:e.scrollHeight,view:e.clientHeight||innerHeight}}""")
    step=max(300,int(info['view']*.86));positions=list(range(0,max(1,info['total']),step)) or [0];bottom=max(0,info['total']-info['view'])
    if bottom not in positions:positions.append(bottom)
    positions=sorted(set(positions))
    base=await page.evaluate(r"""stage=>{const c=v=>(v??'').toString().replace(/\s+/g,' ').trim();return{stage,capturedAt:new Date().toISOString(),url:location.href,title:document.title,bodyText:c(document.body?.innerText||''),html:document.documentElement.outerHTML,controls:[...document.querySelectorAll('button,a,input,[role=button]')].map(e=>({tag:e.tagName.toLowerCase(),id:e.id||'',text:c(e.innerText||e.value||e.textContent),title:e.title||'',href:e.href||'',testId:e.getAttribute('data-appway-test-id')||'',html:e.outerHTML.slice(0,1500)}))}}""",name)
    for index,y in enumerate(positions,1):
        await page.evaluate(r"""([selector,y])=>{const e=selector==='__document__'?document.scrollingElement:document.querySelector(selector);if(e===document.scrollingElement)window.scrollTo(0,y);else e.scrollTop=y}""",[info['selector'],y]);await asyncio.sleep(.12)
        shot=temp/f"{safe(name)}_{index:03d}.png";await visual_screenshot(page,path=str(shot),full_page=False,animations='disabled',caret='hide',type='png')
        state=dict(base);state['stage']=f"{name} {index}/{len(positions)}";state['screenshot']=str(shot);state['scrollPosition']=y;state['scrollTotal']=info['total'];states.append(state)
    await page.evaluate(r"""selector=>{const e=selector==='__document__'?document.scrollingElement:document.querySelector(selector);if(e===document.scrollingElement)window.scrollTo(0,0);else e.scrollTop=0}""",info['selector'])

async def save_download(download, folder, preferred=None):
    path = unique(folder, preferred or download.suggested_filename or "download")
    await download.save_as(str(path)); return path


async def document_rows(page):
    """Resolve Received Documents rows even when View is an icon-only span.  Viewer links are found by the private
    `system1_viewer_marker` (none = every document goes through the UI route).  A link belongs to the row whose
    document ID it contains ('id'); only when no link names an ID and there are exactly as many links as rows are
    they paired in page order ('position'); otherwise the row has no link ('none') and goes through the UI route."""
    return await page.evaluate(r"""(marker) => {
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
      const rows=[...document.querySelectorAll('table.awInfoTable_table tbody tr, .awInfoTableGroup--body > .awInfoTableRow')];
      const out=[];
      for (const row of rows) {
        const cells=[...row.querySelectorAll(':scope > td, :scope > .awInfoTableCell')];
        if (!cells.length) continue;
        const vals=cells.map(c=>clean((c.querySelector('.awInfoTableCell_content')||c).innerText));
        const rowText=clean(row.innerText);
        const documentName=(vals.find(x=>/\.[a-z0-9]{2,8}$/i.test(x))||rowText.match(/([^\s]+\.[a-z0-9]{2,8})\b/i)?.[1]||'');
        const documentId=(vals.find(x=>/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(x))||rowText.match(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i)?.[0]||'');
        const action=[...row.querySelectorAll('button,a,[role=button],span.awIcon,.awInteractiveElement')].find(e=>{
          const d=clean([e.innerText,e.textContent,e.title,e.getAttribute('aria-label'),e.getAttribute('data-appway-test-id'),e.outerHTML].join(' '));
          return /\bview\b|viewdetails|document/i.test(d);
        }) || cells.at(-1)?.querySelector('button,a,[role=button],span.awIcon,.awInteractiveElement');
        if (!documentName && !documentId) continue;
        out.push({index:out.length,documentName,documentId,rowText,cells:vals,
          actionId:action?.id||'',actionTestId:action?.getAttribute('data-appway-test-id')||'',
          actionHtml:action?.outerHTML?.slice(0,2000)||'',hasAction:!!action});
      }
      const html=document.documentElement.outerHTML;
      const esc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      const urls=!marker?[]:[...new Set([...html.matchAll(new RegExp('(?:&quot;|")url(?:&quot;|")\\s*:\\s*(?:&quot;|")([^"<]*'+esc(marker)+'[^"<]+)(?:&quot;|")','g'))]
        .map(m=>m[1].replace(/&amp;/g,'&')))];
      const plain=u=>{try{return decodeURIComponent(u).toLowerCase()}catch{return u.toLowerCase()}};
      const owner=x=>x.documentId?urls.filter(u=>plain(u).includes(x.documentId.toLowerCase())):[];
      const byId=out.some(x=>owner(x).length);
      out.forEach((x,i)=>{
        x.viewerUrlsOnPage=urls.length;
        const own=owner(x);
        if(own.length===1){x.viewerUrl=own[0];x.pairing='id'}
        else if(!byId&&urls.length===out.length){x.viewerUrl=urls[i];x.pairing='position'}
        else x.pairing=own.length>1?'several links name this ID':'none';
      });
      return out;
    }""", VIEWER_MARKER)

async def mark_document_view(page, item):
    return await page.evaluate(r"""d=>{
      document.querySelectorAll('[data-s4-document-view]').forEach(e=>e.removeAttribute('data-s4-document-view'));
      const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
      const rows=[...document.querySelectorAll('table.awInfoTable_table tbody tr, .awInfoTableGroup--body > .awInfoTableRow')];
      let row=rows.find(r=>d.documentId && clean(r.innerText).includes(d.documentId));
      if(!row) row=rows.find(r=>d.documentName && clean(r.innerText).includes(d.documentName));
      if(!row) row=rows[d.index];
      if(!row)return false;
      let target=(d.actionId&&document.getElementById(d.actionId)) ||
        (d.actionTestId&&row.querySelector(`[data-appway-test-id="${CSS.escape(d.actionTestId)}"]`)) ||
        [...row.querySelectorAll('button,a,[role=button],span.awIcon,.awInteractiveElement')].find(e=>/viewdetails|\bview\b|document/i.test(clean([e.innerText,e.textContent,e.title,e.getAttribute('aria-label'),e.outerHTML].join(' ')))) ||
        row.querySelector('td:last-child span.awIcon, td:last-child button, td:last-child a, td:last-child [role=button]');
      if(!target)return false;
      target.setAttribute('data-s4-document-view','1');return true;
    }""",item)

async def save_document_forensics(page, folder, label, extra=None):
    """Persist much richer live evidence before a document-resolution failure."""
    folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    data=await page.evaluate(r"""() => ({
      url:location.href,title:document.title,bodyText:document.body?.innerText||'',
      html:document.documentElement.outerHTML,
      rows:[...document.querySelectorAll('tr')].map((r,i)=>({i,text:r.innerText,html:r.outerHTML})),
      elements:[...document.querySelectorAll('button,a,[role=button],span.awIcon,.awInteractiveElement')].map((e,i)=>({i,tag:e.tagName,id:e.id||'',text:e.innerText||e.textContent||'',title:e.title||'',aria:e.getAttribute('aria-label')||'',testId:e.getAttribute('data-appway-test-id')||'',html:e.outerHTML}))
    })""")
    data['extra']=extra or {}
    (folder/f'{safe(label)}_{stamp}.json').write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
    (folder/f'{safe(label)}_{stamp}.html').write_text(data['html'],encoding='utf-8')
    await visual_screenshot(page,path=str(folder/f'{safe(label)}_{stamp}.png'),full_page=True,animations='disabled',caret='hide')

FILE_KINDS=((b'%PDF','pdf','.pdf'),(b'PK\x03\x04','office/zip','.zip'),(b'\x89PNG','png','.png'),(b'\xff\xd8\xff','jpg','.jpg'),
            (b'GIF8','gif','.gif'),(b'II*\x00','tif','.tif'),(b'MM\x00*','tif','.tif'),(b'\xd0\xcf\x11\xe0','office/ole','.doc'),
            (b'{\\rtf','rtf','.rtf'),(b'BM','bmp','.bmp'))
FILE_URL_ATTRS=re.compile(r'<(?:iframe|embed|object|a|img|video|source)\b[^>]*?\b(?:src|data|href)\s*=\s*["\']([^"\']+)["\']',re.I)


def looks_like(body, content_type):
    """What a response body is: 'html' (a viewer page, never saved as the document), a known file kind, or 'other'."""
    head=bytes(body[:512])
    for magic,kind,_ in FILE_KINDS:
        if head.startswith(magic):return kind
    low=head.lstrip().lower()
    if 'text/html' in (content_type or '') or low.startswith(b'<!doctype html') or b'<html' in low:return 'html'
    return 'other'


def file_name(preferred, disposition, kind, url):
    """The saved name: the row's document name; when it has no extension, the server's file name or the content kind."""
    if Path(preferred).suffix:return preferred
    m=re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',disposition or '',re.I)
    suffix=Path(unquote(m.group(1))).suffix if m else ''
    suffix=suffix or Path(urlparse(url).path).suffix or next((x for _,k,x in FILE_KINDS if k==kind),'.bin')
    return preferred+suffix


async def fetch_file(context, url, folder, preferred, tries):
    """GET `url` with the System 1 sign-in.  A real file is saved (path returned); a viewer page or an error is not
    (None).  Every request is recorded in `tries` for the run report: status, type, size, what the body looks like."""
    if not url or url in ('about:blank',''):return None,b''
    url=ORIGIN+url if url.startswith('/') else url
    rec={'address':P.strip_query(url)}
    tries.append(rec)
    try:
        response=await context.request.get(url,timeout=20000)
        body=await response.body()
    except Exception as exc:
        rec['error']=f'{type(exc).__name__}: {exc}'[:300]
        return None,b''
    ctype=response.headers.get('content-type','').lower();disp=response.headers.get('content-disposition','')
    kind=looks_like(body,ctype)
    rec.update(http=response.status,contentType=ctype[:80],disposition=disp[:160],bytes=len(body),looksLike=kind)
    if not response.ok or kind=='html' or not body:
        return None,body
    target=unique(folder,file_name(preferred,disp,kind,url))
    target.write_bytes(body)
    rec['saved']=target.name
    return target,body


def embedded_file_urls(html_text, base):
    """Addresses a viewer page shows its document from (iframe / embed / object / download link), System 1's host only."""
    out=[]
    for raw in FILE_URL_ATTRS.findall(html_text or ''):
        u=urljoin(base,html.unescape(raw))
        if u.startswith(ORIGIN) and u!=base and u not in out and not re.search(r'\.(?:js|css|ico|svg|woff2?)(?:\?|$)',u,re.I):
            out.append(u)
    return out[:6]


async def direct_response_to_file(context, url, folder, preferred):
    """(Audit Trail) the file behind `url`, or None."""
    saved,_=await fetch_file(context,url,folder,preferred,[])
    return saved


async def fast_direct_document(context, item, folder, semaphore):
    """Download one document through its viewer address, with the System 1 sign-in - nothing is clicked.  When that
    address answers with a viewer page instead of the file, the file the page shows (iframe / embed / download link)
    is fetched instead.  Every request is recorded in item['attempts']."""
    async with semaphore:
        tries=item.setdefault('attempts',[])
        viewer=item.get('viewerUrl','')
        if not viewer:
            return None,f"no viewer address for this row (pairing: {item.get('pairing','none')})"
        preferred=item.get('documentName') or f"document_{item.get('index',0)+1}"
        saved,body=await fetch_file(context,viewer,folder,preferred,tries)
        if saved:
            return saved,'direct-viewer-download'
        last=tries[-1] if tries else {}
        if last.get('looksLike')=='html':
            base=ORIGIN+viewer if viewer.startswith('/') else viewer
            for u in embedded_file_urls(body.decode('utf-8','replace'),base):
                saved,_=await fetch_file(context,u,folder,preferred,tries)
                if saved:
                    return saved,'direct-file-inside-viewer-page'
            if CAPTURES is not None:              # the viewer page itself, for the report
                try:
                    CAPTURES.mkdir(parents=True,exist_ok=True)
                    name=safe(f"viewer-page-{item.get('documentId') or item.get('index',0)+1}",max_length=90)+'.html'
                    (CAPTURES/name).write_text(P.strip_queries(body.decode('utf-8','replace'))[:3000000],encoding='utf-8')
                    item['viewerPageCopy']=name
                except Exception:
                    pass
            return None,'the viewer address answered with a page, and no file was found inside it'
        if 'error' in last:
            return None,'direct request failed: '+last['error']
        return None,f"the viewer address answered HTTP {last.get('http')} ({last.get('looksLike')}, {last.get('bytes')} bytes)"


# The pop-up's OWN toolbar button: a real button (never a link inside the document) named exactly Download / Save.
TOOLBAR_DOWNLOAD_JS=r"""()=>{
  const clean=v=>(v??'').toString().replace(/\s+/g,' ').trim();
  const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!=='none'&&s.visibility!=='hidden'};
  document.querySelectorAll('[data-s4-toolbar-download]').forEach(e=>e.removeAttribute('data-s4-toolbar-download'));
  const named=e=>[e.getAttribute('aria-label'),e.title,e.innerText||e.value||e.textContent].map(clean).find(Boolean)||'';
  const all=[...document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')]
    .filter(e=>visible(e)&&!e.closest('a[href]')&&/^(download|save)( (file|document|a copy))?$/i.test(named(e)));
  if(!all.length)return {found:false,controls:[...document.querySelectorAll('button,[role=button],a[href]')].filter(visible).slice(0,40)
    .map(e=>({tag:e.tagName.toLowerCase(),name:named(e).slice(0,80)}))};
  all[0].setAttribute('data-s4-toolbar-download','1');return {found:true,name:named(all[0])};
}"""


async def read_document_popup(context, popup, folder, preferred, tries, downloads):
    """The document window a View click opened.  Strict, in this order, and NOTHING inside the document is clicked:
      1. the file at the window's own address (Edge's PDF viewer shows the PDF's address);
      2. the file the window's page shows (embed / iframe / object / image), read from the page;
      3. a download the window started by itself (Word, Excel ...);
      4. ONE click on the window's own toolbar Download button, if it has one.
    `downloads` collects downloads the window starts.  Returns (path or None, how / why not)."""
    end=time.monotonic()+15
    while popup.url in ('','about:blank') and time.monotonic()<end and not downloads:
        await asyncio.sleep(.1)
    try:
        await popup.wait_for_load_state('domcontentloaded',timeout=10000)
    except Exception:
        pass
    if downloads:
        return await save_download(downloads[0],folder,preferred),'download-started-by-document-window'
    saved,_=await fetch_file(context,popup.url,folder,preferred,tries)
    if saved:
        return saved,'file-at-document-window-address'
    try:
        shown=await popup.evaluate("()=>[...document.querySelectorAll('embed[src],iframe[src],object[data],img[src],video[src],source[src]')].map(e=>e.src||e.data).filter(Boolean)")
    except Exception:
        shown=[]
    shown+=[f.url for f in popup.frames[1:] if f.url]
    for u in dict.fromkeys(x for x in shown if x.startswith('http')):
        saved,_=await fetch_file(context,u,folder,preferred,tries)
        if saved:
            return saved,'file-shown-in-document-window'
    t=time.monotonic()+3
    while not downloads and time.monotonic()<t:
        await asyncio.sleep(.1)
    if downloads:
        return await save_download(downloads[0],folder,preferred),'download-started-by-document-window'
    try:
        button=await popup.evaluate(TOOLBAR_DOWNLOAD_JS)
    except Exception as exc:
        button={'found':False,'error':type(exc).__name__}
    tries.append({'documentWindowToolbar':button})
    if button.get('found'):
        try:
            async with popup.expect_download(timeout=10000) as info:
                await visual_click(popup.locator('[data-s4-toolbar-download="1"]'),timeout=5000)
            return await save_download(await info.value,folder,preferred),'document-window-toolbar-download'
        except Exception as exc:
            tries.append({'toolbarClick':f'{type(exc).__name__}: {exc}'[:300]})
    return None,'the document window opened, but neither its address, the file it shows nor its own Download button gave the file'


async def ui_fallback_document(context, page, item, folder, events):
    """Only when the direct route failed: click this row's View ONCE.  A file that downloads straight away is saved;
    a document window is read by read_document_popup (strict - never clicked around in).  Every window this step
    opened is closed again, whatever happens."""
    tries=item.setdefault('attempts',[])
    current=await document_rows(page)
    match=next((x for x in current if item.get('documentId') and x.get('documentId')==item.get('documentId')),None)
    if not match:
        match=next((x for x in current if item.get('documentName') and x.get('documentName')==item.get('documentName')),None)
    if not match:
        return None,'row not found during UI fallback'
    if not await mark_document_view(page,match):
        return None,'View icon not resolved during UI fallback'
    preferred=match.get('documentName') or f"document_{match.get('index',0)+1}"
    before=set(context.pages)
    popups,downloads=[],[]
    on_popup=lambda p:(popups.append(p),p.on('download',downloads.append))
    page.on('popup',on_popup);page.on('download',downloads.append)
    try:
        await visual_click(page.locator('[data-s4-document-view="1"]'),timeout=4000,force=True)
        end=time.monotonic()+8
        while not popups and not downloads and time.monotonic()<end:
            await asyncio.sleep(.1)
        if downloads and not popups:
            return await save_download(downloads[0],folder,preferred),'ui-view-downloaded-directly'
        if not popups:
            return None,'View was clicked, but neither a download nor a document window appeared within 8 s'
        saved,how=await read_document_popup(context,popups[0],folder,preferred,tries,downloads)
        if not saved:
            item['captures']=await capture_page(popups[0],f"document-window-{item.get('documentId') or item.get('index',0)+1}")
        return saved,how
    except Exception as exc:
        return None,f'UI fallback failed: {type(exc).__name__}: {exc}'
    finally:
        page.remove_listener('popup',on_popup);page.remove_listener('download',downloads.append)
        for p in [*popups,*(p for p in context.pages if p not in before)]:
            try:
                if p is not page and not p.is_closed():await p.close()
            except Exception:pass


async def download_documents(context, page, folder, events):
    """Download 30+ Received Documents rapidly while retaining full verification."""
    await asyncio.sleep(.10)
    discovered=await document_rows(page)
    if not discovered:
        body=await page.locator('body').inner_text()
        if 'Received Documents' in body:
            raise RuntimeError('Received Documents table exists but no document rows were resolved; diagnostics saved')
        return []

    event(events,'document-rows-resolved',count=len(discovered),
          viewerAddressesOnPage=discovered[0].get('viewerUrlsOnPage',0),documents=[
        {'name':x.get('documentName',''),'id':x.get('documentId',''),'pairing':x.get('pairing',''),
         'viewerAddress':P.strip_query(x.get('viewerUrl',''))} for x in discovered])

    semaphore=asyncio.Semaphore(8)
    results=list(await asyncio.gather(*[
        fast_direct_document(context,item,folder,semaphore) for item in discovered
    ]))
    direct_failed={i:status for i,(saved,status) in enumerate(results) if not saved}
    fallback_indexes=list(direct_failed)
    if fallback_indexes:
        event(events,'document-ui-fallback-started',count=len(fallback_indexes))
        for index in fallback_indexes:
            item=discovered[index]
            saved,status=await ui_fallback_document(context,page,item,folder,events)
            results[index]=(saved,status)

    rows=[]
    for index,(item,(saved,status)) in enumerate(zip(discovered,results)):
        event(events,'document-result',file=saved.name if saved else '',name=item.get('documentName',''),
              documentId=item.get('documentId',''),method=status if saved else '',
              directRoute=direct_failed.get(index,'ok'),failure='' if saved else status,
              attempts=item.get('attempts',[]),captures=item.get('captures',[]),viewerPageCopy=item.get('viewerPageCopy',''))
        rows.append({'source_text':'View','row_text':item.get('rowText',''),
                     'saved_file':saved.name if saved else '','status':status,'name':item.get('documentName','')})
    failures=[row for row in rows if not row['saved_file']]
    if failures:
        raise RuntimeError(f"Downloaded {len(rows)-len(failures)} of {len(rows)} Received Documents; not downloaded: "
                           +"; ".join(f"{r['name'] or '(no name)'} ({r['status']})" for r in failures))
    event(events,'all-received-documents-downloaded',count=len(rows),parallelism=8,
          methods=dict(collections.Counter(r['status'] for r in rows)))
    return rows

async def click_resolved(context, page, item, folder, attr, preferred):
    """Resolve and download a general System 1 control, currently used for Audit Trail."""
    marked = await page.evaluate("""([d,a])=>{
      document.querySelectorAll('['+a+']').forEach(e=>e.removeAttribute(a));
      const all=[...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')];
      let target=(d.id&&document.getElementById(d.id))
        ||(d.testId&&all.find(e=>e.getAttribute('data-appway-test-id')===d.testId))
        ||all.find(e=>{
          const text=(e.innerText||e.value||e.textContent||'').trim();
          return d.text&&text===d.text;
        })
        ||all[d.index];
      if(!target)return false;
      target.setAttribute(a,'1');
      return true;
    }""", [item,attr])
    if not marked:
        return None, "not resolved"

    existing=set(context.pages)
    try:
        async with page.expect_download(timeout=10000) as info:
            await visual_click(page.locator(f'[{attr}="1"]'),timeout=5000)
        return await save_download(await info.value,folder,preferred), "downloaded"
    except Exception:
        await asyncio.sleep(0.3)

    popups=[p for p in context.pages if p not in existing and not p.is_closed()]
    for popup in popups:
        try:
            await popup.wait_for_load_state("commit",timeout=5000)
            saved=await direct_response_to_file(context,popup.url,folder,preferred)
            if saved:
                await popup.close()
                return saved, "downloaded-from-popup"
        except Exception:
            pass
    return None, "no download emitted"


async def download_audit(context,page,folder,events):
    item=await page.evaluate(r"""()=>{const c=v=>(v??'').toString().replace(/\s+/g,' ').trim(),all=[...document.querySelectorAll('button,a,[role=button]')],e=all.find(x=>/audit\s*(trail|trial)/i.test([c(x.innerText||x.textContent),x.id,x.title,x.getAttribute('data-appway-test-id'),x.outerHTML].join(' ')));if(!e)return null;const html=document.documentElement.outerHTML,patterns=[/url(?:&quot;|\")?\s*[:=]\s*(?:&quot;|\")([^"<]+Audit[^"<]+)(?:&quot;|\")/i,/([^"'<\s]+(?:AuditTrail|Audit%20Trail|AuditHistory)[^"'<\s]*)/i];let url='';for(const p of patterns){const m=html.match(p);if(m){url=m[1].replace(/&amp;/g,'&');break}}return{index:all.indexOf(e),text:c(e.innerText||e.textContent),id:e.id||'',testId:e.getAttribute('data-appway-test-id')||'',url,html:e.outerHTML}}""")
    if not item:raise RuntimeError('Audit Trail control not found')
    if item.get('url'):
        url=item['url'];url=ORIGIN+url if url.startswith('/') else url
        direct=await direct_response_to_file(context,url,folder,'Audit Trail.pdf')
        if direct:event(events,'audit-trail-downloaded',file=direct.name,method='direct');return direct,'direct-audit-download'
    saved,status=await click_resolved(context,page,item,folder,'data-s4audit','Audit Trail.pdf')
    if not saved:raise RuntimeError('Audit Trail click produced no file: '+status)
    event(events,'audit-trail-downloaded',file=saved.name,method='ui-fallback');return saved,status

def make_labeled_page(image_path, title, page_number, width=1600):
    from PIL import Image, ImageDraw, ImageFont   # only here: the batch never needs Pillow
    image = Image.open(image_path).convert("RGB")
    ratio = width / image.width
    image = image.resize((width, max(1,int(image.height*ratio))))
    header = 110
    canvas = Image.new("RGB", (width, image.height+header), "white")
    canvas.paste(image,(0,header)); draw=ImageDraw.Draw(canvas); font=ImageFont.load_default()
    draw.text((35,25),title,fill="black",font=font,stroke_width=1)
    draw.text((width-140,25),f"Page {page_number}",fill="black",font=font)
    return canvas


def details_pdf(states, output, meta, iid, sub):
    """Pillow-only PDF. No ReportLab dependency."""
    from PIL import Image, ImageDraw, ImageFont   # only here: the batch never needs Pillow
    width,height = 1600,1000
    nav = Image.new("RGB",(width,height),"white"); draw=ImageDraw.Draw(nav); font=ImageFont.load_default()
    draw.text((50,40),"INSTRUCTION DETAILS",fill="black",font=font,stroke_width=1)
    draw.text((50,75),f"{meta['customerNumber']} - {meta['customerName']}",fill="black",font=font)
    draw.text((50,100),f"Account: {meta['accountNumber']}",fill="black",font=font)
    draw.text((50,125),f"Instruction: {iid} - {sub}",fill="black",font=font)
    draw.text((50,185),"NAVIGATION TABLE",fill="black",font=font,stroke_width=1)
    y=230
    for index,state in enumerate(states,2):
        draw.text((70,y),state["stage"],fill="black",font=font,stroke_width=1)
        draw.text((600,y),str(index),fill="black",font=font); y+=45
    pages=[nav]
    for index,state in enumerate(states,2): pages.append(make_labeled_page(Path(state["screenshot"]),state["stage"],index,width))
    output.parent.mkdir(parents=True,exist_ok=True)
    pages[0].save(output,save_all=True,append_images=pages[1:],resolution=120.0)
    for page in pages: page.close()


def knowledge(out,title,iid,states,events,rows,audit_file,pdf,meta,sub,failure=None):
    from PIL import Image, ImageDraw, ImageFont   # only here: the batch never needs Pillow
    out.mkdir(parents=True,exist_ok=True); structures=[]; interactive=[]; sources=[]
    for n,original in enumerate(states,1):
        state=dict(original); controls=state.pop("controls"); source=state.pop("html"); state.pop("screenshot",None)
        structures.append({"sequence":n,**state}); interactive.append({"sequence":n,"stage":state["stage"],"controls":controls}); sources.append(f'<!-- {n} {html.escape(state["stage"])} -->\n<template>{html.escape(source)}</template>')
    status="FAILED" if failure else "SUCCESS"
    payload={"scriptTitle":TITLE,"status":status,"failure":failure,"instructionId":iid,"identity":meta,"subWorkType":sub,"documents":rows,"auditTrail":audit_file.name if audit_file else None,"events":events,"states":structures}
    (out/"website_structure.json").write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8")
    (out/"interactive_elements.json").write_text(json.dumps({"states":interactive},indent=2,ensure_ascii=False),encoding="utf-8")
    (out/"page_source.html").write_text("<!doctype html><meta charset='utf-8'>\n"+"\n".join(sources),encoding="utf-8")
    (out/"website_knowledge.md").write_text(f'# {title}\n\n- Status: **{status}**\n- Instruction ID: `{iid}`\n- Client: `{meta.get("customerNumber","")} - {meta.get("customerName","")}`\n- Account: `{meta.get("accountNumber","")}`\n- Sub WorkType: `{sub}`\n- Files downloaded: **{sum(bool(x.get("saved_file")) for x in rows)}**\n- Failure: `{failure or "None"}`\n\n```json\n{json.dumps(events,indent=2,ensure_ascii=False)}\n```',encoding="utf-8")
    if pdf and Path(pdf).exists(): shutil.copy2(pdf,out/"visual_capture.pdf")
    else:
        image=Image.new("RGB",(900,220),"white"); ImageDraw.Draw(image).text((25,25),failure or "No visual capture",fill="black",font=ImageFont.load_default()); image.save(out/"visual_capture.pdf"); image.close()


async def ensure_clean_inbox_search(page,timeout=60):
    """Wait for the real My Inbox search box, then clear and verify it."""
    deadline=time.monotonic()+timeout;last=''
    while time.monotonic()<deadline:
        try:
            marked=await page.evaluate("""()=>{
              const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return !!(r.width||r.height)&&s.display!='none'&&s.visibility!='hidden'};
              const inputs=[...document.querySelectorAll('input.awTextInput_input:not([readonly]),input[type=search]')].filter(visible)
                .map(e=>({e,r:e.getBoundingClientRect()})).sort((a,b)=>(b.r.x+b.r.width)-(a.r.x+a.r.width));
              document.querySelectorAll('[data-bulk-search]').forEach(e=>e.removeAttribute('data-bulk-search'));
              if(!inputs.length)return {ok:false,count:0};
              inputs[0].e.setAttribute('data-bulk-search','1');return {ok:true,count:inputs.length,value:inputs[0].e.value||''};
            }""")
            if marked.get('ok'):
                field=page.locator('[data-bulk-search="1"]')
                await field.fill('')
                await field.press('Home')
                value=await field.input_value()
                if value=='':return page
                last=f'search value remained {value!r}'
            else:last='no visible search input yet'
        except Exception as exc:last=f'{type(exc).__name__}: {exc}'
        await asyncio.sleep(.10)
    raise TimeoutError('My Inbox search field was not ready to clear within 60 seconds: '+last)

class StopRun(Exception):
    """The run is stopping (shared stop signal): this case stays in .work, nothing more is downloaded."""


async def process_instruction_once(context,page,case,batch_index,batch_total,attempt,control=None):
    iid=case['iid']
    events=[];rows=[];meta={};sub=P.UNKNOWN_SUB;download_path=None;failure=None;captures=[];trace='';program_error=False
    started=time.monotonic()
    print(f'[{batch_index}/{batch_total}] {iid} - attempt {attempt}/2',flush=True)
    try:
        page=await wait_inbox(context,page)
        page,sub,initial_section=await search_open(context,page,iid,events,
            check_sub=lambda s:guidance_problem(s,case.get('sub_worktype_csv','')))

        # Never trust identity text from Documents, History, Notes, or a stale prior case.
        # Force Overview and verify that its displayed Instruction ID is the exact ID requested
        # before using Client Account Number in the folder name.
        page,meta=await verified_overview_identity(context,page,iid,events)
        if P.norm_account(meta['accountNumber'])!=P.norm_account(case['account']):
            raise CaseHeld('System 1 shows a different Client Account Number than the CSV')

        # Required document-only structure (pipeline: in .work until the case is complete):
        # CASES_ROOT\.work\{instruction_id} - {Account number}\<documents>
        download_path=WORK_ROOT/safe(P.folder_name(iid,meta['accountNumber']),max_length=140)
        if download_path.parent!=WORK_ROOT:
            raise RuntimeError('case folder name left the work folder')
        if download_path.exists():                 # a leftover of an interrupted run: start this case clean
            shutil.rmtree(download_path)
        download_path.mkdir(parents=True,exist_ok=True)

        # Download every Received Document, then download the Audit Trail file
        # from History into the same instruction/account folder.
        if initial_section!='Documents' or await detect_open_section(page)!='Documents':
            page=await section(context,page,'Documents',events)
        rows=await download_documents(context,page,download_path,events)
        if control is not None and control.stopped():
            raise StopRun('run stopping')

        page=await section(context,page,'History',events)
        audit_file,audit_status=await download_audit(
            context,page,download_path,events
        )
        rows.append({
            'source_text':'Audit Trail',
            'row_text':'History - Audit Trail',
            'saved_file':audit_file.name,
            'status':audit_status,
        })

        page=await return_to_my_inbox(context,page,events)
        status='SUCCESS'
    except Exception as exc:
        failure=str(exc) if isinstance(exc,CaseHeld) else f'{type(exc).__name__}: {exc}'
        status='HELD' if isinstance(exc,CaseHeld) else 'STOPPED' if isinstance(exc,StopRun) else 'FAILED'
        if status=='FAILED':                       # file:line of every frame, for the report
            trace=''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))[-6000:]
            program_error=isinstance(exc,PROGRAM_ERRORS)
            if program_error:
                failure='PROGRAM ERROR (not retried) - '+failure
        event(events,'run-failed',error=failure)
        if status!='STOPPED':                      # what the System 1 tab showed at that moment, for the report
            try:
                captures=await capture_page(await live(context,page),f'{iid}-attempt{attempt}-{status.lower()}')
            except Exception:
                pass
        # Recover to My Inbox so one bad ID does not stop the remaining CSV rows.
        try:
            page=await return_to_my_inbox(context,page,events)
        except Exception:
            try:
                page=await open_or_recover_inbox(context,page)
            except Exception:
                pass

    result={
        'instruction_id':iid,'status':status,'attempt':attempt,
        'account_number':meta.get('accountNumber',''),
        'sub_worktype':sub,
        'folder':download_path.name if download_path else '',
        'documents':[x['saved_file'] for x in rows if x.get('source_text')!='Audit Trail'],
        'audit':next((x['saved_file'] for x in rows if x.get('source_text')=='Audit Trail'),''),
        'files_downloaded':sum(bool(x.get('saved_file')) for x in rows),
        'download_path':str(download_path or ''),
        'elapsed_seconds':round(time.monotonic()-started,2),'failure':failure or '',
        'events':events,'captures':captures,'trace':trace,'program_error':program_error,
    }
    print(f'[{batch_index}/{batch_total}] attempt {attempt}/2 {status}: {iid}',flush=True)
    if status=='SUCCESS':
        print(f"Downloaded {result['files_downloaded']} document(s) to: {download_path}",flush=True)
    else:
        print(f"Failure: {result['failure']}",flush=True)
    return page,result

async def process_instruction(context,page,case,batch_index,batch_total,control=None):
    """Run each instruction once, retrying exactly once only after failure (never after a hold, a stop or a program
    error).  result['tries'] keeps every attempt (its events, captures and error trace) for the report."""
    tries=[]
    for attempt in (1,2):
        page,result=await process_instruction_once(
            context,page,case,batch_index,batch_total,attempt,control
        )
        tries.append({k:result.get(k) for k in ('attempt','status','failure','elapsed_seconds','events','captures','trace')})
        result['tries']=tries
        if result['status'] in ('SUCCESS','HELD','STOPPED') or result.get('program_error'):
            return page,result
        if control is not None and control.stopped():
            result['status']='STOPPED'
            return page,result
        if attempt==1:
            print(f"[{batch_index}/{batch_total}] retrying once: {case['iid']}",flush=True)
            try:
                page=await open_or_recover_inbox(context,page)
                page=await ensure_clean_inbox_search(page)
            except Exception:
                page=await open_or_recover_inbox(context)
    return page,result


# ---------------------------------------------------------------------------------------------- pipeline

def choose_sub_worktype(from_system1,from_csv):
    """System 1's Sub WorkType wins over the CSV's (agreed rule); a difference is reported, never silent."""
    if not from_system1 or from_system1==P.UNKNOWN_SUB:
        return from_csv,['System 1 showed no Sub WorkType - the CSV value is used']
    if P.norm_key(from_system1)!=P.norm_key(from_csv):
        return from_system1,[f"Sub Worktype differs: CSV '{from_csv}', System 1 '{from_system1}' - System 1's is used"]
    return from_system1,[]


async def check_system1(context):
    """The sign-in check: exactly one System 1 tab, showing My Inbox.  (ok, problem in plain words)"""
    tabs=[p for p in context.pages if on_system1(p) and not p.is_closed()]
    if not tabs:
        return False,'no System 1 tab is open in the systems Edge window - open System 1 there and sign in'
    if len(tabs)>1:
        return False,f'{len(tabs)} System 1 tabs are open - close all but one'
    try:
        text=await tabs[0].locator('body').inner_text(timeout=3000)
    except Exception:
        text=''
    if 'My Inbox' not in text:
        return False,'the System 1 tab does not show My Inbox yet - finish signing in there'
    return True,''


def reusable_download(paths,iid):
    """A case fully downloaded by an earlier run that stopped before the hand-over: kept, not downloaded again."""
    m=P.read_json(paths.downloads/f'{iid}.json')
    if m and m.get('folder') and (paths.work/m['folder']).is_dir():
        return m
    return None


async def wait_for_go(context,control):
    """Answer every sign-in check until you have pressed Enter and every part said yes (go), or the run stops."""
    answered=0
    while not control.is_go():
        if control.stopped():
            return False
        seq=control.check_seq()
        if seq>answered:
            ok,problem=await check_system1(context)
            control.answer_check('part1',seq,ok,problem)
            answered=seq
        await asyncio.sleep(0.3)
    return True


def write_diagnostics(paths,case,result):
    """What this part saw for one case (every attempt, every document request), for the coordinator's report."""
    try:
        P.write_json(paths.diagnostics('part1')/f"{case['iid']}.json",{
            'iid':case['iid'],'csv_sub_worktype':case.get('sub_worktype_csv',''),'csv_account':case.get('account',''),
            **{k:result.get(k) for k in ('status','failure','sub_worktype','account_number','folder','documents','audit',
                                         'files_downloaded','elapsed_seconds')},
            'tries':result.get('tries') or [],'written':P.now()})
    except Exception:
        pass                                       # diagnostics never decide anything


async def run_cases(context,page,todo,paths,control,say,counts):
    """Every case of the run in CSV order; counts['done'/'held'] stay right even if this raises."""
    bugs=collections.Counter()             # program error text -> cases it failed
    for index,case in enumerate(todo,1):
        if control.stopped():
            say(f'stopped before {len(todo)-index+1} case(s) were downloaded')
            break
        iid=case['iid']
        if reusable_download(paths,iid):
            say(f'{iid} already downloaded by an earlier run - kept',iid=iid)
            counts['done']+=1
            continue
        (paths.downloads/f'{iid}.json').unlink(missing_ok=True)
        say(f'{iid} downloading ({index}/{len(todo)})','detail',iid)
        page,result=await process_instruction(context,page,case,index,len(todo),control)
        write_diagnostics(paths,case,result)
        if result['status']=='SUCCESS':
            sub,warnings=choose_sub_worktype(result['sub_worktype'],case.get('sub_worktype_csv',''))
            P.write_json(paths.downloads/f'{iid}.json',{
                'iid':iid,'account':P.norm_account(result['account_number']),'folder':result['folder'],
                'sub_worktype':sub,'sub_worktype_system1':result['sub_worktype'],
                'sub_worktype_csv':case.get('sub_worktype_csv',''),'warnings':warnings,
                'documents':result['documents'],'audit':result['audit'],'finished':P.now()})
            counts['done']+=1
            say(f"{iid} downloaded: {len(result['documents'])} document(s) + Audit Trail "
                f"in {result['elapsed_seconds']:.0f}s ({index}/{len(todo)})",iid=iid)
            for w in warnings:
                say(f'{iid} WARNING: {w}',iid=iid)
        elif result['status']=='STOPPED':
            say(f'{iid} left unfinished in .work (run stopping)',iid=iid)
            break
        else:
            counts['held']+=1
            control.hold(iid,'System 1: '+result['failure'],'part1')
            say(f"{iid} NOT downloaded: {result['failure']}",iid=iid)
            if result.get('program_error'):
                bugs[result['failure']]+=1
                if bugs[result['failure']]>=2:  # every later case would fail the same way
                    rest=todo[index:]
                    for c in rest:
                        control.hold(c['iid'],'System 1: not reached - Part 1 stopped on a program error: '+result['failure'],'part1')
                    counts['held']+=len(rest)
                    say(f"PART 1 STOPPED: the same program error on 2 cases - {len(rest)} case(s) not reached. "
                        f"{result['failure']} (full trace in the pipeline report)")
                    break


async def run_pipeline(run_dir):
    global CAPTURES
    import copilot_case_sender as sender            # Part 3's storage rules and fast Playwright start, reused as-is
    cfg=sender.load_config()
    policy,problems=sender.validate_config(cfg)
    if problems:
        raise RuntimeError('configuration problems: '+'; '.join(problems))
    sender.bootstrap_storage(cfg,policy)            # TEMP, caches, write guard: nothing on the system drive
    configure(cfg)
    paths=P.Paths(cfg.runtime_root,cfg.cases_root,run_dir)
    CAPTURES=paths.captures
    control=P.Control(paths)
    todo=[c for c in P.read_plan(paths)['cases'] if c.get('needs')=='download']
    say=lambda text,level='minimal',iid='': control.event('part1',text,level,iid)
    log=lambda phase,alias='',**f: print(phase,alias,f.get('note',''),flush=True)
    t0=time.monotonic()
    pw,where,secs=await sender.start_driver(sender.load_playwright()[0],log,bool(cfg.node_executable),refresh=False)
    say(f'Playwright ready in {secs:.1f}s ({where})','detail')
    counts={'done':0,'held':0}
    try:
        browser=None
        while browser is None:                     # the coordinator has just opened the systems Edge
            try:
                browser=await connect_systems_edge(pw,sender,int(cfg.systems_cdp_port))
            except Exception as exc:
                if control.stopped() or time.monotonic()-t0>120:
                    raise RuntimeError(f'could not join the systems Edge: {type(exc).__name__}: {exc}') from None
                await asyncio.sleep(1)
        context=browser.contexts[0]
        say('connected to the systems Edge - waiting for your sign-in','detail')
        cursor=await VisualCursor.start(context,CursorConfig(
            enabled=os.environ.get('STUDIO_VISUAL_CURSOR','1')!='0'
        ))
        try:
            if not await wait_for_go(context,control):
                return 0
            mine=[p for p in context.pages if on_system1(p)]   # the one System 1 tab the sign-in check confirmed
            page=await open_or_recover_inbox(context,mine[0] if mine else None)
            page.set_default_timeout(4000)
            await run_cases(context,page,todo,paths,control,say,counts)
        finally:
            await cursor.close()
    finally:
        await pw.stop()                            # disconnects only: the systems Edge stays open as it is
        control.finished('part1',downloaded=counts['done'],held=counts['held'])
    say(f"finished: {counts['done']} case(s) downloaded, {counts['held']} not downloaded")
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=TITLE+' (started by orchestrator/run_pipeline.py)')
    p.add_argument('--run-dir',required=True)
    args=p.parse_args(argv)
    print(TITLE,flush=True)
    try:
        return asyncio.run(run_pipeline(args.run_dir))
    except Exception as exc:
        traceback.print_exc()
        try:
            import copilot_case_sender as sender
            cfg=sender.load_config()
            control=P.Control(P.Paths(cfg.runtime_root,cfg.cases_root,args.run_dir))
            control.event('part1',f'Part 1 stopped: {type(exc).__name__}: {exc}')
            if not control.is_finished('part1'):
                control.finished('part1',error=f'{type(exc).__name__}: {exc}')
        except Exception:
            pass
        return 1


if __name__=='__main__':
    raise SystemExit(main())
