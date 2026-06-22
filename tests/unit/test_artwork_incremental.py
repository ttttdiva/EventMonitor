import asyncio
import threading

from src.services.account_processor import AccountProcessor


class DummyExtractor:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def fetch_user_works(self, identifier, limit=None):
        self.calls.append((identifier, limit))
        key = "full" if limit is None else limit
        return self.responses.get(key, [])

    def download_media_for_works(self, identifier, work_ids, **kwargs):
        self.calls.append(("download", identifier, list(work_ids), kwargs))
        return {work_id: [f"data/media/{work_id}.jpg"] for work_id in work_ids}


class DummyDbManager:
    def __init__(self, has_posts, existing_ids=None, latest_id=None):
        self._has_posts = has_posts
        self._existing_ids = set(existing_ids or [])
        self._latest_id = latest_id

    def has_any_posts(self, identifier, platform):
        return self._has_posts

    def get_existing_post_ids(self, identifier, platform):
        return set(self._existing_ids)

    def get_latest_post_id(self, identifier, platform):
        return self._latest_id


class DummyStatusNotifier:
    def notify_running(self, **kwargs):
        return None

    def add_new_tweets(self, count):
        return None

    def increment_processed_accounts(self):
        return None

    def notify_error(self, *args, **kwargs):
        return None


class DummyHydrusClient:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.import_settings = {"event_tweets_only": False}

    async def import_kemono_images(self, work, local_media):
        return list(local_media)

    async def import_pixiv_images(self, work, local_media):
        return list(local_media)


def make_processor(db_manager, **kwargs):
    return AccountProcessor(
        config={},
        db_manager=db_manager,
        event_detector=None,
        twitter_monitor=None,
        discord_notifier=None,
        backup_manager=None,
        hydrus_client=kwargs.get("hydrus_client"),
        status_notifier=kwargs.get("status_notifier"),
        pixiv_extractor=kwargs.get("pixiv_extractor"),
        kemono_extractor=kwargs.get("kemono_extractor"),
    )


def test_fetch_incremental_artworks_runs_full_crawl_without_baseline():
    extractor = DummyExtractor({
        "full": [{"id": "3"}, {"id": "2"}, {"id": "1"}],
    })
    processor = make_processor(DummyDbManager(has_posts=False))

    works = asyncio.run(processor._fetch_incremental_artworks("pixiv", "user1", extractor))

    assert works == [{"id": "3"}, {"id": "2"}, {"id": "1"}]
    assert extractor.calls == [("user1", None)]


def test_fetch_incremental_artworks_stops_when_known_id_seen():
    extractor = DummyExtractor({
        20: [{"id": "30"}, {"id": "29"}, {"id": "28"}, {"id": "10"}],
        "full": [],
    })
    processor = make_processor(
        DummyDbManager(has_posts=True, existing_ids={"10", "9"}, latest_id="10")
    )

    works = asyncio.run(processor._fetch_incremental_artworks("pixiv", "user1", extractor))

    assert [work["id"] for work in works] == ["30", "29", "28", "10"]
    assert extractor.calls == [("user1", 20)]


def test_fetch_incremental_artworks_expands_limit_before_stopping():
    extractor = DummyExtractor({
        20: [{"id": f"{200 - i}"} for i in range(20)],
        50: [{"id": f"{200 - i}"} for i in range(40)] + [{"id": "known"}],
        "full": [],
    })
    processor = make_processor(
        DummyDbManager(has_posts=True, existing_ids={"known"}, latest_id="known")
    )

    works = asyncio.run(processor._fetch_incremental_artworks("pixiv", "user1", extractor))

    assert works[-1]["id"] == "known"
    assert extractor.calls == [("user1", 20), ("user1", 50)]


