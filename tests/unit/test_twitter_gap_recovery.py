from datetime import datetime, timedelta, timezone

import pytest

from src.twitter_monitor import TwitterMonitor


class DummyUser:
    def __init__(self, user_id="123", display_name="dummy"):
        self.id = user_id
        self.displayname = display_name


class DummyPool:
    async def stats(self):
        return {}

    async def accounts_info(self):
        return [{"active": True, "locks": {"UserTweets": 0}}]


class DummyTweet:
    def __init__(self, tweet_id: str, date: datetime, username: str = "artist", text: str = ""):
        self.id = int(tweet_id)
        self.date = date
        self.rawContent = text or f"tweet-{tweet_id}"
        self.user = type("User", (), {"username": username})()
        self.media = type("Media", (), {"photos": [], "videos": []})()
        self.retweetedTweet = None


class DummyApi:
    def __init__(self, tweets):
        self._tweets = tweets
        self.user_by_login_calls = 0
        self.user_tweets_calls = 0
        self.user_tweets_and_replies_calls = 0
        self.user_media_calls = 0
        self.pool = DummyPool()

    async def user_by_login(self, username):
        self.user_by_login_calls += 1
        return DummyUser(display_name=username)

    async def user_tweets(self, user_id):
        self.user_tweets_calls += 1
        for tweet in self._tweets:
            yield tweet

    async def user_tweets_and_replies(self, user_id):
        self.user_tweets_and_replies_calls += 1
        for tweet in self._tweets:
            yield tweet

    async def user_media(self, user_id):
        self.user_media_calls += 1
        for tweet in self._tweets:
            yield tweet


class DummyDbManager:
    def __init__(self, existing_ids, latest_date):
        self._existing_ids = set(existing_ids)
        self._latest_date = latest_date

    def check_tweet_exists(self, tweet_id):
        return str(tweet_id) in self._existing_ids

    def get_latest_tweet_date(self, username):
        return self._latest_date


@pytest.mark.asyncio
async def test_quick_check_detects_gap_beyond_first_two_tweets(monkeypatch):
    latest_date = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
    tweets = [
        DummyTweet("2033000000000000001", latest_date),
        DummyTweet("2032999999999999999", latest_date - timedelta(minutes=5)),
        DummyTweet("2032999999999999998", latest_date - timedelta(minutes=10)),
    ]
    monitor = TwitterMonitor(
        {
            "tweet_settings": {
                "quick_check_scan_count": 5,
                "incremental_overlap_hours": 48,
                "gallery_dl": {"enabled": False},
            }
        },
        db_manager=DummyDbManager(
            existing_ids={"2033000000000000001", "2032999999999999999"},
            latest_date=latest_date,
        ),
    )
    monitor.api = DummyApi(tweets)

    async def noop():
        return None

    monkeypatch.setattr(monitor, "_initialize_accounts", noop)

    assert await monitor.check_for_new_tweets(
        "artist",
        latest_tweet_id="2033000000000000001",
        latest_tweet_date=latest_date,
    ) is True


@pytest.mark.asyncio
async def test_quick_check_reuses_result_for_same_latest_marker(monkeypatch):
    latest_date = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
    tweets = [
        DummyTweet("2033000000000000001", latest_date),
    ]
    monitor = TwitterMonitor(
        {
            "tweet_settings": {
                "quick_check_scan_count": 5,
                "quick_check_fallback_mode": "none",
                "incremental_overlap_hours": 48,
                "gallery_dl": {"enabled": False},
            }
        },
        db_manager=DummyDbManager(
            existing_ids={"2033000000000000001"},
            latest_date=latest_date,
        ),
    )
    monitor.api = DummyApi(tweets)

    async def noop():
        return None

    monkeypatch.setattr(monitor, "_initialize_accounts", noop)

    first = await monitor.check_for_new_tweets(
        "artist",
        latest_tweet_id="2033000000000000001",
        latest_tweet_date=latest_date,
    )
    second = await monitor.check_for_new_tweets(
        "artist",
        latest_tweet_id="2033000000000000001",
        latest_tweet_date=latest_date,
    )

    assert first is False
    assert second is False
    assert monitor.api.user_by_login_calls == 1
    assert monitor.api.user_tweets_calls == 1


@pytest.mark.asyncio
async def test_quick_check_default_skips_replies_fallback(monkeypatch):
    latest_date = datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc)
    tweets = [
        DummyTweet("2033000000000000001", latest_date),
    ]
    monitor = TwitterMonitor(
        {
            "tweet_settings": {
                "quick_check_scan_count": 5,
                "incremental_overlap_hours": 48,
                "gallery_dl": {"enabled": False},
            }
        },
        db_manager=DummyDbManager(
            existing_ids={"2033000000000000001"},
            latest_date=latest_date,
        ),
    )
    monitor.api = DummyApi(tweets)

    async def noop():
        return None

    monkeypatch.setattr(monitor, "_initialize_accounts", noop)

    assert await monitor.check_for_new_tweets(
        "artist",
        latest_tweet_id="2033000000000000001",
        latest_tweet_date=latest_date,
    ) is False

    assert monitor.api.user_tweets_calls == 1
    assert monitor.api.user_tweets_and_replies_calls == 0
    assert monitor.api.user_media_calls == 1


@pytest.mark.asyncio
async def test_twscrape_incremental_fetch_recovers_gap_behind_known_tweet(monkeypatch):
    latest_date = datetime.now(timezone.utc) - timedelta(hours=2)
    tweets = [
        DummyTweet("2033000000000000001", latest_date),
        DummyTweet("2032999999999999998", latest_date - timedelta(minutes=10)),
        DummyTweet("2032999999999999990", latest_date - timedelta(minutes=20)),
    ]

    monitor = TwitterMonitor(
        {
            "tweet_settings": {
                "incremental_overlap_hours": 24,
                "consecutive_known_stop_count": 5,
                "gallery_dl": {"enabled": False},
            }
        }
    )
    monitor.api = DummyApi(tweets)

    async def noop():
        return None

    async def always_true(*args, **kwargs):
        return True

    async def false_sensitive(username):
        return False

    class DummyDatabaseManager:
        def __init__(self, config):
            self.config = config

        def get_existing_tweet_ids(self, username):
            return {"2033000000000000001", "2032999999999999990"}

        def get_latest_tweet_date(self, username):
            return latest_date

        def get_latest_tweet_id(self, username):
            return "2033000000000000001"

    monkeypatch.setattr("src.database.DatabaseManager", DummyDatabaseManager)
    monkeypatch.setattr(monitor, "_initialize_accounts", noop)
    monkeypatch.setattr(monitor, "check_for_new_tweets", always_true)
    monkeypatch.setattr(monitor, "_resolve_account_sensitive", false_sensitive)

    recovered = await monitor._get_user_tweets_twscrape_internal(
        "artist",
        days_lookback=30,
        latest_date_override=latest_date,
        latest_id_override="2033000000000000001",
    )

    assert [tweet["id"] for tweet in recovered] == ["2032999999999999998"]
