import asyncio

import pytest

from src.services.account_processor import AccountProcessor
from src.status_notifier import StatusNotifier


class DummyStatusNotifier:
    def __init__(self):
        self.completed_accounts = 0
        self.completed_discord_servers = 0

    def increment_completed_accounts(self):
        self.completed_accounts += 1

    def increment_completed_discord_servers(self):
        self.completed_discord_servers += 1


def test_status_notifier_calculates_progress_percent():
    notifier = StatusNotifier(config={})
    notifier.set_target_counts(total_accounts=3, total_discord_servers=1)
    notifier.increment_processed_accounts()
    notifier.increment_completed_accounts()
    notifier.increment_completed_accounts()
    notifier.increment_completed_discord_servers()

    data = notifier._get_status_data()

    assert data["total_accounts"] == 3
    assert data["total_discord_servers"] == 1
    assert data["completed_targets"] == 3
    assert data["total_targets"] == 4
    assert data["progress_percent"] == 75.0


@pytest.mark.asyncio
async def test_account_processor_counts_failed_account_as_completed():
    status_notifier = DummyStatusNotifier()
    processor = AccountProcessor(
        config={},
        db_manager=None,
        event_detector=None,
        twitter_monitor=None,
        discord_notifier=None,
        backup_manager=None,
        hydrus_client=None,
        status_notifier=status_notifier,
    )

    async def fail_monitor_account(_account):
        raise RuntimeError("boom")

    processor._process_monitor_account = fail_monitor_account

    with pytest.raises(RuntimeError, match="boom"):
        await processor.process_account(
            {"username": "example_user", "platform": "twitter"},
            semaphore=asyncio.Semaphore(1),
        )

    assert status_notifier.completed_accounts == 1
    assert status_notifier.completed_discord_servers == 0
