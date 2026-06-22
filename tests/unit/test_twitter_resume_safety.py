import types
from datetime import datetime, timedelta
import pytest

from src.services.account_processor import AccountProcessor


class DummyDbManager:
    def __init__(self):
        self.checked_ids = []
        self.stale_checked_ids = []
        self.saved_event_batches = []
        self.notified_ids = []
        self.updated_hydrus = []
        self.pending_event_tweets = []
        self.unnotified_events = []
        self.pending_hydrus_tweets = []
        self.last_pending_since_date = None
        self.last_unnotified_since_date = None
        self.saved_all_tweets = []
        self.saved_log_only_tweets = []
        self.twitter_retry = {"monitor": {}, "log_only": {}}

    @staticmethod
    def _tweet_date(tweet):
        value = tweet["date"]
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)

    def get_tweets_pending_event_check(self, since_date=None):
        self.last_pending_since_date = since_date
        if since_date is None:
            return list(self.pending_event_tweets)
        return [
            tweet for tweet in self.pending_event_tweets
            if self._tweet_date(tweet) >= since_date
        ]

    def mark_stale_tweets_checked_for_event(self, before_date):
        stale = [
            tweet for tweet in self.pending_event_tweets
            if self._tweet_date(tweet) < before_date
        ]
        stale_ids = [tweet["id"] for tweet in stale]
        self.stale_checked_ids.extend(stale_ids)
        stale_id_set = {str(tweet_id) for tweet_id in stale_ids}
        self.pending_event_tweets = [
            tweet for tweet in self.pending_event_tweets
            if str(tweet["id"]) not in stale_id_set
        ]
        return len(stale_ids)

    def mark_tweets_checked_for_event(self, tweet_ids):
        self.checked_ids.append(list(tweet_ids))
        checked = {str(tweet_id) for tweet_id in tweet_ids}
        self.pending_event_tweets = [
            tweet for tweet in self.pending_event_tweets
            if str(tweet["id"]) not in checked
        ]
        return len(tweet_ids)

    def save_event_tweets(self, tweets, username):
        self.saved_event_batches.append((username, list(tweets)))

    def get_unnotified_tweets(self, since_date=None):
        self.last_unnotified_since_date = since_date
        if since_date is None:
            return list(self.unnotified_events)
        if not self.unnotified_events:
            return []
        if not isinstance(self.unnotified_events[0].tweet_date, datetime):
            return list(self.unnotified_events)
        return [
            tweet for tweet in self.unnotified_events
            if tweet.tweet_date >= since_date
        ]

    def mark_as_notified(self, tweet_id):
        self.notified_ids.append(tweet_id)

    def estimate_hydrus_expected_count(self, local_media):
        return len(local_media)

    def update_hydrus_import_status(self, tweet_id, imported_count, expected_count=None):
        self.updated_hydrus.append((tweet_id, imported_count, expected_count))

    def get_pending_hydrus_tweets(self):
        return list(self.pending_hydrus_tweets)

    def is_event_tweet(self, tweet_id):
        return tweet_id.startswith("event")

    def filter_new_tweets(self, tweets, username):
        saved_ids = {tweet["id"] for tweet in self.saved_all_tweets}
        return [tweet for tweet in tweets if tweet.get("id") not in saved_ids]

    def filter_log_only_tweets(self, tweets, username):
        saved_ids = {tweet["id"] for tweet in self.saved_log_only_tweets}
        return [tweet for tweet in tweets if tweet.get("id") not in saved_ids]

    def save_all_tweets(self, tweets, username):
        self.saved_all_tweets.extend(dict(tweet) for tweet in tweets)
        return len(tweets)

    def save_single_log_only_tweet(self, tweet, username):
        self.saved_log_only_tweets.append(dict(tweet))
        return True

    def get_tweet_count_for_user(self, username):
        return len([tweet for tweet in self.saved_all_tweets if tweet.get("username") == username])

    def get_log_only_tweet_count_for_user(self, username):
        return len([tweet for tweet in self.saved_log_only_tweets if tweet.get("username") == username])

    def get_twitter_retry_tweets(self, username, is_log_only=False):
        scope = "log_only" if is_log_only else "monitor"
        return [dict(tweet) for tweet in self.twitter_retry[scope].values()]

    def upsert_twitter_retry(self, username, tweet, is_log_only=False, error=None):
        scope = "log_only" if is_log_only else "monitor"
        self.twitter_retry[scope][tweet["id"]] = dict(tweet)

    def clear_twitter_retry(self, username, tweet_id, is_log_only=False):
        scope = "log_only" if is_log_only else "monitor"
        self.twitter_retry[scope].pop(tweet_id, None)