class DummyArtworkDbManager(DummyDbManager):
    def __init__(self):
        super().__init__(has_posts=False)
        self.saved_monitor = []
        self.saved_log_only = []
        self.retry_queue = {}
        self.hydrus_updates = []

    def filter_new_kemono_works(self, works, identifier):
        return list(works)

    def filter_new_pixiv_works(self, works, identifier):
        return list(works)

    def filter_kemono_log_only_works(self, works, identifier):
        return list(works)

    def filter_pixiv_log_only_works(self, works, identifier):
        return list(works)

    def save_kemono_works(self, works, identifier):
        self.saved_monitor.append((identifier, [work["id"] for work in works]))
        return len(works)

    def save_pixiv_works(self, works, identifier):
        self.saved_monitor.append((identifier, [work["id"] for work in works]))
        return len(works)

    def save_single_kemono_log_only_work(self, work, identifier):
        self.saved_log_only.append((identifier, work["id"]))
        return True

    def save_single_pixiv_log_only_work(self, work, identifier):
        self.saved_log_only.append((identifier, work["id"]))
        return True

    def get_artwork_retry_works(self, platform, identifier, is_log_only=False):
        scope = "log_only" if is_log_only else "monitor"
        return [
            entry["payload"]
            for entry in self.retry_queue.get((platform, identifier), {}).get(scope, {}).values()
        ]

    def upsert_artwork_retry(self, platform, identifier, work, is_log_only=False, error=None):
        scope = "log_only" if is_log_only else "monitor"
        queue = self.retry_queue.setdefault((platform, identifier), {"monitor": {}, "log_only": {}})
        entry = queue[scope].get(work["id"], {"retry_count": 0})
        queue[scope][work["id"]] = {
            "payload": dict(work),
            "retry_count": entry["retry_count"] + 1,
            "last_error": error,
        }

    def clear_artwork_retry(self, platform, identifier, work_id, is_log_only=False):
        scope = "log_only" if is_log_only else "monitor"
        queue = self.retry_queue.get((platform, identifier))
        if not queue:
            return
        queue[scope].pop(work_id, None)

    def estimate_hydrus_expected_count(self, local_media):
        return len(local_media)

    def update_kemono_hydrus_import_status(self, work_id, imported_count, expected_count=None):
        self.hydrus_updates.append((work_id, imported_count, expected_count))

    def update_pixiv_hydrus_import_status(self, work_id, imported_count, expected_count=None):
        self.hydrus_updates.append((work_id, imported_count, expected_count))


def test_kemono_monitor_account_downloads_and_saves_sequentially():
    history = []

    class SequentialKemonoExtractor(DummyExtractor):
        def fetch_user_works(self, identifier, limit=None):
            return [
                {"id": "fanbox_2", "media": ["b"], "media_hashes": {"b": "hash-b"}, "file_count": 1, "date": "2024-01-02"},
                {"id": "fanbox_1", "media": ["a"], "media_hashes": {"a": "hash-a"}, "file_count": 1, "date": "2024-01-01"},
            ]

        def download_media_for_works(self, identifier, work_ids, **kwargs):
            history.append(("download", work_ids[0]))
            return {work_ids[0]: [f"data/media/{work_ids[0]}.jpg"]}

    class TrackingDbManager(DummyArtworkDbManager):
        def save_kemono_works(self, works, identifier):
            history.append(("save", works[0]["id"]))
            return super().save_kemono_works(works, identifier)

    db_manager = TrackingDbManager()
    processor = make_processor(
        db_manager,
        hydrus_client=DummyHydrusClient(enabled=False),
        status_notifier=DummyStatusNotifier(),
        kemono_extractor=SequentialKemonoExtractor(),
    )

    asyncio.run(
        processor._process_artwork_monitor_account(
            {"username": "fanbox/123", "display_name": "cedar"},
            "kemono",
        )
    )

    assert history == [
        ("download", "fanbox_1"),
        ("save", "fanbox_1"),
        ("download", "fanbox_2"),
        ("save", "fanbox_2"),
    ]


def test_pixiv_log_only_account_downloads_and_saves_sequentially():
    history = []

    class SequentialPixivExtractor(DummyExtractor):
        def fetch_user_works(self, identifier, limit=None):
            return [
                {"id": "102", "media": ["b"], "file_count": 1, "date": "2024-01-02"},
                {"id": "101", "media": ["a"], "file_count": 1, "date": "2024-01-01"},
            ]

        def download_media_for_works(self, identifier, work_ids, **kwargs):
            history.append(("download", work_ids[0]))
            return {work_ids[0]: [f"data/media/{work_ids[0]}.jpg"]}

    class TrackingDbManager(DummyArtworkDbManager):
        def save_single_pixiv_log_only_work(self, work, identifier):
            history.append(("save", work["id"]))
            return super().save_single_pixiv_log_only_work(work, identifier)

    db_manager = TrackingDbManager()
    processor = make_processor(
        db_manager,
        hydrus_client=DummyHydrusClient(enabled=False),
        status_notifier=DummyStatusNotifier(),
        pixiv_extractor=SequentialPixivExtractor(),
    )

    asyncio.run(
        processor._process_artwork_log_only_account(
            {"username": "12345", "display_name": "pixiv-user"},
            "pixiv",
        )
    )

    assert history == [
        ("download", "101"),
        ("save", "101"),
        ("download", "102"),
        ("save", "102"),
    ]


