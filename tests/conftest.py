"""Minimal pytest configuration for EventMonitor."""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _set_test_env():
    """Ensure predictable environment variables for tests."""
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
    os.environ.setdefault("GOOGLE_API_KEY", "test-google-key")
    os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.invalid/webhook")