class DummyEventDetector:
    enabled = True

    def __init__(self):
        self.calls = []

    async def detect_event_tweets(self, tweets):
        self.calls.append([tweet["id"] for tweet in tweets])
        if not tweets:
            return []
        enriched = dict(tweets[0])
        enriched["event_analysis"] = {
            "is_event_related": True,
            "confidence": 0.9,
            "reason": "test",
            "event_type": "comic",
        }
        return [enriched]


class DummyDiscordNotifier:
    enabled = True

    def __init__(self):
        self.calls = []

    async def send_notification(self, tweet, username, display_name):
        self.calls.append((tweet["id"], username, display_name))


class DummyHydrusClient:
    def __init__(self, event_tweets_only=True):
        self.enabled = True
        self.import_settings = {"event_tweets_only": event_tweets_only}
        self.calls = []

    async def import_tweet_images(self, tweet, local_media):
        self.calls.append((tweet["id"], list(local_media)))
        return list(local_media)


class DummyStatusNotifier:
    def notify_running(self, **kwargs):
        return None

    def add_new_tweets(self, count):
        return None

    def increment_processed_accounts(self):
        return None

    def notify_error(self, *args):
        return None


class DummyBackupManager:
    backup_config = {"enabled": False}
    rclone_client = None

    def should_use_batch_mode(self, is_first_run=False):
        return False

    async def backup_tweet_and_save(
        self,
        tweet,
        username,
        is_log_only=False,
        hydrus_client=None,
        is_first_run=False,
    ):
        return True


class DummyGalleryExtractor:
    def __init__(self, media_paths):
        self.media_paths = media_paths
        self.calls = []

    def download_media_for_tweets(
        self,
        username,
        tweet_ids,
        move_to_images=True,
        is_private_account=False,
    ):
        self.calls.append((username, list(tweet_ids)))
        return {
            tweet_id: self.media_paths.get(tweet_id, [])
            for tweet_id in tweet_ids
            if self.media_paths.get(tweet_id)
        }


class DummyTwitterMonitor:
    def __init__(self, tweets=None, media_paths=None):
        self.tweets = list(tweets or [])
        self.gallery_dl_extractor = DummyGalleryExtractor(media_paths or {})

    async def get_user_tweets_with_gallery_dl_first(
        self,
        username,
        days_lookback=365,
        event_detection_enabled=True,
    ):
        return list(self.tweets), []

    def is_account_private(self, username):
        return False

    def get_resolved_twitter_id(self, username):
        return None


def make_processor(
    db_manager,
    event_detector=None,
    discord_notifier=None,
    hydrus_client=None,
    pending_event_max_age_days=0,
    twitter_monitor=None,
    status_notifier=None,
    backup_manager=None,
):
    return AccountProcessor(
        config={
            "event_detection": {"enabled": True},
            "tweet_settings": {
                "days_lookback": 30,
                "pending_event_max_age_days": pending_event_max_age_days,
            },
            "log_only_accounts": {"enabled": False},
            "monitored_accounts": [
                {
                    "username": "artist",
                    "platform": "twitter",
                    "display_name": "Artist",
                    "custom_tags": ["creator:test"],
                    "rank": 1,
                    "event_detection_enabled": True,
                }
            ],
            "media_storage": {
                "images_path": "images",
                "videos_path": "videos",
            },
        },
        db_manager=db_manager,
        event_detector=event_detector,
        twitter_monitor=twitter_monitor,
        discord_notifier=discord_notifier,
        backup_manager=backup_manager,
        hydrus_client=hydrus_client,
        status_notifier=status_notifier,
    )


def test_validate_tweet_download_rejects_incomplete_media():
    processor = make_processor(DummyDbManager())

    assert not processor._validate_tweet_download(
        "artist",
        {
            "id": "1",
            "media": ["a", "b"],
            "videos": [],
            "local_media": ["images/artist/1.jpg"],
        },
    )
    assert processor._validate_tweet_download(
        "artist",
        {
            "id": "2",
            "media": ["a"],
            "videos": [],
            "local_media": ["images/artist/2.jpg"],
        },
    )