def test_failed_artwork_is_retried_from_queue_on_next_run():
    class RetryExtractor(DummyExtractor):
        def __init__(self):
            super().__init__({})
            self.fetch_responses = [
                [{"id": "fanbox_1", "media": ["a"], "file_count": 1, "date": "2024-01-01"}],
                [],
            ]
            self.download_responses = [
                {},
                {"fanbox_1": ["data/media/fanbox_1.jpg"]},
            ]

        def fetch_user_works(self, identifier, limit=None):
            self.calls.append((identifier, limit))
            return self.fetch_responses.pop(0)

        def download_media_for_works(self, identifier, work_ids, **kwargs):
            self.calls.append(("download", identifier, list(work_ids), kwargs))
            return self.download_responses.pop(0)

    db_manager = DummyArtworkDbManager()
    extractor = RetryExtractor()
    processor = make_processor(
        db_manager,
        hydrus_client=DummyHydrusClient(enabled=False),
        status_notifier=DummyStatusNotifier(),
        kemono_extractor=extractor,
    )
    account = {"username": "fanbox/123", "display_name": "cedar"}

    asyncio.run(
        processor._process_artwork_monitor_account(
            account,
            "kemono",
        )
    )

    queued = db_manager.retry_queue[("kemono", "fanbox/123")]["monitor"]
    assert "fanbox_1" in queued
    assert queued["fanbox_1"]["retry_count"] == 1
    assert queued["fanbox_1"]["last_error"] == "download_incomplete"
    assert db_manager.saved_monitor == []

    asyncio.run(
        processor._process_artwork_monitor_account(
            account,
            "kemono",
        )
    )

    assert db_manager.saved_monitor == [("fanbox/123", ["fanbox_1"])]
    assert db_manager.retry_queue[("kemono", "fanbox/123")]["monitor"] == {}


def test_pending_hydrus_retry_uses_artwork_platform_specs(tmp_path):
    media_file = tmp_path / "skeb.jpg"
    media_file.write_bytes(b"image")
    bluesky_file = tmp_path / "bluesky.jpg"
    bluesky_file.write_bytes(b"image")

    class PendingDbManager(DummyDbManager):
        def __init__(self):
            super().__init__(has_posts=False)
            self.pending_calls = []
            self.updates = []
            self.pending = {
                "skeb": [{"id": "skeb-1", "local_media": [str(media_file)]}],
                "bluesky": [{"id": "bsky-1", "local_media": [str(bluesky_file)]}],
            }

        def get_pending_hydrus_works(self, platform):
            self.pending_calls.append(platform)
            return list(self.pending.get(platform, []))

        def estimate_hydrus_expected_count(self, local_media):
            return len(local_media)

        def update_skeb_hydrus_import_status(
            self, work_id, imported_count, expected_count=None, force=False
        ):
            self.updates.append(("skeb", work_id, imported_count, expected_count, force))

        def update_bluesky_hydrus_import_status(
            self, work_id, imported_count, expected_count=None, force=False
        ):
            self.updates.append(("bluesky", work_id, imported_count, expected_count, force))

    class PendingHydrusClient(DummyHydrusClient):
        def __init__(self):
            super().__init__(enabled=True)
            self.imports = []

        async def import_skeb_images(self, work, local_media):
            self.imports.append(("skeb", work["id"], list(local_media)))
            return [(local_media[0], "hash-skeb")]

        async def import_bluesky_images(self, work, local_media):
            self.imports.append(("bluesky", work["id"], list(local_media)))
            return [(local_media[0], "hash-bluesky")]

    db_manager = PendingDbManager()
    hydrus_client = PendingHydrusClient()
    processor = make_processor(
        db_manager,
        hydrus_client=hydrus_client,
        status_notifier=DummyStatusNotifier(),
    )

    asyncio.run(processor.retry_pending_hydrus_imports())

    assert set(db_manager.pending_calls) == set(AccountProcessor.ARTWORK_PLATFORM_SPECS)
    assert hydrus_client.imports == [
        ("skeb", "skeb-1", [str(media_file)]),
        ("bluesky", "bsky-1", [str(bluesky_file)]),
    ]
    assert db_manager.updates == [
        ("skeb", "skeb-1", 1, 1, True),
        ("bluesky", "bsky-1", 1, 1, True),
    ]
