"""Unified CAPTCHA solving: 2Captcha service (preferred) with ddddocr fallback.

Usage:
    solver = create_solver()  # reads TWO_CAPTCHA_API_KEY from env
    answer = await solver.solve(image_bytes)  # returns uppercase text
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class CaptchaSolver(ABC):
    """Base class for CAPTCHA solvers."""

    @abstractmethod
    async def solve(self, image_bytes: bytes) -> str:
        """Solve a CAPTCHA image. Returns the answer text (uppercased)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...


class TwoCaptchaSolver(CaptchaSolver):
    """Solve CAPTCHAs via 2captcha.com (human workers, high accuracy)."""

    def __init__(self, api_key: str) -> None:
        from twocaptcha import TwoCaptcha

        self._solver = TwoCaptcha(api_key, defaultTimeout=60, pollingInterval=5)
        logger.info("2Captcha solver initialized")

    @property
    def name(self) -> str:
        return "2captcha"

    async def solve(self, image_bytes: bytes) -> str:
        """Send image to 2captcha and return the solved text."""
        loop = asyncio.get_running_loop()

        # Write image to a temp file (2captcha SDK prefers file paths)
        def _solve_sync() -> str:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(image_bytes)
                tmp_path = f.name
            try:
                result = self._solver.normal(
                    tmp_path,
                    caseSensitive=1,
                    minLength=4,
                    maxLength=6,
                    lang="en",
                )
                return result["code"].upper()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        answer = await loop.run_in_executor(None, _solve_sync)
        logger.info("2Captcha solved: %s", answer)
        return answer


class LocalOcrSolver(CaptchaSolver):
    """Solve CAPTCHAs locally with ddddocr (free, less accurate)."""

    def __init__(self) -> None:
        import ddddocr

        try:
            self._ocr = ddddocr.DdddOcr(beta=True, show_ad=False)
        except TypeError:
            self._ocr = ddddocr.DdddOcr(beta=True)
        logger.info("Local OCR solver (ddddocr) initialized")

    @property
    def name(self) -> str:
        return "ddddocr"

    async def solve(self, image_bytes: bytes) -> str:
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, lambda: self._ocr.classification(image_bytes).upper()
        )
        logger.info("ddddocr solved: %s", answer)
        return answer


def create_solver() -> CaptchaSolver:
    """Create the best available CAPTCHA solver.

    Prefers 2captcha if TWO_CAPTCHA_API_KEY is set, otherwise falls back to ddddocr.
    """
    api_key = os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
    if api_key:
        try:
            return TwoCaptchaSolver(api_key)
        except Exception:
            logger.exception("Failed to initialize 2Captcha — falling back to local OCR")

    return LocalOcrSolver()


def create_solvers() -> tuple[CaptchaSolver, CaptchaSolver | None]:
    """Create both solvers for cascading strategy.

    Returns (primary, fallback) where:
      - primary = LocalOcrSolver (free, instant ~0.05s)
      - fallback = TwoCaptchaSolver or None (paid, ~6s, different error profile)

    Usage: try primary first.  If server rejects, retry with fallback.
    """
    primary = LocalOcrSolver()

    fallback: CaptchaSolver | None = None
    api_key = os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
    if api_key:
        try:
            fallback = TwoCaptchaSolver(api_key)
        except Exception:
            logger.exception("Failed to initialize 2Captcha fallback solver")

    logger.info(
        "Cascading solvers: primary=%s, fallback=%s",
        primary.name,
        fallback.name if fallback else "none",
    )
    return primary, fallback
