from __future__ import annotations

import os
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup
from curl_cffi import requests
from .worker_protocol import check_cooldown, defer_until


BASE_URL = "https://irs.thsrc.com.tw"
BOOKING_PAGE_URL = f"{BASE_URL}/IMINT/?locale=tw"

DEFAULT_BROWSER_IMPERSONATE = "firefox"
SUPPORTED_BROWSER_IMPERSONATES = frozenset(("firefox", "chrome"))

SESSION_MAX_ATTEMPTS = 4
SESSION_MAX_ELAPSED_SECONDS = 100.0
SESSION_RETRY_DELAYS_SECONDS = (0.0, 2.0, 5.0, 10.0)
SESSION_REQUEST_TIMEOUT = (10.0, 20.0)


@dataclass(frozen=True)
class SessionHandshakeResult:
    response: Optional[requests.Response]
    jsession_id: Optional[str]
    classification: str
    title: str
    attempts: int
    elapsed_seconds: float
    browser_impersonate: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.response is not None and self.jsession_id)


def resolve_browser_impersonate(value: Optional[str] = None) -> str:
    """Resolve the configured curl_cffi browser target to a supported value."""
    configured = value
    if configured is None:
        configured = os.environ.get("THSR_BROWSER_IMPERSONATE", "")
    normalized = configured.strip().lower()
    if normalized in SUPPORTED_BROWSER_IMPERSONATES:
        return normalized
    return DEFAULT_BROWSER_IMPERSONATE


def booking_headers() -> dict:
    """Add locale preferences without overriding curl_cffi's coherent headers."""
    return {
        "Accept-Language": "zh-TW,zh;q=0.8,en-US;q=0.5,en;q=0.3",
    }


class BookingSession(requests.Session):
    def request(self, method, url, **kwargs):
        check_cooldown()
        response = super().request(method, url, **kwargs)
        delay = retry_after_seconds(response)
        if response.status_code == 429:
            delay = delay if delay is not None else 30.0
        if delay is not None:
            defer_until(time.time() + delay)
        return response


def create_booking_session(
    impersonate: Optional[str] = None,
    logger: Callable[[str], None] = print,
) -> requests.Session:
    configured = impersonate
    if configured is None:
        configured = os.environ.get("THSR_BROWSER_IMPERSONATE", "")
    browser = resolve_browser_impersonate(configured)

    if configured and configured.strip().lower() not in SUPPORTED_BROWSER_IMPERSONATES:
        logger(
            "Unsupported THSR_BROWSER_IMPERSONATE value "
            f"{configured!r}; falling back to {DEFAULT_BROWSER_IMPERSONATE}."
        )

    session = BookingSession(impersonate=browser)
    session.headers.update(booking_headers())
    session.max_redirects = 20
    setattr(session, "_thsr_browser_impersonate", browser)
    return session


def cookie_names(cookie_jar) -> List[str]:
    """Return cookie names across curl_cffi and requests-style cookie jars."""
    if cookie_jar is None:
        return []

    names = set()
    jar = getattr(cookie_jar, "jar", cookie_jar)
    try:
        for cookie in jar:
            name = getattr(cookie, "name", None)
            if name:
                names.add(str(name))
    except TypeError:
        pass

    try:
        names.update(str(name) for name in cookie_jar.keys())
    except (AttributeError, TypeError):
        pass
    return sorted(names)


def get_jsession_id(
    session: requests.Session,
    response: requests.Response,
) -> Optional[str]:
    """Read JSESSIONID without including its value in diagnostics."""
    for cookie_jar in (session.cookies, response.cookies):
        jar = getattr(cookie_jar, "jar", cookie_jar)
        try:
            for cookie in jar:
                if str(getattr(cookie, "name", "")).upper() == "JSESSIONID":
                    return str(cookie.value)
        except TypeError:
            pass

        try:
            value = cookie_jar.get("JSESSIONID")
        except (AttributeError, KeyError, TypeError):
            value = None
        if value:
            return str(value)
    return None


