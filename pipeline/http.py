"""Polite async fetcher for websites and directories.

- robots.txt checked per host (urllib.robotparser), User-Agent HeidiCaseResearch/0.1
- max 1 request/second per host, max 20 hosts in parallel
- hosts that answer 403/429 are marked blocked and skipped from then on
- responses cached on disk by URL hash (data/cache/http)
Not used for API endpoints (Google, Overpass, Tavily, OpenRouter).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import CACHE, USER_AGENT

HTTP_CACHE = CACHE / "http"


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    from_cache: bool = False


@dataclass
class Skip:
    url: str
    reason: str  # disallowed_by_robots, blocked_403, blocked_429, robots_unavailable, error:...


def decode(content: bytes, declared: str | None) -> str:
    """UTF-8 first; some sites send Latin-1 bytes despite declaring UTF-8."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if declared and declared.lower().replace("-", "") not in ("utf8",):
        try:
            return content.decode(declared)
        except (LookupError, UnicodeDecodeError):
            pass
    return content.decode("cp1252", errors="replace")


def _cache_key(method: str, url: str, data: dict | None) -> str:
    raw = method + " " + url + " " + json.dumps(data or {}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


@dataclass
class _Host:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0
    robots: RobotFileParser | None = None
    robots_loaded: bool = False
    blocked: str | None = None


class Fetcher:
    def __init__(self, max_hosts: int = 20, timeout: float = 15.0, min_interval: float = 1.0) -> None:
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.sem = asyncio.Semaphore(max_hosts)
        self.min_interval = min_interval
        self.hosts: dict[str, _Host] = {}
        self.skips: list[Skip] = []
        self.requests = 0
        HTTP_CACHE.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    def _host(self, host: str) -> _Host:
        return self.hosts.setdefault(host, _Host())

    async def _wait_turn(self, h: _Host) -> None:
        delay = h.last + self.min_interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        h.last = time.monotonic()

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=10),
        reraise=True,
    )
    async def _send(self, method: str, url: str, data: dict | None) -> httpx.Response:
        self.requests += 1
        return await self.client.request(method, url, data=data)

    async def _load_robots(self, scheme: str, host: str, h: _Host) -> None:
        h.robots_loaded = True
        rp = RobotFileParser()
        try:
            await self._wait_turn(h)
            r = await self._send("GET", f"{scheme}://{host}/robots.txt", None)
        except httpx.HTTPError:
            h.robots = None  # unreachable: allow, the page request will fail on its own
            return
        if r.status_code in (401, 403):
            rp.disallow_all = True
        elif r.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(decode(r.content, r.encoding).splitlines())
        h.robots = rp

    def allowed(self, url: str) -> bool:
        h = self._host(httpx.URL(url).host)
        return h.robots is None or h.robots.can_fetch(USER_AGENT, url)

    async def fetch(
        self, url: str, method: str = "GET", data: dict | None = None, use_cache: bool = True
    ) -> Response | Skip:
        key = _cache_key(method, url, data)
        path = HTTP_CACHE / f"{key}.json"
        if use_cache and path.exists():
            return Response(**json.loads(path.read_text(encoding="utf-8")) | {"from_cache": True})

        u = httpx.URL(url)
        h = self._host(u.host)
        async with self.sem, h.lock:
            if h.blocked:
                return self._skip(url, h.blocked)
            if not h.robots_loaded:
                await self._load_robots(u.scheme, u.host, h)
            if not self.allowed(url):
                return self._skip(url, "disallowed_by_robots")
            await self._wait_turn(h)
            try:
                r = await self._send(method, url, data)
            except httpx.HTTPError as e:
                return self._skip(url, f"error:{type(e).__name__}")
            if r.status_code in (403, 429):
                h.blocked = f"blocked_{r.status_code}"
                return self._skip(url, h.blocked)

        resp = Response(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            content_type=r.headers.get("content-type", ""),
            text=decode(r.content, r.encoding),
        )
        if r.status_code < 500:
            path.write_text(json.dumps(asdict(resp)), encoding="utf-8")
        return resp

    def _skip(self, url: str, reason: str) -> Skip:
        s = Skip(url, reason)
        self.skips.append(s)
        return s
