import subprocess
import sys

import pytest
import requests

from src.rate_limit_utils import (
    extract_rate_limit_wait_seconds,
    get_rate_limit_wait_seconds,
    request_with_rate_limit_retry,
)
from src.subprocess_utils import _run_with_idle_timeout_once, run_with_idle_timeout


class DummyLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


class DummyResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def close(self):
        self.closed = True


class DummySession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, timeout, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_extract_rate_limit_wait_seconds_uses_retry_after_header():
    wait_seconds = extract_rate_limit_wait_seconds(
        "",
        headers={"Retry-After": "12"},
    )

    assert wait_seconds == 12


def test_get_rate_limit_wait_seconds_uses_fixed_interval_without_server_hint():
    assert get_rate_limit_wait_seconds("429 Too Many Requests", 0) == 60
    assert get_rate_limit_wait_seconds("429 Too Many Requests", 3) == 60


def test_request_with_rate_limit_retry_waits_and_retries():
    session = DummySession(
        [
            DummyResponse(status_code=429, headers={"Retry-After": "2"}),
            DummyResponse(status_code=200, text="ok"),
        ]
    )
    logger = DummyLogger()
    slept = []
    throttles = []

    response = request_with_rate_limit_retry(
        session,
        "get",
        "https://example.com",
        logger=logger,
        throttle=lambda: throttles.append("tick"),
        max_retries=1,
        sleep_fn=slept.append,
    )

    assert response is not None
    assert response.status_code == 200
    assert len(session.calls) == 2
    assert len(throttles) == 2
    assert slept == [get_rate_limit_wait_seconds("", 0, headers={"Retry-After": "2"})]
    assert logger.warnings


def test_request_with_rate_limit_retry_retries_without_budget_cap_by_default():
    session = DummySession(
        [
            DummyResponse(status_code=429, text="429 Too Many Requests"),
            DummyResponse(status_code=429, text="429 Too Many Requests"),
            DummyResponse(status_code=200, text="ok"),
        ]
    )
    logger = DummyLogger()
    slept = []

    response = request_with_rate_limit_retry(
        session,
        "get",
        "https://example.com",
        logger=logger,
        sleep_fn=slept.append,
    )

    assert response is not None
    assert response.status_code == 200
    assert len(session.calls) == 3
    assert slept == [60, 60]
    assert logger.warnings


def test_run_with_idle_timeout_retries_rate_limited_returncode():
    calls = []
    slept = []
    results = [
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=1,
            stdout="",
            stderr="429 Too Many Requests retry in 3 seconds",
        ),
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    ]

    def fake_runner(cmd, idle_timeout, text):
        calls.append((tuple(cmd), idle_timeout, text))
        return results.pop(0)

    result = run_with_idle_timeout(
        ["gallery-dl"],
        idle_timeout=10,
        rate_limit_retries=1,
        sleep_fn=slept.append,
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert len(calls) == 2
    assert slept == [get_rate_limit_wait_seconds("retry in 3 seconds", 0)]


def test_run_with_idle_timeout_retries_rate_limited_timeout():
    calls = []
    slept = []
    timeout = subprocess.TimeoutExpired(
        cmd=["gallery-dl"],
        timeout=10,
        output="",
        stderr="429 Too Many Requests retry in 4 seconds",
    )
    results = [
        timeout,
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    ]

    def fake_runner(cmd, idle_timeout, text):
        calls.append((tuple(cmd), idle_timeout, text))
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    result = run_with_idle_timeout(
        ["gallery-dl"],
        idle_timeout=10,
        rate_limit_retries=1,
        sleep_fn=slept.append,
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert len(calls) == 2
    assert slept == [get_rate_limit_wait_seconds("retry in 4 seconds", 0)]


def test_run_with_idle_timeout_retries_rate_limit_without_budget_cap_by_default():
    calls = []
    slept = []
    results = [
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=1,
            stdout="",
            stderr="429 Too Many Requests retry in 1 seconds",
        ),
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=1,
            stdout="",
            stderr="429 Too Many Requests retry in 1 seconds",
        ),
        subprocess.CompletedProcess(
            args=["gallery-dl"],
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    ]

    def fake_runner(cmd, idle_timeout, text):
        calls.append((tuple(cmd), idle_timeout, text))
        return results.pop(0)

    result = run_with_idle_timeout(
        ["gallery-dl"],
        idle_timeout=10,
        sleep_fn=slept.append,
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert len(calls) == 3
    assert slept == [
        get_rate_limit_wait_seconds("retry in 1 seconds", 0),
        get_rate_limit_wait_seconds("retry in 1 seconds", 1),
    ]


def test_run_with_idle_timeout_keeps_non_rate_limit_timeout_behavior():
    timeout = subprocess.TimeoutExpired(
        cmd=["gallery-dl"],
        timeout=10,
        output="",
        stderr="plain timeout",
    )

    def fake_runner(cmd, idle_timeout, text):
        raise timeout

    with pytest.raises(subprocess.TimeoutExpired):
        run_with_idle_timeout(
            ["gallery-dl"],
            idle_timeout=10,
            rate_limit_retries=1,
            runner=fake_runner,
        )


def test_run_with_idle_timeout_once_extends_deadline_after_rate_limit_line():
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "print('429 Too Many Requests retry in 0 seconds', flush=True); "
            "time.sleep(6); "
            "print('ok', flush=True)"
        ),
    ]

    result = _run_with_idle_timeout_once(cmd, idle_timeout=5, text=True)

    assert result.returncode == 0
    assert "ok" in result.stdout