def classify_session_response(response: requests.Response) -> Tuple[str, str]:
    """Classify a THSR session response without exposing response content."""
    soup = BeautifulSoup(response.text or "", "html.parser")
    title = soup.title.get_text(" ", strip=True)[:120] if soup.title else "(no title)"
    searchable = f"{title} {soup.get_text(' ', strip=True)[:10000]}".lower()

    if response.status_code == 429:
        return "rate-limited", title
    if response.status_code >= 500:
        return "maintenance-or-overloaded", title
    if any(
        marker in searchable
        for marker in (
            "waiting room",
            "queue-it",
            "queue",
            "排隊",
            "等候進入",
            "流量管制",
        )
    ):
        return "queue", title
    if any(
        marker in searchable
        for marker in (
            "maintenance",
            "service unavailable",
            "系統維護",
            "系統忙碌",
            "稍後再試",
        )
    ):
        return "maintenance-or-overloaded", title
    if soup.select_one("#BookingS1Form_homeCaptcha_passCode"):
        return "booking-page", title
    return "unexpected-page", title


def retry_after_seconds(
    response: requests.Response,
    now_epoch: Optional[float] = None,
) -> Optional[float]:
    """Preserve the server's full cooldown, including long Retry-After values."""
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            current_time = time.time() if now_epoch is None else now_epoch
            seconds = retry_at.timestamp() - current_time
        except (TypeError, ValueError, OverflowError):
            return None
    return max(seconds, 0.0) if math.isfinite(seconds) else None


def establish_booking_session(
    session: requests.Session,
    *,
    max_attempts: int = SESSION_MAX_ATTEMPTS,
    retry_delays: Sequence[float] = SESSION_RETRY_DELAYS_SECONDS,
    max_elapsed_seconds: float = SESSION_MAX_ELAPSED_SECONDS,
    request_timeout: Tuple[float, float] = SESSION_REQUEST_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    logger: Callable[[str], None] = print,
) -> SessionHandshakeResult:
    """Establish a booking session with bounded, server-aware retries."""
    started_at = monotonic()
    browser = getattr(
        session, "_thsr_browser_impersonate", DEFAULT_BROWSER_IMPERSONATE
    )
    last_response = None
    last_classification = "connection-error"
    last_title = "(no response)"
    last_error = "No response received"
    attempts = 0
    server_delay = 0.0

    for attempt_index in range(max_attempts):
        configured_delay = (
            float(retry_delays[attempt_index])
            if attempt_index < len(retry_delays)
            else 0.0
        )
        delay = max(configured_delay, server_delay)
        elapsed = monotonic() - started_at
        if delay > 0:
            if elapsed + delay >= max_elapsed_seconds:
                last_error = "Session retry budget exhausted before the next attempt"
                break
            logger(
                f"Session retry {attempt_index + 1}/{max_attempts} in {delay:.0f}s..."
            )
            sleep(delay)
        server_delay = 0.0

        if monotonic() - started_at >= max_elapsed_seconds:
            last_error = "Session retry budget exhausted"
            break

        attempts += 1
        try:
            response = session.get(
                BOOKING_PAGE_URL,
                timeout=request_timeout,
                allow_redirects=True,
            )
            last_response = response
            classification, title = classify_session_response(response)
            jsession_id = get_jsession_id(session, response)
            if classification == "booking-page" and not jsession_id:
                classification = "missing-jsessionid"

            names = sorted(
                set(cookie_names(session.cookies) + cookie_names(response.cookies))
            )
            logger(
                f"Session response {attempts}/{max_attempts}: "
                f"status={response.status_code}, type={classification}, "
                f"title={title!r}, cookies={names}"
            )

            last_classification = classification
            last_title = title
            last_error = f"{classification}, HTTP {response.status_code}"
            if response.ok and classification == "booking-page" and jsession_id:
                return SessionHandshakeResult(
                    response=response,
                    jsession_id=jsession_id,
                    classification=classification,
                    title=title,
                    attempts=attempts,
                    elapsed_seconds=monotonic() - started_at,
                    browser_impersonate=browser,
                )
            server_delay = retry_after_seconds(response, now_epoch=wall_time())
            if server_delay is None:
                server_delay = 30.0 if response.status_code == 429 else 0.0
        except requests.exceptions.RequestException as exc:
            last_classification = "connection-error"
            last_title = "(no response)"
            last_error = f"{type(exc).__name__}: {exc}"
            logger(
                f"Session request {attempts}/{max_attempts} failed: {last_error}"
            )

    return SessionHandshakeResult(
        response=last_response,
        jsession_id=None,
        classification=last_classification,
        title=last_title,
        attempts=attempts,
        elapsed_seconds=monotonic() - started_at,
        browser_impersonate=browser,
        error=last_error,
    )


def probe_booking_session() -> SessionHandshakeResult:
    """Perform the same GET-only handshake used by a real booking attempt."""
    session = create_booking_session()
    try:
        return establish_booking_session(session)
    finally:
        session.close()
