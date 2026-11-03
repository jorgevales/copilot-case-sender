"""Async Playwright visual cursor. Drop-in replacement for studio_visual_cursor.py.

The pointer stays at its last interaction position. Native mouse movement does
not reset it. Focus/input/programmatic clicks provide background-action feedback.

Pipeline changes (2026-11-03, the pointer stayed in the System 1 AND System 2 tabs after the run):
  - `only` (a page -> bool test) limits the pointer to the pages Part 1 works on; any other page that still carries
    one (an earlier run) has it removed instead of drawn;
  - `close()` removes it from every page with a time limit per frame (a tab that does not answer cannot hold the
    clean-up), and returns what it found: {"removed", "absent", "unreachable"};
  - a page that is left (navigation, bfcache) destroys its own pointer, so going Back never shows a stale one;
  - an install meets a pointer left by another run (other token): the old one is destroyed first;
  - REMOVE / SWEEP are the one clean-up script, also used by the coordinator's end-of-run sweep (no Playwright).
"""
from __future__ import annotations

import asyncio, weakref, uuid
from dataclasses import asdict, dataclass

KEY = '__studio_visual_cursor_v1__'
HOST = 'data-studio-visual-cursor'
_INSTANCES = weakref.WeakKeyDictionary()

@dataclass(frozen=True)
class CursorConfig:
    enabled: bool = True
    size: int = 32
    fill: str = '#7955ed'
    outline: str = '#ffffff'
    outline_width: float = 2
    shadow: str = 'drop-shadow(0 2px 3px #0009)'
    svg: str = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M2 2 L26 18 L15 20 L10 30 Z" fill="var(--fill)" stroke="var(--outline)" stroke-width="var(--outline-width)" stroke-linejoin="round"/></svg>'
    hotspot_x: float = 2
    hotspot_y: float = 2
    movement_ms: int = 110
    press_ms: int = 90
    ripple_ms: int = 380
    ripple_size: int = 52
    ripple_color: str = '#946bff'
    ripple_width: int = 3
    max_ripples: int = 8
    reduced_motion: bool | None = None
    preview_timeout_ms: int = 250
    visuals_in_screenshots: bool = False
    visuals_in_traces: bool = False

STOP_SECONDS = 3.0         # longest wait for one frame to take or drop the pointer

# Removes the pointer of ANY run from one document: its state object (timers, observer, listeners, animations) and
# every host element.  Returns how many it found (0 = none).  deep=true also reaches same-origin iframes.
REMOVE = r"""deep => {
 const key='__studio_visual_cursor_v1__';let found=0;
 const wipe=w=>{try{const s=w[key];if(s){found++;try{s.destroy()}catch(e){}try{delete w[key]}catch(e){}}
   w.document.querySelectorAll('[data-studio-visual-cursor]').forEach(e=>{found++;e.remove();});}catch(e){}};
 wipe(window);
 if(deep)document.querySelectorAll('iframe').forEach(f=>{try{if(f.contentWindow)wipe(f.contentWindow)}catch(e){}});
 return found;
}"""
SWEEP = "(" + REMOVE + ")(true)"   # one Runtime.evaluate expression, for a tab reached over its own DevTools socket

