from src.account_priority import build_account_key, sort_accounts_for_platform


class DummyDbManager:
    def __init__(self, has_posts=None, recent_counts=None):
        self.has_posts = has_posts or {}
        self.recent_counts = recent_counts or {}

    def has_any_posts(self, username, platform):
        return self.has_posts.get((platform, username), False)

    def get_recent_post_count_twitter(self, username, days):
        return self.recent_counts.get(("twitter", username), 0)


def test_build_account_key_defaults_empty_platform_to_twitter():
    assert build_account_key("example", "") == "twitter:example"
    assert build_account_key("example", None) == "twitter:example"


def test_runtime_prioritized_account_moves_to_front_with_priority_sort():
    accounts = [
        {"username": "old_a", "platform": "twitter"},
        {"username": "old_b", "platform": "twitter"},
        {"username": "fresh_runtime", "platform": "twitter"},
    ]
    db_manager = DummyDbManager()

    sorted_accounts, log_message = sort_accounts_for_platform(
        platform="twitter",
        accounts=accounts,
        db_manager=db_manager,
        priority_config={"enabled": True, "window_days": 7},
        runtime_prioritized_accounts={build_account_key("fresh_runtime", "twitter")},
    )

    assert [account["username"] for account in sorted_accounts] == [
        "fresh_runtime",
        "old_a",
        "old_b",
    ]
    assert log_message is not None
    assert "fresh_runtime(runtime/new)" in log_message


def test_runtime_prioritized_account_moves_to_front_when_priority_sort_disabled():
    accounts = [
        {"username": "old_a", "platform": "twitter"},
        {"username": "fresh_runtime", "platform": "twitter"},
        {"username": "old_b", "platform": "twitter"},
    ]
    db_manager = DummyDbManager()

    sorted_accounts, log_message = sort_accounts_for_platform(
        platform="twitter",
        accounts=accounts,
        db_manager=db_manager,
        priority_config={"enabled": False, "window_days": 7},
        runtime_prioritized_accounts={build_account_key("fresh_runtime", "twitter")},
    )

    assert [account["username"] for account in sorted_accounts] == [
        "fresh_runtime",
        "old_a",
        "old_b",
    ]
    assert log_message is not None
    assert "Runtime priority [twitter]" in log_message
