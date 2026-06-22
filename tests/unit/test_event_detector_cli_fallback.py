import json
import os
import sys
import types

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from event_detector import EventDetector  # noqa: E402


def base_config(routes, providers=None):
    default_providers = {
        "gemini_cli": {
            "command": 'python -c "import sys; sys.exit(1)"',
            "args": ["-o", "json"],
            "timeout": 5,
            "env_vars": {},
        },
        "codex_cli": {
            "command": "python tests/mock_codex_cli.py",
            "args": [
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-rules",
                "--sandbox",
                "read-only",
            ],
            "timeout": 5,
            "env_vars": {},
        },
        "gemini_api": {
            "timeout": 5,
            "env_vars": {},
        },
        "openai_api": {
            "timeout": 5,
            "env_vars": {},
        },
    }
    if providers:
        default_providers.update(providers)

    return {
        "event_detection": {
            "enabled": True,
            "keywords": ["参加", "join"],
            "exclude_keywords": [],
            "openai_temperature": 0.3,
        },
        "llm_providers": default_providers,
        "llm_routes": routes,
    }


@pytest.mark.asyncio
async def test_codex_cli_uses_route_model_and_effort_after_gemini_cli_failure():
    config = base_config([
        {
            "name": "gemini-cli-test",
            "provider": "gemini_cli",
            "model": "gemini-cli-model",
        },
        {
            "name": "codex-spark-medium",
            "provider": "codex_cli",
            "model": "gpt-5.3-codex-spark",
            "effort": "medium",
        },
    ])
    detector = EventDetector(config)

    results = await detector.detect_event_tweets([
        {
            "id": "codex_fallback_test",
            "text": "コミケに参加します。スペースは東A-12aです。",
        }
    ])

    assert len(results) == 1
    reason = results[0]["event_analysis"]["reason"]
    assert "Model: gpt-5.3-codex-spark" in reason
    assert "Effort: medium" in reason
    assert "Prompt contains tweet: True" in reason
    assert "Schema: True" in reason


@pytest.mark.asyncio
async def test_codex_cli_rate_limit_falls_through_to_next_route():
    config = base_config(
        [
            {
                "name": "codex-spark-medium",
                "provider": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "effort": "medium",
            },
            {
                "name": "codex-5.5-medium",
                "provider": "codex_cli",
                "model": "gpt-5.5",
                "effort": "medium",
            },
        ],
        providers={
            "codex_cli": {
                "command": "python tests/mock_codex_cli.py",
                "args": [
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                ],
                "timeout": 5,
                "env_vars": {"MOCK_CODEX_RATE_LIMIT_MODEL": "gpt-5.3-codex-spark"},
            },
        },
    )
    detector = EventDetector(config)

    results = await detector.detect_event_tweets([
        {
            "id": "codex_rate_limit_fallback_test",
            "text": "join test announcement",
        }
    ])

    assert len(results) == 1
    reason = results[0]["event_analysis"]["reason"]
    assert "Model: gpt-5.5" in reason
    assert "Effort: medium" in reason


@pytest.mark.asyncio
async def test_gemini_api_is_used_after_cli_fallbacks_fail():
    config = base_config(
        [
            {
                "name": "gemini-cli-fail",
                "provider": "gemini_cli",
                "model": "gemini-cli-model",
            },
            {
                "name": "codex-fail",
                "provider": "codex_cli",
                "model": "gpt-5.3-codex-spark",
            },
            {
                "name": "gemini-api-3-flash",
                "provider": "gemini_api",
                "model": "gemini-3-flash-preview",
            },
        ],
        providers={
            "codex_cli": {
                "command": 'python -c "import sys; sys.exit(1)"',
                "args": ["exec"],
                "timeout": 5,
                "env_vars": {},
            },
        },
    )

    class FakeGeminiModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            content = json.dumps({
                "is_event_related": True,
                "confidence": 0.9,
                "event_type": "コミケ",
                "event_date": None,
                "participation_type": "サークル参加",
                "reason": "Gemini API fallback",
            })
            return types.SimpleNamespace(text=content)

    gemini_models = FakeGeminiModels()
    detector = EventDetector(config)
    detector.gemini_client = types.SimpleNamespace(models=gemini_models)

    results = await detector.detect_event_tweets([
        {
            "id": "api_fallback_test",
            "text": "コミケに参加します。スペースは東A-12aです。",
        }
    ])

    assert len(results) == 1
    assert results[0]["event_analysis"]["reason"] == "Gemini API fallback"
    assert gemini_models.calls[0]["model"] == "gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_gemini_cli_quota_exhaustion_uses_next_route_until_cooldown():
    config = base_config(
        [
            {
                "name": "gemini-cli-quota",
                "provider": "gemini_cli",
                "model": "gemini-cli-model",
            },
            {
                "name": "codex-spark-medium",
                "provider": "codex_cli",
                "model": "gpt-5.3-codex-spark",
                "effort": "medium",
            },
        ],
        providers={
            "gemini_cli": {
                "command": (
                    "python -c \"import sys; "
                    "sys.stderr.write('TerminalQuotaError: quota will reset after 1h2m3s. "
                    "reason: QUOTA_EXHAUSTED retryDelayMs: 3723000'); "
                    "sys.exit(1)\""
                ),
                "args": ["-o", "json"],
                "timeout": 5,
                "env_vars": {},
            },
        },
    )
    detector = EventDetector(config)

    results = await detector.detect_event_tweets([
        {
            "id": "gemini_quota_test",
            "text": "コミケに参加します。スペースは東A-12aです。",
        }
    ])

    assert len(results) == 1
    assert detector.gemini_cli_quota_until is not None
    assert "Model: gpt-5.3-codex-spark" in results[0]["event_analysis"]["reason"]