INSTALL = r"""cfg => {
 const key='__studio_visual_cursor_v1__';
 if(window[key]){
   if(window[key].token===cfg.token){window[key].ensure();return;}
   try{window[key].destroy()}catch(e){}         // left by another run: never two pointers, never a dead one
   try{delete window[key]}catch(e){}
 }
 let dead=false,host=null,shadow=null,pointer=null,glyph=null,moveAnimation=null;
 let root=null,observer=null,focusTimer=null,lastDown=null;
 const listeners=[],animations=new Set(),timers=new Map();
 const reduced=()=>cfg.reduced_motion===null?matchMedia('(prefers-reduced-motion: reduce)').matches:cfg.reduced_motion;
 const duration=n=>reduced()?0:Math.max(0,n);
 const state={x:cfg.initial_x,y:cfg.initial_y,lastInput:null,token:cfg.token,ensure,move,previewElement,destroy};
 window[key]=state;
 function listen(target,type,fn){target.addEventListener(type,fn,true);listeners.push([target,type,fn]);}
 function animate(el,frames,ms){
   const a=el.animate(frames,{duration:duration(ms),easing:'ease-out',fill:'none'});
   animations.add(a);a.finished.catch(()=>{}).finally(()=>animations.delete(a));return a;
 }
 function report(){
   if(window===window.top&&!dead){
     // Removable console listener bridges position only; no exposed bindings,
     // persistent init scripts, storage writes, or application input are added.
     console.debug(cfg.report_prefix+JSON.stringify({x:state.x,y:state.y}));
   }
 }
 function ensure(){
   if(dead)return;
   if(root!==document.documentElement){
     root=document.documentElement;
     for(const [t,n,f] of listeners){t.removeEventListener(n,f,true);t.addEventListener(n,f,true);}
   }
   if(window!==window.top||!root)return;
   if(host?.isConnected)return;
   if(!host){
     host=document.createElement('div');host.setAttribute('data-studio-visual-cursor','');
     host.setAttribute('aria-hidden','true');host.setAttribute('inert','');
     host.style.cssText='all:initial;position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;pointer-events:none!important;z-index:2147483647!important;overflow:hidden!important;contain:strict!important;';
     shadow=host.attachShadow({mode:'open'});
     const style=document.createElement('style');
     style.textContent=':host,*{pointer-events:none!important;box-sizing:border-box}.pointer{position:absolute;left:0;top:0;will-change:transform}.glyph{transform-origin:'+cfg.hotspot_x+'px '+cfg.hotspot_y+'px}svg{display:block;width:100%;height:100%}.ripple{position:absolute;border-radius:50%;transform:translate(-50%,-50%)}';
     shadow.append(style);pointer=document.createElement('div');pointer.className='pointer';
     glyph=document.createElement('div');glyph.className='glyph';
     glyph.style.width=cfg.size+'px';glyph.style.height=cfg.size+'px';glyph.style.filter=cfg.shadow;
     glyph.style.setProperty('--fill',cfg.fill);glyph.style.setProperty('--outline',cfg.outline);glyph.style.setProperty('--outline-width',cfg.outline_width);
     const doc=new DOMParser().parseFromString(cfg.svg,'image/svg+xml');
     doc.querySelectorAll('script,foreignObject,style,iframe,object,embed').forEach(e=>e.remove());
     doc.querySelectorAll('*').forEach(e=>[...e.attributes].forEach(a=>{if(/^on/i.test(a.name)||/href$/i.test(a.name))e.removeAttribute(a.name)}));
     if(doc.documentElement.localName==='svg')glyph.append(document.importNode(doc.documentElement,true));
     pointer.append(glyph);shadow.append(pointer);
   }
   root.append(host);place(state.x,state.y);
 }
 function place(x,y){
   if(!Number.isFinite(x)||!Number.isFinite(y))return;
   state.x=x;state.y=y;
   if(pointer)pointer.style.transform=`translate(${x-cfg.hotspot_x}px,${y-cfg.hotspot_y}px)`;
 }
 async function move(x,y){
   if(dead)return;ensure();if(!pointer)return;
   const from=getComputedStyle(pointer).transform;
   moveAnimation?.cancel();place(x,y);report();
   moveAnimation=animate(pointer,[{transform:from},{transform:pointer.style.transform}],cfg.movement_ms);
   await moveAnimation.finished.catch(()=>{});
 }
 function pulse(kind,x,y){
   if(dead)return;ensure();moveAnimation?.cancel();place(x,y);report();
   if(!glyph)return;
   if(kind==='down'){
     glyph.style.transform='scale(.82)';
     animate(glyph,[{transform:'scale(1)'},{transform:'scale(.82)'}],cfg.press_ms);
   }else{
     glyph.style.transform='scale(1)';
     animate(glyph,[{transform:'scale(.82)'},{transform:'scale(1)'}],cfg.press_ms);
     const rings=shadow.querySelectorAll('.ripple');
     if(rings.length>=Math.max(1,cfg.max_ripples))rings[0].remove();
     const ring=document.createElement('div');ring.className='ripple';
     ring.style.cssText=`left:${x}px;top:${y}px;width:${cfg.ripple_size}px;height:${cfg.ripple_size}px;border:${cfg.ripple_width}px solid ${cfg.ripple_color};`;
     shadow.append(ring);
     const a=animate(ring,[{opacity:.9,transform:'translate(-50%,-50%) scale(.15)'},{opacity:0,transform:'translate(-50%,-50%) scale(1)'}],cfg.ripple_ms);
     a.finished.catch(()=>{}).finally(()=>ring.remove());
   }
 }
 function send(data){
   if(dead)return;
   if(window!==window.top){parent.postMessage({studioCursor:cfg.token,...data},'*');return;}
   if(data.kind==='preview')void move(data.x,data.y);
   else{state.lastInput={...data};pulse(data.kind,data.x,data.y);}
 }
 function point(e){
   if(!e||e.nodeType!==1||e.closest?.('[data-studio-visual-cursor]'))return null;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);
   if(!r.width||!r.height||s.visibility==='hidden'||s.display==='none')return null;
   return {x:r.left+r.width/2,y:r.top+r.height/2};
 }
 function background(e){
   const p=point(e);if(p)send({kind:'preview',...p,source:'background-selection'});
 }
 listen(window,'message',event=>{
   const d=event.data;if(!d||d.studioCursor!==cfg.token||!['preview','down','up'].includes(d.kind))return;
   const frame=[...document.querySelectorAll('iframe,frame')].find(f=>f.contentWindow===event.source);
   if(!frame||!Number.isFinite(d.x)||!Number.isFinite(d.y))return;
   const r=frame.getBoundingClientRect(),sx=r.width/(frame.offsetWidth||r.width),sy=r.height/(frame.offsetHeight||r.height);
   send({...d,x:r.left+(frame.clientLeft+d.x)*sx,y:r.top+(frame.clientTop+d.y)*sy});
 });
 // Deliberately do NOT follow mousemove: background Playwright/browser mouse
 // positions (often 0,0) must not pull the pointer away from its last target.
 for(const [event,kind] of [['mousedown','down'],['mouseup','up']]){
   listen(window,event,e=>{
     if(!e.isTrusted)return;
     if(kind==='down'){
       lastDown={target:e.target,time:performance.now()};
       if(focusTimer!==null){clearTimeout(focusTimer);focusTimer=null;}
     }
     send({kind,x:e.clientX,y:e.clientY,source:'native-input'});
   });
 }
 listen(window,'click',e=>{
   if(e.isTrusted)return;
   const p=point(e.target);if(!p)return;
   send({kind:'down',...p,source:'programmatic-input'});
   send({kind:'up',...p,source:'programmatic-input'});
 });
 listen(window,'focusin',e=>{
   if(lastDown&&performance.now()-lastDown.time<250&&lastDown.target===e.target)return;
   if(focusTimer!==null)clearTimeout(focusTimer);
   focusTimer=setTimeout(()=>{focusTimer=null;if(!dead)background(e.target);},0);
 });
 for(const name of ['input','change','select'])listen(window,name,e=>background(e.target));
 function previewElement(e){
   if(dead)return Promise.resolve();background(e);
   return new Promise(resolve=>{const id=setTimeout(()=>{timers.delete(id);resolve();},duration(cfg.movement_ms));timers.set(id,resolve);});
 }
 observer=new MutationObserver(records=>{
   ensure();
   // Existing Studio automation marks resolved targets before background actions.
   // Observe only those known selection markers, not arbitrary DOM changes.
   const selected=records.find(r=>r.type==='attributes'&&r.target.getAttribute(r.attributeName)==='1');
   if(selected)background(selected.target);
 });
 observer.observe(document,{childList:true,subtree:true,attributes:true,attributeFilter:['data-s4view','data-s4-document-view','data-s4-popup-download']});
 listen(window,'resize',()=>ensure());
 listen(window,'blur',()=>{if(glyph)glyph.style.transform='scale(1)';});
 // A page that is left goes into the back/forward cache as it is: destroy first, so Back never shows the pointer.
 listen(window,'pagehide',()=>destroy());
 function destroy(){
   if(dead)return;dead=true;observer.disconnect();
   for(const [t,n,f] of listeners)t.removeEventListener(n,f,true);
   for(const a of animations)a.cancel();animations.clear();
   if(focusTimer!==null)clearTimeout(focusTimer);
   for(const [id,resolve] of timers){clearTimeout(id);resolve();}timers.clear();
   host?.remove();if(window[key]===state)delete window[key];
 }
 ensure();
}"""