@pytest.mark.asyncio
async def test_monitor_retries_queued_tweet_when_normal_fetch_is_empty():
    db_manager = DummyDbManager()
    db_manager.twitter_retry["monitor"]["retry-1"] = {
        "id": "retry-1",
        "text": "queued media tweet",
        "date": "2026-05-17T00:00:00+00:00",
        "url": "https://x.com/artist/status/retry-1",
        "media": ["https://example.test/retry.jpg"],
        "videos": [],
    }
    twitter_monitor = DummyTwitterMonitor(
        tweets=[],
        media_paths={"retry-1": ["images/artist/retry.jpg"]},
    )
    processor = make_processor(
        db_manager,
        twitter_monitor=twitter_monitor,
        backup_manager=DummyBackupManager(),
        hydrus_client=DummyHydrusClient(),
        status_notifier=DummyStatusNotifier(),
    )

    await processor._process_monitor_account(
        {
            "username": "artist",
            "display_name": "Artist",
            "custom_tags": [],
            "rank": 1,
        }
    )

    assert [tweet["id"] for tweet in db_manager.saved_all_tweets] == ["retry-1"]
    assert db_manager.twitter_retry["monitor"] == {}
    assert twitter_monitor.gallery_dl_extractor.calls == [("artist", ["retry-1"])]


@pytest.mark.asyncio
async def test_log_only_downloads_twscrape_media_and_clears_retry():
    db_manager = DummyDbManager()
    db_manager.twitter_retry["log_only"]["retry-log"] = {
        "id": "retry-log",
        "source": "twscrape",
        "text": "queued log media tweet",
        "date": "2026-05-17T00:00:00+00:00",
        "url": "https://x.com/artist/status/retry-log",
        "media": ["https://example.test/retry-log.jpg"],
        "videos": [],
    }
    twitter_monitor = DummyTwitterMonitor(
        tweets=[],
        media_paths={"retry-log": ["images/artist/retry-log.jpg"]},
    )
    processor = make_processor(
        db_manager,
        twitter_monitor=twitter_monitor,
        backup_manager=DummyBackupManager(),
        hydrus_client=DummyHydrusClient(),
        status_notifier=DummyStatusNotifier(),
    )

    await processor._process_log_only_account(
        {
            "username": "artist",
            "display_name": "Artist",
            "custom_tags": [],
            "rank": 1,
        }
    )

    assert [tweet["id"] for tweet in db_manager.saved_log_only_tweets] == ["retry-log"]
    assert db_manager.saved_log_only_tweets[0]["local_media"] == [
        "images/artist/retry-log.jpg"
    ]
    assert db_manager.twitter_retry["log_only"] == {}
    assert twitter_monitor.gallery_dl_extractor.calls == [("artist", ["retry-log"])]


@pytest.mark.asyncio
async def test_resume_pending_event_checks_marks_checked_and_saves_events():
    db_manager = DummyDbManager()
    db_manager.pending_event_tweets = [
        {
            "id": "tweet-1",
            "username": "artist",
            "display_name": "Artist",
            "text": "コミケ出ます",
            "date": "2026-03-11T12:00:00+00:00",
            "url": "https://x.com/artist/status/1",
            "media": [],
            "local_media": [],
            "huggingface_urls": [],
            "sensitive": False,
        }
    ]
    detector = DummyEventDetector()
    processor = make_processor(db_manager, event_detector=detector)

    await processor.resume_pending_twitter_work()

    assert detector.calls == [["tweet-1"]]
    assert db_manager.checked_ids == [["tweet-1"]]
    assert db_manager.saved_event_batches[0][0] == "artist"
    assert db_manager.saved_event_batches[0][1][0]["id"] == "tweet-1"


@pytest.mark.asyncio
async def test_resume_pending_event_checks_marks_stale_backlog_without_llm():
    db_manager = DummyDbManager()
    db_manager.pending_event_tweets = [
        {
            "id": "tweet-old",
            "username": "artist",
            "display_name": "Artist",
            "text": "去年のコミケ参加告知",
            "date": (datetime.utcnow() - timedelta(days=30)).isoformat(),
            "url": "https://x.com/artist/status/old",
            "media": [],
            "local_media": [],
            "huggingface_urls": [],
            "sensitive": False,
        },
        {
            "id": "tweet-recent",
            "username": "artist",
            "display_name": "Artist",
            "text": "コミケ出ます",
            "date": (datetime.utcnow() - timedelta(days=1)).isoformat(),
            "url": "https://x.com/artist/status/recent",
            "media": [],
            "local_media": [],
            "huggingface_urls": [],
            "sensitive": False,
        },
    ]
    detector = DummyEventDetector()
    processor = make_processor(
        db_manager,
        event_detector=detector,
        pending_event_max_age_days=7,
    )

    await processor.resume_pending_twitter_work()

    assert db_manager.last_pending_since_date is not None
    assert db_manager.stale_checked_ids == ["tweet-old"]
    assert detector.calls == [["tweet-recent"]]
    assert db_manager.checked_ids == [["tweet-recent"]]
    assert db_manager.saved_event_batches[0][1][0]["id"] == "tweet-recent"


