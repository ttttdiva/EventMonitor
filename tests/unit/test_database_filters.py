import json
from datetime import datetime, timedelta

import pytest

from src.database import (
    AllTweets,
    DatabaseManager,
    LogOnlyTweet,
    PixivLogOnlyWork,
    PixivWork,
    SkebWork,
)


@pytest.fixture
def sqlite_config(tmp_path):
    db_path = tmp_path / "eventmonitor.db"
    return {
        "database": {
            "type": "sqlite",
            "path": str(db_path),
        }
    }


@pytest.fixture
def db_manager(sqlite_config):
    return DatabaseManager(sqlite_config)


def _seed_all_tweets(session, tweet_ids):
    now = datetime.utcnow()
    for idx, tweet_id in enumerate(tweet_ids):
        session.add(
            AllTweets(
                id=tweet_id,
                username=f"user{idx}",
                display_name=f"User {idx}",
                tweet_text="seed",
                tweet_date=now - timedelta(minutes=idx),
                tweet_url=f"https://x.com/user{idx}/status/{tweet_id}",
                media_urls=json.dumps([]),
                local_media=json.dumps([]),
                huggingface_urls=json.dumps([]),
            )
        )
    session.commit()


def _seed_log_only_tweets(session, tweet_ids):
    now = datetime.utcnow()
    for idx, tweet_id in enumerate(tweet_ids):
        session.add(
            LogOnlyTweet(
                id=tweet_id,
                username=f"logger{idx}",
                display_name=f"Logger {idx}",
                tweet_text="seed",
                tweet_date=now - timedelta(minutes=idx),
                tweet_url=f"https://x.com/logger{idx}/status/{tweet_id}",
                media_urls=json.dumps([]),
                huggingface_urls=json.dumps([]),
            )
        )
    session.commit()


def test_filter_new_tweets_returns_only_unknown_ids(db_manager):
    session = db_manager.Session()
    _seed_all_tweets(session, ["1", "2"])

    incoming = [
        {"id": "1", "text": "old"},
        {"id": "3", "text": "new"},
        {"id": "", "text": "missing"},
        {"text": "no-id"},
    ]

    filtered = db_manager.filter_new_tweets(incoming, "tester")
    ids = [tweet.get("id") for tweet in filtered]

    assert "1" not in ids
    assert "3" in ids
    # entries without IDs pass through so they can be inspected upstream
    assert None in ids


def test_filter_new_tweets_handles_large_batches(db_manager):
    session = db_manager.Session()
    existing_ids = [f"seed_{i}" for i in range(950)]
    _seed_all_tweets(session, existing_ids)

    incoming = [{"id": tweet_id} for tweet_id in existing_ids]
    incoming.extend({"id": f"fresh_{i}"} for i in range(5))

    filtered = db_manager.filter_new_tweets(incoming, "tester")

    assert {tweet["id"] for tweet in filtered if tweet.get("id") and tweet["id"].startswith("fresh_")} == {
        "fresh_0",
        "fresh_1",
        "fresh_2",
        "fresh_3",
        "fresh_4",
    }


def test_filter_log_only_tweets(db_manager):
    session = db_manager.Session()
    _seed_log_only_tweets(session, ["log_1"])

    incoming = [
        {"id": "log_1", "text": "old"},
        {"id": "log_2", "text": "new"},
    ]

    filtered = db_manager.filter_log_only_tweets(incoming, "logger")

    assert [tweet["id"] for tweet in filtered] == ["log_2"]


def test_get_existing_and_latest_post_ids_for_artwork_platform(db_manager):
    session = db_manager.Session()
    now = datetime.utcnow()
    session.add(
        PixivWork(
            id="pixiv_old",
            user_id="123",
            display_name="User",
            title="old",
            work_date=now - timedelta(days=1),
            work_url="https://www.pixiv.net/artworks/pixiv_old",
            media_urls=json.dumps([]),
            local_media=json.dumps([]),
            huggingface_urls=json.dumps([]),
        )
    )
    session.add(
        PixivLogOnlyWork(
            id="pixiv_new",
            user_id="123",
            display_name="User",
            title="new",
            work_date=now,
            work_url="https://www.pixiv.net/artworks/pixiv_new",
            media_urls=json.dumps([]),
            huggingface_urls=json.dumps([]),
        )
    )
    session.commit()

    assert db_manager.get_existing_post_ids("123", "pixiv") == {"pixiv_old", "pixiv_new"}
    assert db_manager.get_latest_post_id("123", "pixiv") == "pixiv_new"


