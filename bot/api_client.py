from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaStatus:
    wilaya_code: str
    wilaya_name: str
    available: bool
    remaining: int | None = None


def _pick(d: dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "open", "available"):
            return True
        if s in ("false", "no", "0", "closed", "unavailable"):
            return False
    return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def parse_wilaya_quotas(payload: Any) -> list[QuotaStatus]:
    """
    Defensive parser: supports common API shapes.
    Adjust here when you know the exact response format.
    """
    if isinstance(payload, dict):
        items = (
            payload.get("data")
            or payload.get("results")
            or payload.get("items")
            or payload.get("wilayas")
            or payload.get("quotas")
        )
        if items is None:
            # Some APIs return the list at root with additional metadata
            items = payload.get("payload")
    else:
        items = payload

    if not isinstance(items, list):
        logger.debug("Unexpected wilaya-quotas payload shape: %s", type(payload))
        return []

    out: list[QuotaStatus] = []
    for obj in items:
        if not isinstance(obj, dict):
            continue

        code = _pick(obj, ("wilaya_code", "wilayaCode", "code", "id", "wilaya", "wilayaId"))
        # adhahi.dz uses wilayaNameFr/wilayaNameAr
        name = _pick(
            obj,
            (
                "wilaya_name",
                "wilayaName",
                "wilayaNameFr",
                "wilayaNameAr",
                "name",
                "label",
                "title",
            ),
        )

        available_raw = _pick(
            obj,
            (
                "available",
                "isAvailable",
                "open",
                "isOpen",
                "quota_available",
                "quotaAvailable",
                "hasQuota",
            ),
        )
        remaining_raw = _pick(
            obj,
            (
                "remaining",
                "quota_remaining",
                "quotaRemaining",
                "rest",
                "left",
                "quantity",
                "quota",
            ),
        )

        if code is None or name is None:
            logger.debug("Skipping entry missing code/name: keys=%s", list(obj.keys()))
            continue

        available = _to_bool(available_raw)
        if available is None:
            # heuristic: if remaining is numeric and >0, treat as available
            rem_i = _to_int(remaining_raw)
            if rem_i is not None:
                available = rem_i > 0
            else:
                logger.debug("Skipping entry missing availability: code=%r keys=%s", code, list(obj.keys()))
                continue

        remaining = _to_int(remaining_raw)
        out.append(
            QuotaStatus(
                wilaya_code=str(code),
                wilaya_name=str(name),
                available=bool(available),
                remaining=remaining,
            )
        )
    return out


class QuotaApiClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout_s: float = 10.0, proxy_url: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip() if api_key else None
        self._timeout = 30.0  # Increased default timeout
        self._default_proxy = proxy_url

        # Some public endpoints behave differently without a browser-like UA/Referer.
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Referer": "https://adhahi.dz/register",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(
                timeout=self._timeout,
                connect=15.0,  # Increased connect timeout
                read=self._timeout,
                write=10.0,
                pool=10.0
            ),
            follow_redirects=True,
            proxy=self._default_proxy,
        )

    def create_session(self, proxy_url: str | None = None, timeout_s: float | None = None) -> httpx.AsyncClient:
        """Create an isolated HTTP client for user-specific operations."""
        timeout = timeout_s if timeout_s is not None else self._timeout
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
                "Referer": "https://adhahi.dz/register",
            },
            timeout=httpx.Timeout(
                timeout=timeout,
                connect=15.0,  # Increased connect timeout
                read=timeout,
                write=10.0,
                pool=10.0
            ),
            follow_redirects=True,
            proxy=proxy_url if proxy_url is not None else self._default_proxy,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_wilaya_quotas(self, use_proxy: bool = False, proxy_url: str | None = None) -> dict[str, QuotaStatus]:
        """Fetch quotas with automatic retry and proxy rotation if use_proxy is True."""
        import time
        import random
        import string
        from .proxy import get_proxy_url
        
        # Add cache-busting parameter to bypass CDN/proxy caches
        path = f"/api/v1/public/wilaya-quotas?_t={int(time.time() * 1000)}"
        
        delay_s = 0.5
        last_exc: Exception | None = None
        
        for attempt in range(3):
            # If use_proxy is True, generate a fresh session ID for each attempt to rotate the proxy IP.
            # This is effective against "SSL record layer failure" caused by bad proxy nodes.
            current_proxy = proxy_url
            if use_proxy:
                session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
                current_proxy = get_proxy_url(session_id=session_id)
            elif current_proxy is None and self._default_proxy:
                 current_proxy = self._default_proxy

            mode = "via proxy" if current_proxy else "directly"
            if attempt > 0:
                logger.info("Retrying Quota API request (%d/3) %s...", attempt + 1, mode)
            else:
                logger.info("Fetching quotas from adhahi.dz %s...", mode)
            
            try:
                # We always create a fresh session for quotas to ensure no connection reuse if it failed
                async with self.create_session(proxy_url=current_proxy) as client:
                    return await self._fetch_once(client, path)
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code == 429:
                    raise  # Rate limit hit: raise immediately
                
                if attempt == 2: break
                logger.warning("Quota API request failed (HTTP %s, attempt %d/3); retrying in %.1fs", 
                               e.response.status_code, attempt + 1, delay_s)
            except (httpx.ConnectError, httpx.ProxyError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt == 2: break
                logger.warning("Network error (%s: %s, attempt %d/3); retrying in %.1fs", 
                               type(e).__name__, str(e), attempt + 1, delay_s)
            except Exception as e:
                last_exc = e
                if attempt == 2: break
                logger.warning("Unexpected error (%s: %s, attempt %d/3); retrying in %.1fs", 
                               type(e).__name__, str(e), attempt + 1, delay_s)

            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 2, 5.0)

        logger.error("Quota API request failed after 3 attempts. Final error: %s: %s", 
                     type(last_exc).__name__, last_exc)
        return {}

    async def _fetch_once(self, client: httpx.AsyncClient, path: str) -> dict[str, QuotaStatus]:
        """Performs a single GET request and parses the result."""
        resp = await client.get(path)
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"Server error '{resp.status_code}' for url '{resp.request.url}'",
                request=resp.request,
                response=resp,
            )
        resp.raise_for_status()
        logger.info("Quota API request successful (HTTP %d)", resp.status_code)
        payload = resp.json()
        statuses = parse_wilaya_quotas(payload)
        return {s.wilaya_code: s for s in statuses}

