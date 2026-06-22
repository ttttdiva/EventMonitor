"""
gallery-dl subprocess helpers with idle-timeout and rate-limit retry support.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, List, Optional

from .rate_limit_utils import get_rate_limit_wait_seconds, is_rate_limit_error


logger = logging.getLogger("EventMonitor.SubprocessUtils")


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value)


def _run_with_idle_timeout_once(
    cmd: List[str],
    idle_timeout: int = 120,
    text: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )

    stdout_buf: list = []
    stderr_buf: list = []
    deadline = time.monotonic() + idle_timeout
    lock = threading.Lock()

    def _reader(stream, buf):
        nonlocal deadline
        try:
            while True:
                line = stream.readline()
                if not line:
                    break

                now = time.monotonic()
                line_text = _to_text(line)
                with lock:
                    buf.append(line)
                    deadline = now + idle_timeout
                    if is_rate_limit_error(line_text):
                        wait_seconds = get_rate_limit_wait_seconds(line_text, 0)
                        # Allow the subprocess to stay quiet for the server-advised
                        # backoff period and still have one full idle window afterward.
                        deadline = max(deadline, now + wait_seconds + idle_timeout)
        except (ValueError, OSError):
            pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_buf), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_buf), daemon=True)
    t_out.start()
    t_err.start()

    while proc.poll() is None:
        time.sleep(2)
        with lock:
            expired = time.monotonic() >= deadline
        if expired:
            proc.kill()
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            join = "" if text else b""
            raise subprocess.TimeoutExpired(
                cmd,
                idle_timeout,
                output=join.join(stdout_buf),
                stderr=join.join(stderr_buf),
            )

    t_out.join(timeout=10)
    t_err.join(timeout=10)

    join = "" if text else b""
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=join.join(stdout_buf),
        stderr=join.join(stderr_buf),
    )


def run_with_idle_timeout(
    cmd: List[str],
    idle_timeout: int = 120,
    text: bool = True,
    rate_limit_retries: Optional[int] = None,
    default_rate_limit_wait: int = 60,
    max_rate_limit_wait: int = 3600,
    sleep_fn: Callable[[float], None] = time.sleep,
    runner=None,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess with idle-timeout enforcement and rate-limit retries.
    """
    run_once = runner or _run_with_idle_timeout_once

    attempt = 0
    while True:
        can_retry = rate_limit_retries is None or attempt < rate_limit_retries
        try:
            result = run_once(cmd, idle_timeout=idle_timeout, text=text)
        except subprocess.TimeoutExpired as exc:
            error_text = "\n".join(filter(None, (_to_text(exc.output), _to_text(exc.stderr))))
            if can_retry and is_rate_limit_error(error_text):
                wait_seconds = get_rate_limit_wait_seconds(
                    error_text,
                    attempt,
                    default_wait=default_rate_limit_wait,
                    max_wait=max_rate_limit_wait,
                )
                stderr_snippet = _to_text(exc.stderr).strip()[:300]
                logger.warning(
                    f"Command rate limited after idle timeout; waiting {wait_seconds}s "
                    f"before retry {attempt + 1}"
                    + (
                        f"/{rate_limit_retries}"
                        if rate_limit_retries is not None else ""
                    )
                    + f": {cmd[0]}"
                    + (f"\n  stderr: {stderr_snippet}" if stderr_snippet else "")
                )
                sleep_fn(wait_seconds)
                attempt += 1
                continue
            raise

        combined_text = "\n".join(
            filter(None, (_to_text(result.stdout), _to_text(result.stderr)))
        )
        if (
            result.returncode != 0
            and can_retry
            and is_rate_limit_error(combined_text)
        ):
            wait_seconds = get_rate_limit_wait_seconds(
                combined_text,
                attempt,
                default_wait=default_rate_limit_wait,
                max_wait=max_rate_limit_wait,
            )
            # Log truncated stderr to help diagnose false-positive rate-limit detection
            stderr_snippet = _to_text(result.stderr).strip()[:300]
            logger.warning(
                f"Command rate limited (rc={result.returncode}); waiting {wait_seconds}s before retry "
                f"{attempt + 1}"
                + (
                    f"/{rate_limit_retries}"
                    if rate_limit_retries is not None else ""
                )
                + f": {cmd[0]}"
                + (f"\n  stderr: {stderr_snippet}" if stderr_snippet else "")
            )
            sleep_fn(wait_seconds)
            attempt += 1
            continue

        return result
