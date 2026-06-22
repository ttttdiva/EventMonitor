"""
Helpers for detecting and handling rate limits.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping, Optional

import requests


DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60
MAX_RATE_LIMIT_WAIT_SECONDS = 3600
RATE_LIMIT_BUFFER_SECONDS = 5

_RATE_LIMIT_KEYWORDS = (
    "429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "retry later",
    "retry in",
    "retry this action",
)


def _normalize_headers(
    headers: Optional[Mapping[str, str]],
) -> Mapping[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): value for key, value in headers.items()}


def parse_retry_after_seconds(
    retry_after: Optional[str],
) -> Optional[int]:
    if not retry_after:
        return None

    value = retry_after.strip()
    if not value:
        return None

    if value.isdigit():
        return max(0, int(value))

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
    return max(0, seconds)


def extract_rate_limit_wait_seconds(
    text: str,
    headers: Optional[Mapping[str, str]] = None,
) -> Optional[int]:
    normalized_headers = _normalize_headers(headers)
    retry_after = parse_retry_after_seconds(normalized_headers.get("retry-after"))
    if retry_after is not None:
        return retry_after

    if not text:
        return None

    lowered = text.lower()
    patterns = (
        ("retry in ", 1),
        ("wait for ", 1),
        ("try again in ", 1),
    )

    for marker, seconds_per_unit in patterns:
        idx = lowered.find(marker)
        if idx == -1:
            continue

        tail = lowered[idx + len(marker):]
        parts = tail.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue

        amount = int(parts[0])
        unit = parts[1]
        if unit.startswith("second"):
            return amount * seconds_per_unit
        if unit.startswith("minute"):
            return amount * 60
        if unit.startswith("hour"):
            return amount * 3600

    marker = "you can retry this action in "
    idx = lowered.find(marker)
    if idx != -1:
        tail = lowered[idx + len(marker):]
        parts = tail.split()
        if len(parts) >= 2 and parts[0].isdigit():
            amount = int(parts[0])
            unit = parts[1]
            if unit.startswith("second"):
                return amount
            if unit.startswith("minute"):
                return amount * 60
            if unit.startswith("hour"):
                return amount * 3600

    marker = "retry this action in about "
    idx = lowered.find(marker)
    if idx != -1:
        tail = lowered[idx + len(marker):]
        parts = tail.split()
        if len(parts) >= 2 and parts[0].isdigit():
            amount = int(parts[0])
            unit = parts[1]
            if unit.startswith("second"):
                return amount
            if unit.startswith("minute"):
                return amount * 60
            if unit.startswith("hour"):
                return amount * 3600

    return None


def is_rate_limit_error(
    text: str = "",
    *,
    headers: Optional[Mapping[str, str]] = None,
    status_code: Optional[int] = None,
) -> bool:
    if status_code == 429:
        return True

    normalized_headers = _normalize_headers(headers)
    if normalized_headers.get("retry-after"):
        return True

    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in _RATE_LIMIT_KEYWORDS)


def get_rate_limit_wait_seconds(
    text: str,
    attempt: int,
    *,
    headers: Optional[Mapping[str, str]] = None,
    default_wait: int = DEFAULT_RATE_LIMIT_WAIT_SECONDS,
    max_wait: int = MAX_RATE_LIMIT_WAIT_SECONDS,
    buffer_seconds: int = RATE_LIMIT_BUFFER_SECONDS,
) -> int:
    parsed_wait = extract_rate_limit_wait_seconds(text, headers=headers)
    if parsed_wait is None:
        return max(1, min(max_wait, default_wait))

    bounded = max(1, min(max_wait, parsed_wait))
    return min(max_wait, bounded + max(0, buffer_seconds))


def request_with_rate_limit_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    logger,
    throttle: Optional[Callable[[], None]] = None,
    max_retries: Optional[int] = None,
    timeout: int = 30,
    sleep_fn: Callable[[float], None] = time.sleep,
    **kwargs,
) -> Optional[requests.Response]:
    method_name = method.upper()
    attempt = 0

    while True:
        if throttle:
            throttle()

        try:
            response = session.request(method_name, url, timeout=timeout, **kwargs)
            if response.status_code == 429:
                if max_retries is not None and attempt >= max_retries:
                    response.close()
                    logger.error(
                        f"{method_name} request rate limited for {url} and retry budget exhausted"
                    )
                    return None

                wait_seconds = get_rate_limit_wait_seconds(
                    response.text,
                    attempt,
                    headers=response.headers,
                )
                response.close()
                logger.warning(
                    f"{method_name} request rate limited for {url}; "
                    f"waiting {wait_seconds}s before retry {attempt + 1}"
                    + (
                        f"/{max_retries}"
                        if max_retries is not None else ""
                    )
                )
                sleep_fn(wait_seconds)
                attempt += 1
                continue

            response.raise_for_status()
            return response

        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status_code = response.status_code if response is not None else None
            headers = response.headers if response is not None else None
            response_text = response.text if response is not None else ""
            error_text = f"{exc}\n{response_text}".strip()

            if (
                (max_retries is None or attempt < max_retries)
                and is_rate_limit_error(
                error_text,
                headers=headers,
                status_code=status_code,
                )
            ):
                if response is not None:
                    response.close()
                wait_seconds = get_rate_limit_wait_seconds(
                    error_text,
                    attempt,
                    headers=headers,
                )
                logger.warning(
                    f"{method_name} request rate limited for {url}; "
                    f"waiting {wait_seconds}s before retry {attempt + 1}"
                    + (
                        f"/{max_retries}"
                        if max_retries is not None else ""
                    )
                )
                sleep_fn(wait_seconds)
                attempt += 1
                continue

            logger.error(f"{method_name} request failed for {url}: {exc}")
            return None
