"""Azure calls of the Azure reviewer: sign-in, Document Intelligence Layout, the language model (stdlib + requests).

Errors are sorted into what the reviewer does next (design agreed 2026-11-09):
  TemporaryError  network trouble, 429 throttling, 5xx      -> retried 3 times (Retry-After honoured), then the case is held
  SafeStop        credentials, quota used up, wrong endpoint/deployment -> the whole run stops safely
  Refused         the model's content filter refused         -> the case is held
  BadRequest      any other rejection                       -> the case is held (the report has Azure's reason)
Nothing here ever prints or logs a key, a token or an endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

DI_API = "2024-11-30"                     # Document Intelligence v4.0 (GA)
DI_MODEL = "prebuilt-layout"
SCOPE = "https://cognitiveservices.azure.com"
BACKOFF = (2, 5, 15)                      # seconds between the 3 retries when Azure gives no Retry-After
MAX_WAIT = 60                             # never wait longer than this for one retry
POLL_LIMIT = 300                          # seconds one Document Intelligence analysis may take


class AzureError(Exception):
    """Base: `str()` is a plain-English reason without addresses or secrets."""


class TemporaryError(AzureError):
    pass


class SafeStop(AzureError):
    pass


class Refused(AzureError):
    pass


class BadRequest(AzureError):
    pass


class BadAnswer(AzureError):
    """The model answered, but not with the JSON asked for (the raw text is kept)."""

    def __init__(self, reason: str, raw: str = ""):
        super().__init__(reason)
        self.raw = raw


class Stopped(Exception):
    """A stop was requested: nothing new is sent to Azure."""


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------- sign-in

class Auth:
    """`key` (the VDI: keys typed in setup) or `signin` (the laptop: the user's own `az login`, no key on disk)."""

    def __init__(self, cfg, run=subprocess.run):
        self.mode = "signin" if str(cfg.azure_auth).lower() in ("signin", "cli") else "key"
        self.keys = {"di": cfg.azure_docintel_key, "openai": cfg.azure_openai_key}
        self.az = (getattr(cfg, "az_cli_path", "") or shutil.which("az") or shutil.which("az.cmd") or "az")
        self.run = run
        self._token, self._expires, self._lock = "", 0.0, threading.Lock()

    def headers(self, service: str) -> dict:
        if self.mode == "key":
            key = self.keys[service]
            return {"Ocp-Apim-Subscription-Key": key} if service == "di" else {"api-key": key}
        return {"Authorization": "Bearer " + self.token()}

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            try:
                done = self.run([self.az, "account", "get-access-token", "--resource", SCOPE, "-o", "json"],
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                raise SafeStop("the Azure sign-in could not be read (Azure CLI 'az' not found or not answering) - "
                               "on the laptop: install the Azure CLI or set az_cli_path in setup, run 'az login', "
                               "then start again") from None
            if done.returncode != 0:
                raise SafeStop("Azure sign-in is missing or expired - run 'az login' in a terminal, then start again")
            try:
                data = json.loads(done.stdout)
                token = data["accessToken"]
            except (ValueError, KeyError, TypeError):
                raise SafeStop("the Azure CLI gave no sign-in token - run 'az login', then start again") from None
            expires = data.get("expires_on")
            try:
                self._expires = float(expires)
            except (TypeError, ValueError):
                self._expires = time.time() + 45 * 60
            self._token = token
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._expires = "", 0.0


# ---------------------------------------------------------------------------------------------- HTTP

def _azure_message(resp) -> tuple:
    """(code, message) of an Azure error body, short and single-line."""
    code, msg = "", ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            msg = str(err.get("message") or "")
            inner = err.get("innererror") or {}
            if isinstance(inner, dict) and inner.get("code"):
                code = f"{code}/{inner['code']}" if code else str(inner["code"])
    except ValueError:
        msg = resp.text or ""
    return code, " ".join(msg.split())[:220]


def classify(status: int, code: str, message: str, what: str) -> AzureError:
    text = f"{code} {message}".lower()
    tail = f" ({code}: {message})" if code or message else ""
    if "content_filter" in text or "responsibleaipolicyviolation" in text or "content management policy" in text:
        return Refused(f"{what}: Azure's content filter refused this case{tail}")
    if "quota" in text and status in (403, 429) and ("out of call volume" in text or "exceeded" in text
                                                     or "monthly" in text or status == 403):
        return SafeStop(f"{what}: the Azure quota is used up{tail} - on the free tier this resets monthly; "
                        "a paid tier needs the subscription owner")
    if status == 401:
        return SafeStop(f"{what}: Azure did not accept the credentials (401) - check the key, or run 'az login'")
    if status == 403:
        return SafeStop(f"{what}: Azure refused access (403){tail} - check the key or the Azure role")
    if status == 404:
        return SafeStop(f"{what}: Azure says not found (404){tail} - check the endpoint and the deployment name in setup")
    if status in (408, 429) or status >= 500:
        return TemporaryError(f"{what}: Azure is busy or unavailable ({status}){tail}")
    return BadRequest(f"{what}: Azure rejected the request ({status}){tail}")


class Http:
    """One requests session per thread; every call retried per the agreed rules."""

    def __init__(self, auth: Auth, tries: int = 3, sleep=time.sleep, stop=None, on_wait=None, session_factory=None):
        import requests                                   # here: on the VDI it lives in the deps folder (added first)
        self.requests = requests
        self.auth, self.tries, self.sleep = auth, tries, sleep
        self.stop = stop or (lambda: False)
        self.on_wait = on_wait or (lambda what, seconds: None)
        self.factory = session_factory or requests.Session
        self.local = threading.local()

    def session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = self.local.s = self.factory()
        return s

    def call(self, method: str, url: str, service: str, what: str, timeout: float = 120, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        signed_again = False
        attempt = 0
        while True:
            if self.stop():
                raise Stopped()
            resp = None
            try:
                resp = self.session().request(method, url, headers={**self.auth.headers(service), **headers},
                                              timeout=timeout, **kw)
            except self.requests.RequestException as exc:
                err = TemporaryError(f"{what}: network problem ({type(exc).__name__})")
            else:
                if resp.status_code < 400:
                    return resp
                code, message = _azure_message(resp)
                err = classify(resp.status_code, code, message, what)
                if resp.status_code == 401 and self.auth.mode == "signin" and not signed_again:
                    signed_again = True                   # a token that expired early: one fresh token, once
                    self.auth.invalidate()
                    continue
            if not isinstance(err, TemporaryError) or attempt >= self.tries:
                if isinstance(err, TemporaryError):
                    raise TemporaryError(f"{err} - still failing after {self.tries} retries")
                raise err
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            if resp is not None:
                try:
                    if resp.headers.get("retry-after-ms"):
                        wait = float(resp.headers["retry-after-ms"]) / 1000
                    elif resp.headers.get("Retry-After"):
                        wait = float(resp.headers["Retry-After"])
                except (TypeError, ValueError):
                    pass
            wait = max(1.0, min(float(wait), MAX_WAIT))
            attempt += 1
            self.on_wait(what, wait)
            self.sleep(wait)


# ---------------------------------------------------------------------------------------------- Document Intelligence

class DocIntel:
    """Layout on one file.  Free tier (F0): Azure reads 2 pages per request, so the file is sent in 2-page pieces
    (pages=1-2, 3-4, ...) until a piece comes back with fewer than 2 pages.  Every piece's result is cached under
    RUNTIME_ROOT by the file's fingerprint, so a file seen before costs no pages."""

    def __init__(self, endpoint: str, http: Http, cache_dir: Path, free: bool = True, max_pages: int = 200):
        self.base = endpoint.rstrip("/")
        self.http, self.cache, self.free, self.max_pages = http, Path(cache_dir), free, max_pages
        self.pages_used, self.cached_pages, self._lock = 0, 0, threading.Lock()

    def info(self) -> dict:
        r = self.http.call("GET", f"{self.base}/documentintelligence/info?api-version={DI_API}", "di",
                           "Document Intelligence check", timeout=30)
        return r.json()

    def analyze(self, data: bytes, paged: bool = True) -> list:
        """Every piece's analyzeResult (page numbers are the file's own).  `paged` False = one request (an image or
        an Office file: no page ranges)."""
        fp = fingerprint(data)
        if not self.free or not paged:
            return [self._piece(fp, data, None)]
        out, start = [], 1
        while start <= self.max_pages:
            res = self._piece(fp, data, f"{start}-{start + 1}")
            if res is None:                               # past the last page
                break
            out.append(res)
            if len(res.get("pages") or []) < 2:
                break
            start += 2
        return out

    def _piece(self, fp: str, data: bytes, pages):
        path = self.cache / f"{fp}-{DI_MODEL}-{DI_API}-{pages or 'all'}.json"
        try:
            cached = json.loads(path.read_text("utf-8"))
            if cached.get("outOfRange"):
                return None
            with self._lock:
                self.cached_pages += len(cached.get("pages") or [])
            return cached
        except (OSError, ValueError):
            pass
        res = self._request(data, pages)
        _write_atomic(path, json.dumps({"outOfRange": True} if res is None else res))
        if res is not None:
            with self._lock:
                self.pages_used += len(res.get("pages") or [])
        return res

    def _request(self, data: bytes, pages):
        url = (f"{self.base}/documentintelligence/documentModels/{DI_MODEL}:analyze?api-version={DI_API}"
               + (f"&pages={pages}" if pages else ""))
        try:
            r = self.http.call("POST", url, "di", "Document Intelligence", data=data,
                               headers={"Content-Type": "application/octet-stream"})
        except BadRequest as exc:
            # past the last page: Azure answers 400 InvalidArgument (2026-11-10, F0: just "Invalid argument.") -
            # only ever asked after the previous piece came back with 2 full pages
            if pages and not pages.startswith("1-") and ("(400)" in str(exc) or _out_of_range(str(exc))):
                return None
            raise
        loc = r.headers.get("Operation-Location") or r.headers.get("operation-location")
        if not loc:
            raise BadRequest("Document Intelligence gave no result address")
        t0 = time.monotonic()
        while True:
            g = self.http.call("GET", loc, "di", "Document Intelligence result", timeout=60)
            body = g.json()
            status = body.get("status")
            if status == "succeeded":
                return body.get("analyzeResult") or {}
            if status == "failed":
                err = body.get("error") or {}
                text = " ".join(str(x) for x in (err.get("code"), err.get("message"),
                                                   (err.get("innererror") or {}).get("message")) if x)
                if pages and not pages.startswith("1-") and _out_of_range(text):
                    return None
                raise BadRequest(f"Document Intelligence could not read the file ({' '.join(text.split())[:200]})")
            if time.monotonic() - t0 > POLL_LIMIT:
                raise TemporaryError("Document Intelligence did not finish within 5 minutes")
            try:
                wait = float(g.headers.get("Retry-After") or 1)
            except (TypeError, ValueError):
                wait = 1.0
            self.http.sleep(max(0.5, min(wait, 5)))


def _out_of_range(text: str) -> bool:
    t = text.lower()
    return "page" in t and any(w in t for w in ("range", "out of", "exceed", "invalid", "not exist", "beyond"))


# ---------------------------------------------------------------------------------------------- language model

class Chat:
    """Azure OpenAI v1 chat completions, strict JSON schema output."""

    def __init__(self, endpoint: str, http: Http):
        base = endpoint.rstrip("/")
        if base.endswith("/chat/completions"):
            self.url = base
        elif "/openai/v1" in base:
            self.url = base + "/chat/completions"
        else:
            self.url = base + "/openai/v1/chat/completions"
        self.http = http
        self.usage, self._lock = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}, threading.Lock()

    def complete(self, model: str, messages: list, name: str, schema: dict, what: str) -> tuple:
        """(parsed object, raw text, model name Azure reports)."""
        body = {"model": model, "messages": messages,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": name, "strict": True, "schema": schema}}}
        r = self.http.call("POST", self.url, "openai", what, json=body, timeout=600)
        data = r.json()
        with self._lock:
            u = data.get("usage") or {}
            self.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
            self.usage["calls"] += 1
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        raw = msg.get("content") or ""
        if choice.get("finish_reason") == "content_filter" or msg.get("refusal"):
            raise Refused(f"{what}: the model refused this case ({' '.join(str(msg.get('refusal') or 'content filter').split())[:160]})")
        if choice.get("finish_reason") == "length":
            raise BadAnswer(f"{what}: the answer was cut off (too long)", raw)
        try:
            obj = json.loads(raw)
        except ValueError:
            raise BadAnswer(f"{what}: the answer is not JSON", raw) from None
        if not isinstance(obj, dict):
            raise BadAnswer(f"{what}: the answer is not a JSON object", raw)
        return obj, raw, data.get("model") or model

    def ping(self, model: str) -> str:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        _obj, _raw, name = self.complete(model, [{"role": "user", "content": 'Reply with {"ok":true}'}], "ping",
                                         schema, f"model check ({model})")
        return name
