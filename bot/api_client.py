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
    def __init__(self, base_url: str, api_key: str | None = None, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip() if api_key else None
        self._timeout = timeout_s

        # Some public endpoints behave differently without a browser-like UA/Referer.
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/149.0",
            "Referer": "https://adhahi.dz/register",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_wilaya_quotas(self) -> dict[str, QuotaStatus]:
        # Adhahi endpoint from provided curl
        path = "/api/v1/public/wilaya-quotas"
        delay_s = 0.5
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(path)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Server error '{resp.status_code}' for url '{resp.request.url}'",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                payload = resp.json()
                statuses = parse_wilaya_quotas(payload)
                return {s.wilaya_code: s for s in statuses}
            except Exception as e:
                last_exc = e
                if attempt == 2:
                    break
                logger.warning("Quota API request failed (attempt %s/3); retrying in %.1fs", attempt + 1, delay_s)
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2, 5.0)

        logger.error("Quota API request failed after retries: %s", last_exc)
        return {}