@pytest.mark.asyncio
async def test_background_event_detection_saves_and_notifies_events():
    db_manager = DummyDbManager()
    db_manager.pending_event_tweets = [
        {
            "id": "tweet-bg",
            "username": "artist",
            "display_name": "Artist",
            "text": "コミケ出ます",
            "date": "2026-03-11T12:00:00+00:00",
            "url": "https://x.com/artist/status/bg",
            "media": [],
            "local_media": [],
            "huggingface_urls": [],
            "sensitive": False,
        }
    ]
    detector = DummyEventDetector()
    notifier = DummyDiscordNotifier()
    processor = make_processor(
        db_manager,
        event_detector=detector,
        discord_notifier=notifier,
        hydrus_client=DummyHydrusClient(event_tweets_only=True),
    )

    processor.schedule_pending_event_detection("test")
    await processor.wait_for_pending_event_detection()

    assert detector.calls == [["tweet-bg"]]
    assert db_manager.checked_ids == [["tweet-bg"]]
    assert db_manager.saved_event_batches[0][1][0]["id"] == "tweet-bg"
    assert notifier.calls == [("tweet-bg", "artist", "Artist")]
    assert db_manager.notified_ids == ["tweet-bg"]


@pytest.mark.asyncio
async def test_resume_unnotified_events_marks_notified_after_send():
    db_manager = DummyDbManager()
    db_manager.unnotified_events = [
        types.SimpleNamespace(
            id="event-1",
            username="artist",
            display_name="Artist",
            tweet_text="コミティア参加します",
            tweet_date=types.SimpleNamespace(isoformat=lambda: "2026-03-11T12:00:00+00:00"),
            tweet_url="https://x.com/artist/status/2",
            media_urls='["https://example.com/a.jpg"]',
            local_media='[]',
            sensitive=False,
            analysis_result='{"event_type":"comitia","confidence":0.95}',
            event_type="comitia",
            event_date="2026-05-05",
            participation_type="一般参加",
            confidence_score="0.95",
            space_number="A-01",
            circle_name="Artist Circle",
        )
    ]
    notifier = DummyDiscordNotifier()
    processor = make_processor(
        db_manager,
        event_detector=DummyEventDetector(),
        discord_notifier=notifier,
        hydrus_client=DummyHydrusClient(event_tweets_only=True),
    )

    await processor.resume_pending_twitter_work()

    assert notifier.calls == [("event-1", "artist", "Artist")]
    assert db_manager.notified_ids == ["event-1"]


@pytest.mark.asyncio
async def test_resume_unnotified_events_skips_stale_backlog_by_default():
    db_manager = DummyDbManager()
    db_manager.unnotified_events = [
        types.SimpleNamespace(
            id="event-old",
            username="artist",
            display_name="Artist",
            tweet_text="old event",
            tweet_date=datetime.utcnow() - timedelta(days=30),
            tweet_url="https://x.com/artist/status/old",
            media_urls="[]",
            local_media="[]",
            sensitive=False,
            analysis_result="{}",
            event_type=None,
            event_date=None,
            participation_type=None,
            confidence_score=None,
            space_number=None,
            circle_name=None,
        ),
        types.SimpleNamespace(
            id="event-recent",
            username="artist",
            display_name="Artist",
            tweet_text="recent event",
            tweet_date=datetime.utcnow() - timedelta(days=1),
            tweet_url="https://x.com/artist/status/recent",
            media_urls="[]",
            local_media="[]",
            sensitive=False,
            analysis_result="{}",
            event_type=None,
            event_date=None,
            participation_type=None,
            confidence_score=None,
            space_number=None,
            circle_name=None,
        ),
    ]
    notifier = DummyDiscordNotifier()
    processor = make_processor(
        db_manager,
        event_detector=DummyEventDetector(),
        discord_notifier=notifier,
        hydrus_client=DummyHydrusClient(event_tweets_only=True),
    )

    await processor.resume_pending_twitter_work()

    assert db_manager.last_unnotified_since_date is not None
    assert notifier.calls == [("event-recent", "artist", "Artist")]
    assert db_manager.notified_ids == ["event-recent"]
