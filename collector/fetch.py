"""HTTP 조회와 (설치된 경우) 헤드리스 브라우저 렌더링."""
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import requests

from .ref import KST


def now_kst():
    return datetime.now(KST).replace(microsecond=0).isoformat()


@dataclass
class Fetched:
    requested: str
    final_url: str
    status: int | None
    content_type: str
    content: bytes
    error: str | None
    at: str
    agent: str = "script"
    history: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 300 and not self.error

    @property
    def is_pdf(self):
        return "pdf" in self.content_type.lower() or self.content[:5] == b"%PDF-"

    def failure_code(self):
        """조회 실패를 설계서 4.4 코드로 분류. 성공이면 None."""
        if self.ok:
            return None
        if self.status in (401, 403, 429):
            return "ACCESS_BLOCKED"
        if self.status in (404, 410):
            return "BODY_MISSING"
        return "FETCH_FAILED"


class Fetcher:
    def __init__(self, cfg):
        c = cfg["collection"]
        self.timeout = c["fetch_timeout_sec"]
        self.retries = c["fetch_retries"]
        self.backoff = c["retry_backoff_sec"]
        self.delay = c["per_host_delay_sec"]
        self.max_bytes = c["max_bytes"]
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": c["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": c.get("accept_language", "ko,en;q=0.9,ja;q=0.8"),
        })
        self.browser_enabled = bool(c.get("browser_fallback", True))
        self._last = {}
        self._browser = None
        self._pw = None

    def _wait_host(self, url):
        host = urlsplit(url).hostname or ""
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.delay:
                time.sleep(self.delay - gap)
        self._last[host] = time.monotonic()

    def get(self, url):
        """재시도 포함 HTTP GET. 4xx는 재시도하지 않는다."""
        last = None
        for i in range(self.retries):
            self._wait_host(url)
            at = now_kst()
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
                body = b""
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > self.max_bytes:
                        break
                hist = [h.url for h in r.history]
                last = Fetched(url, r.url, r.status_code, r.headers.get("Content-Type", ""), body, None, at, history=hist)
                if r.status_code < 500 and r.status_code != 429:
                    return last
            except requests.RequestException as e:
                last = Fetched(url, url, None, "", b"", f"{type(e).__name__}: {e}"[:300], at)
            if i < self.retries - 1:
                time.sleep(self.backoff * (2 ** i))
        return last

    def browser_available(self):
        if not self.browser_enabled:
            return False
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            return False

    def render(self, url):
        """자바스크립트 렌더링이 필요한 페이지. playwright가 없으면 None."""
        if not self.browser_available():
            return None
        from playwright.sync_api import sync_playwright
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        at = now_kst()
        page = self._browser.new_page()
        try:
            resp = page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
            html = page.content().encode("utf-8")
            status = resp.status if resp else None
            return Fetched(url, page.url, status, "text/html; charset=utf-8", html, None, at, agent="browser")
        except Exception as e:  # playwright 오류 형식이 다양하다
            return Fetched(url, url, None, "", b"", f"{type(e).__name__}: {e}"[:300], at, agent="browser")
        finally:
            page.close()

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