class VisualCursor:
    def __init__(self, context, config=None, *, tracing=False, only=None):
        self.context=context
        self._only=only              # page -> bool: the pages the pointer may be drawn on (None = every page)
        self.cleanup={"removed":0,"absent":0,"unreachable":0}
        self.config=config or CursorConfig()
        self._tracing=tracing
        self.enabled=self.config.enabled and (not tracing or self.config.visuals_in_traces)
        self._pages={}
        self._positions=weakref.WeakKeyDictionary()
        self._tasks=set()
        self._started=False
        self._closed=False
        self._token=uuid.uuid4().hex
        self._prefix='__studio_cursor_position_'+self._token+':'
        self._page_handler=lambda page:self._schedule(self._attach(page))

    @classmethod
    async def start(cls, context, config=None, *, tracing=False, only=None):
        current=_INSTANCES.get(context)
        if current and not current._closed:return current
        current=cls(context,config,tracing=tracing,only=only)
        _INSTANCES[context]=current
        if current.enabled:await current._start()
        return current

    def _schedule(self, coroutine):
        if self._closed or not self.enabled:
            coroutine.close();return
        task=asyncio.create_task(coroutine);self._tasks.add(task)
        def done(t):
            self._tasks.discard(t)
            if not t.cancelled():t.exception()
        task.add_done_callback(done)

    async def _start(self):
        if self._started:return
        self._started=True
        self.context.on('page',self._page_handler)
        await asyncio.gather(*(self._attach(p) for p in self.context.pages))

    def _record(self, page, message):
        text=message.text
        if not text.startswith(self._prefix):return
        try:
            import json, math
            point=json.loads(text[len(self._prefix):])
            x,y=float(point['x']),float(point['y'])
            if math.isfinite(x) and math.isfinite(y):self._positions[page]=(x,y)
        except (ValueError,TypeError,KeyError):pass

    async def _install(self, frame):
        if self._closed or not self.enabled:return
        try:
            cfg=asdict(self.config)
            cfg['token']=self._token;cfg['report_prefix']=self._prefix
            cfg['initial_x'],cfg['initial_y']=self._positions.get(frame.page,(24,24))
            await asyncio.wait_for(frame.evaluate(INSTALL,cfg),STOP_SECONDS)   # a silent tab never holds Part 1
        except Exception:pass

    async def _attach(self, page):
        if self._closed or not self.enabled or page.is_closed():return
        if page not in self._pages:
            nav=lambda frame:self._schedule(self._refresh(page))
            dom=lambda:self._schedule(self._refresh(page))
            closed=lambda:self._detach_page(page)
            console=lambda message:self._record(page,message)
            self._pages[page]=(nav,dom,closed,console)
            for name,fn in zip(('framenavigated','domcontentloaded','close','console'),self._pages[page]):page.on(name,fn)
        await self._refresh(page)

    def _allowed(self, page):
        try:
            return self._only is None or bool(self._only(page))
        except Exception:
            return False

    async def _refresh(self, page):
        if not self._allowed(page):          # e.g. the System 2 tab, or a System 1 tab that went elsewhere
            await asyncio.gather(*(self._remove(f) for f in page.frames))
            return
        await asyncio.gather(*(self._install(f) for f in page.frames))

    async def _remove(self, frame):
        """'removed' / 'absent' / 'unreachable' (the frame did not answer within STOP_SECONDS, or failed)."""
        try:
            found=await asyncio.wait_for(frame.evaluate(REMOVE,False),STOP_SECONDS)
            return 'removed' if found else 'absent'
        except Exception:
            return 'unreachable'

    def _detach_page(self, page):
        handlers=self._pages.pop(page,None)
        if handlers:
            for name,fn in zip(('framenavigated','domcontentloaded','close','console'),handlers):page.remove_listener(name,fn)

    async def move_to(self, page, x, y):
        if not self.enabled or self._closed:return
        try:
            self._positions[page]=(x,y)
            await self._attach(page)
            if not self._allowed(page):return
            await page.evaluate('(p)=>window.__studio_visual_cursor_v1__?.move(p.x,p.y)',{'x':x,'y':y})
        except Exception:pass

    async def move_to_locator(self, locator, *, position=None):
        if not self.enabled or self._closed:return
        try:
            deadline=asyncio.get_running_loop().time()+max(.001,self.config.preview_timeout_ms/1000)
            remaining=lambda:max(1,(deadline-asyncio.get_running_loop().time())*1000)
            await self._attach(locator.page)
            if asyncio.get_running_loop().time()>=deadline:return
            await locator.scroll_into_view_if_needed(timeout=remaining())
            if asyncio.get_running_loop().time()>=deadline:return
            box=await locator.bounding_box(timeout=remaining())
            if not box:return
            x=box['x']+box['width']/2;y=box['y']+box['height']/2
            if position is not None:
                metrics=await locator.evaluate('(e)=>({w:e.offsetWidth,h:e.offsetHeight,bx:e.clientLeft,by:e.clientTop})',timeout=remaining())
                x=box['x']+(metrics['bx']+position['x'])*box['width']/(metrics['w'] or box['width'])
                y=box['y']+(metrics['by']+position['y'])*box['height']/(metrics['h'] or box['height'])
            await self.move_to(locator.page,x,y)
        except Exception:pass

    async def click(self, locator, **options):
        if self.enabled and not self._closed and not options.get('trial',False):
            await self.move_to_locator(locator,position=options.get('position'))
        return await locator.click(**options)

    async def screenshot(self, page, **options):
        if self.enabled and not self.config.visuals_in_screenshots:
            options['style']=options.get('style','')+'\n[data-studio-visual-cursor]{visibility:hidden!important}'
        return await page.screenshot(**options)

    async def set_enabled(self, enabled):
        if self._closed:return
        enabled=bool(enabled) and (not self._tracing or self.config.visuals_in_traces)
        if enabled==self.enabled:return
        self.enabled=enabled
        if enabled:await self._start()
        else:await self._stop()

    async def _stop(self):
        if self._started:
            self.context.remove_listener('page',self._page_handler);self._started=False
        for page in list(self._pages):self._detach_page(page)
        tasks=list(self._tasks)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self._tasks.clear()
        try:
            frames=[f for p in self.context.pages if not p.is_closed() for f in p.frames]
        except Exception:
            frames=[]
        outcomes=await asyncio.gather(*(self._remove(f) for f in frames))
        self.cleanup={k:outcomes.count(k) for k in ("removed","absent","unreachable")}
        return self.cleanup

    async def close(self):
        """Remove the pointer from every page (also pages it was never drawn on: an earlier run may have left one).
        Safe to call again: the second call does nothing and returns the first call's counts."""
        if self._closed:return self.cleanup
        self._closed=True;self.enabled=False
        try:
            await self._stop()
        finally:
            self._positions.clear()
            if _INSTANCES.get(self.context) is self:del _INSTANCES[self.context]
        return self.cleanup


def for_page(page):
    return _INSTANCES.get(page.context)

async def visual_click(locator, **options):
    cursor=for_page(locator.page)
    if cursor:return await cursor.click(locator,**options)
    return await locator.click(**options)

async def visual_screenshot(page, **options):
    cursor=for_page(page)
    if cursor:return await cursor.screenshot(page,**options)
    return await page.screenshot(**options)
