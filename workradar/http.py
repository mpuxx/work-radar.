"""Polite HTTP: per-host concurrency limits, Retry-After, exponential backoff, request accounting."""
import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse

UA = "WorkRadar/1.0 (+job search for UK contractors; respects robots and API terms)"
HOST_LIMIT = 2                      # at most 2 requests in flight per host
_host_sem = defaultdict(lambda: threading.Semaphore(HOST_LIMIT))
_counts = defaultdict(int)
_lock = threading.Lock()


class RateLimited(Exception):
    def __init__(self, retry_after, url):
        super().__init__(f"rate limited by {urlparse(url).hostname}; retry after {retry_after}s")
        self.retry_after = retry_after


class HttpError(Exception):
    def __init__(self, status, url, msg=""):
        super().__init__(f"HTTP {status} from {urlparse(url).hostname} {msg}".strip())
        self.status = status


def take_counts():
    with _lock:
        c = dict(_counts)
        _counts.clear()
    return c


def request(url, data=None, headers=None, timeout=30, retries=2, accept="application/json", method=None, sleep=time.sleep):
    host = urlparse(url).hostname
    h = {"User-Agent": UA, "Accept": accept}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    delay = 2
    for attempt in range(retries + 1):
        with _host_sem[host]:
            with _lock:
                _counts[host] += 1
            try:
                req = urllib.request.Request(url, data=body, headers=h, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code == 503:
                    ra = e.headers.get("Retry-After")
                    wait = int(ra) if ra and ra.isdigit() else delay * 30
                    # Never hammer: surface long waits to the scheduler instead of sleeping on them.
                    if wait > 20 or attempt == retries:
                        raise RateLimited(wait, url)
                    sleep(wait)
                    delay *= 2
                    continue
                if 500 <= e.code < 600 and attempt < retries:
                    sleep(delay)
                    delay *= 2
                    continue
                raise HttpError(e.code, url)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < retries:
                    sleep(delay)
                    delay *= 2
                    continue
                raise HttpError(0, url, str(getattr(e, "reason", e))[:80])


def get_json(url, **kw):
    status, headers, raw = request(url, **kw)
    return json.loads(raw)


def get_text(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", **kw):
    status, headers, raw = request(url, accept=accept, **kw)
    ct = headers.get("Content-Type", "")
    enc = "utf-8"
    if "charset=" in ct:
        enc = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return raw.decode(enc, "replace"), headers