def test_artwork_save_uses_common_date_fallback_and_pending_hydrus(db_manager):
    saved = db_manager.save_skeb_works(
        [
            {
                "id": "skeb_1",
                "date": "",
                "text": "commission",
                "media": ["https://example.test/skeb_1.jpg"],
                "local_media": ["images/skeb/skeb_1.jpg"],
                "tags": ["skeb-tag"],
                "sensitive": True,
            }
        ],
        "artist",
    )

    assert saved == 1

    session = db_manager.Session()
    try:
        record = session.query(SkebWork).filter(SkebWork.id == "skeb_1").one()
        assert record.work_date is not None
        assert record.hydrus_expected_count == 1
        assert json.loads(record.tags) == ["skeb-tag"]
    finally:
        session.close()

    pending = db_manager.get_pending_hydrus_works("skeb")

    assert pending[0]["id"] == "skeb_1"
    assert pending[0]["username"] == "artist"
    assert pending[0]["local_media"] == ["images/skeb/skeb_1.jpg"]
    assert pending[0]["sensitive"] is True


def test_artwork_filter_and_save_refreshes_media_increased_records(db_manager):
    db_manager.save_skeb_works(
        [
            {
                "id": "skeb_2",
                "date": "2024-01-01T00:00:00+00:00",
                "text": "old",
                "media": [],
                "local_media": [],
            }
        ],
        "artist",
    )

    incoming = [
        {
            "id": "skeb_2",
            "date": "2024-01-01T00:00:00+00:00",
            "text": "new",
            "media": ["https://example.test/a.jpg", "https://example.test/b.jpg"],
            "local_media": ["images/skeb/a.jpg", "images/skeb/b.jpg"],
        }
    ]

    assert db_manager.filter_new_skeb_works(incoming, "artist") == incoming
    assert db_manager.save_skeb_works(incoming, "artist") == 1

    session = db_manager.Session()
    try:
        record = session.query(SkebWork).filter(SkebWork.id == "skeb_2").one()
        assert json.loads(record.media_urls) == [
            "https://example.test/a.jpg",
            "https://example.test/b.jpg",
        ]
        assert json.loads(record.local_media) == ["images/skeb/a.jpg", "images/skeb/b.jpg"]
        assert record.hydrus_expected_count == 2
        assert record.hydrus_imported_count == 0
    finally:
        session.close()


def test_twitter_retry_queue_survives_latest_progress_and_clears_saved(db_manager):
    retry_tweet = {
        "id": "retry-1",
        "username": "artist",
        "display_name": "Artist",
        "text": "needs media",
        "date": "2026-05-17T00:00:00+00:00",
        "url": "https://x.com/artist/status/retry-1",
        "media": ["https://example.test/retry.jpg"],
        "videos": [],
        "local_media": [],
    }

    db_manager.upsert_twitter_retry(
        "artist",
        retry_tweet,
        error="download_incomplete",
    )

    queued = db_manager.get_twitter_retry_tweets("artist")
    assert [tweet["id"] for tweet in queued] == ["retry-1"]

    saved = db_manager.save_all_tweets(
        [
            {
                **retry_tweet,
                "local_media": ["images/artist/retry.jpg"],
            }
        ],
        "artist",
    )
    assert saved == 1

    assert db_manager.get_twitter_retry_tweets("artist") == []


def test_twitter_retry_queue_keeps_log_only_separate(db_manager):
    monitor_tweet = {
        "id": "monitor-retry",
        "text": "monitor",
        "date": "2026-05-17T00:00:00+00:00",
        "url": "https://x.com/artist/status/monitor-retry",
        "media": ["https://example.test/monitor.jpg"],
        "videos": [],
    }
    log_tweet = {
        "id": "log-retry",
        "text": "log",
        "date": "2026-05-17T00:00:00+00:00",
        "url": "https://x.com/artist/status/log-retry",
        "media": ["https://example.test/log.jpg"],
        "videos": [],
    }

    db_manager.upsert_twitter_retry("artist", monitor_tweet, error="download_incomplete")
    db_manager.upsert_twitter_retry(
        "artist",
        log_tweet,
        is_log_only=True,
        error="download_incomplete",
    )

    assert [tweet["id"] for tweet in db_manager.get_twitter_retry_tweets("artist")] == [
        "monitor-retry"
    ]
    assert [
        tweet["id"]
        for tweet in db_manager.get_twitter_retry_tweets("artist", is_log_only=True)
    ] == ["log-retry"]
