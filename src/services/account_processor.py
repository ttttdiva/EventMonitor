import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..path_utils import convert_paths_to_relative, get_media_base_paths, to_absolute_path
from ..utils import setup_logging


class AccountProcessor:
    """Handles per-account processing for EventMonitor."""

    ARTWORK_INCREMENTAL_LIMITS = (20, 50, 100, 200, 500)
    DEFAULT_UNNOTIFIED_RESUME_MAX_AGE_DAYS = 7
    DEFAULT_PENDING_EVENT_MAX_AGE_DAYS = 7

    ARTWORK_PLATFORM_SPECS = {
        "pixiv": {
            "extractor_attr": "pixiv_extractor",
            "filter_monitor": "filter_new_pixiv_works",
            "filter_log_only": "filter_pixiv_log_only_works",
            "save_monitor": "save_pixiv_works",
            "save_log_only": "save_single_pixiv_log_only_work",
            "hydrus_import": "import_pixiv_images",
            "hydrus_update": "update_pixiv_hydrus_import_status",
            "unreachable": "_record_unreachable_pixiv",
        },
        "kemono": {
            "extractor_attr": "kemono_extractor",
            "filter_monitor": "filter_new_kemono_works",
            "filter_log_only": "filter_kemono_log_only_works",
            "save_monitor": "save_kemono_works",
            "save_log_only": "save_single_kemono_log_only_work",
            "hydrus_import": "import_kemono_images",
            "hydrus_update": "update_kemono_hydrus_import_status",
            "unreachable": "_record_unreachable_kemono",
        },
        "tinami": {
            "extractor_attr": "tinami_extractor",
            "filter_monitor": "filter_new_tinami_works",
            "filter_log_only": "filter_tinami_log_only_works",
            "save_monitor": "save_tinami_works",
            "save_log_only": "save_single_tinami_log_only_work",
            "hydrus_import": "import_tinami_images",
            "hydrus_update": "update_tinami_hydrus_import_status",
            "unreachable": "_record_unreachable_tinami",
        },
        "poipiku": {
            "extractor_attr": "poipiku_extractor",
            "filter_monitor": "filter_new_poipiku_works",
            "filter_log_only": "filter_poipiku_log_only_works",
            "save_monitor": "save_poipiku_works",
            "save_log_only": "save_single_poipiku_log_only_work",
            "hydrus_import": "import_poipiku_images",
            "hydrus_update": "update_poipiku_hydrus_import_status",
            "unreachable": "_record_unreachable_poipiku",
        },
        "fantia": {
            "extractor_attr": "fantia_extractor",
            "filter_monitor": "filter_new_fantia_works",
            "filter_log_only": "filter_fantia_log_only_works",
            "save_monitor": "save_fantia_works",
            "save_log_only": "save_single_fantia_log_only_work",
            "hydrus_import": "import_fantia_images",
            "hydrus_update": "update_fantia_hydrus_import_status",
            "unreachable": "_record_unreachable_fantia",
            "fetch_by_ids": "fetch_works_metadata_by_ids",
        },
        "nijie": {
            "extractor_attr": "nijie_extractor",
            "filter_monitor": "filter_new_nijie_works",
            "filter_log_only": "filter_nijie_log_only_works",
            "save_monitor": "save_nijie_works",
            "save_log_only": "save_single_nijie_log_only_work",
            "hydrus_import": "import_nijie_images",
            "hydrus_update": "update_nijie_hydrus_import_status",
            "unreachable": "_record_unreachable_nijie",
        },
        "skeb": {
            "extractor_attr": "skeb_extractor",
            "filter_monitor": "filter_new_skeb_works",
            "filter_log_only": "filter_skeb_log_only_works",
            "save_monitor": "save_skeb_works",
            "save_log_only": "save_single_skeb_log_only_work",
            "hydrus_import": "import_skeb_images",
            "hydrus_update": "update_skeb_hydrus_import_status",
            "unreachable": "_record_unreachable_skeb",
        },
        "bilibili": {
            "extractor_attr": "bilibili_extractor",
            "filter_monitor": "filter_new_bilibili_works",
            "filter_log_only": "filter_bilibili_log_only_works",
            "save_monitor": "save_bilibili_works",
            "save_log_only": "save_single_bilibili_log_only_work",
            "hydrus_import": "import_bilibili_images",
            "hydrus_update": "update_bilibili_hydrus_import_status",
            "unreachable": "_record_unreachable_bilibili",
            "fetch_by_ids": "fetch_works_metadata_by_ids",
        },
        "misskey": {
            "extractor_attr": "misskey_extractor",
            "filter_monitor": "filter_new_misskey_works",
            "filter_log_only": "filter_misskey_log_only_works",
            "save_monitor": "save_misskey_works",
            "save_log_only": "save_single_misskey_log_only_work",
            "hydrus_import": "import_misskey_images",
            "hydrus_update": "update_misskey_hydrus_import_status",
            "unreachable": "_record_unreachable_misskey",
        },
        "gelbooru": {
            "extractor_attr": "gelbooru_extractor",
            "filter_monitor": "filter_new_gelbooru_works",
            "filter_log_only": "filter_gelbooru_log_only_works",
            "save_monitor": "save_gelbooru_works",
            "save_log_only": "save_single_gelbooru_log_only_work",
            "hydrus_import": "import_gelbooru_images",
            "hydrus_update": "update_gelbooru_hydrus_import_status",
            "unreachable": "_record_unreachable_gelbooru",
        },
        "fanbox": {
            "extractor_attr": "fanbox_extractor",
            "filter_monitor": "filter_new_fanbox_works",
            "filter_log_only": "filter_fanbox_log_only_works",
            "save_monitor": "save_fanbox_works",
            "save_log_only": "save_single_fanbox_log_only_work",
            "hydrus_import": "import_fanbox_images",
            "hydrus_update": "update_fanbox_hydrus_import_status",
            "unreachable": "_record_unreachable_fanbox",
            "fetch_by_ids": "fetch_works_metadata_by_ids",
        },
        "bluesky": {
            "extractor_attr": "bluesky_extractor",
            "filter_monitor": "filter_new_bluesky_works",
            "filter_log_only": "filter_bluesky_log_only_works",
            "save_monitor": "save_bluesky_works",
            "save_log_only": "save_single_bluesky_log_only_work",
            "hydrus_import": "import_bluesky_images",
            "hydrus_update": "update_bluesky_hydrus_import_status",
            "unreachable": "_record_unreachable_bluesky",
        },
        "privatter": {
            "extractor_attr": "privatter_extractor",
            "filter_monitor": "filter_new_privatter_works",
            "filter_log_only": "filter_privatter_log_only_works",
            "save_monitor": "save_privatter_works",
            "save_log_only": "save_single_privatter_log_only_work",
            "hydrus_import": "import_privatter_images",
            "hydrus_update": "update_privatter_hydrus_import_status",
            "unreachable": "_record_unreachable_privatter",
        },
    }

    def __init__(
        self,
        config: dict,
        db_manager,
        event_detector,
        twitter_monitor,
        discord_notifier,
        backup_manager,
        hydrus_client,
        status_notifier,
        pixiv_extractor=None,
        kemono_extractor=None,
        tinami_extractor=None,
        poipiku_extractor=None,
        fantia_extractor=None,
        nijie_extractor=None,
        skeb_extractor=None,
        bilibili_extractor=None,
        misskey_extractor=None,
        gelbooru_extractor=None,
        fanbox_extractor=None,
        bluesky_extractor=None,
        privatter_extractor=None,
        discord_exporter=None,
        account_status_tracker=None,
        shutdown=None,
    ) -> None:
        self.config = config
        self.db_manager = db_manager
        self.event_detector = event_detector
        self.twitter_monitor = twitter_monitor
        self.discord_notifier = discord_notifier
        self.backup_manager = backup_manager
        self.hydrus_client = hydrus_client
        self.status_notifier = status_notifier
        self.pixiv_extractor = pixiv_extractor
        self.kemono_extractor = kemono_extractor
        self.tinami_extractor = tinami_extractor
        self.poipiku_extractor = poipiku_extractor
        self.fantia_extractor = fantia_extractor
        self.nijie_extractor = nijie_extractor
        self.skeb_extractor = skeb_extractor
        self.bilibili_extractor = bilibili_extractor
        self.misskey_extractor = misskey_extractor
        self.gelbooru_extractor = gelbooru_extractor
        self.fanbox_extractor = fanbox_extractor
        self.bluesky_extractor = bluesky_extractor
        self.privatter_extractor = privatter_extractor
        self.discord_exporter = discord_exporter
        self.account_status_tracker = account_status_tracker
        self.shutdown = shutdown
        self.logger = logging.getLogger("EventMonitor.AccountProcessor")
        self._event_detection_task: Optional[asyncio.Task] = None
        self._event_detection_requested = False

    @property
    def _shutdown_requested(self) -> bool:
        """シャットダウンが要求されているかチェック"""
        return self.shutdown is not None and self.shutdown.requested

    async def process_account(self, account: Dict[str, Any], semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            username = account["username"]
            platform = account.get("platform", "twitter")

            try:
                # フラグ済みアカウント: 軽量リチェックで復活/継続判定
                if self.account_status_tracker and self.account_status_tracker.is_flagged(username):
                    twitter_id = account.get("twitter_id") if platform in ("twitter", "") else None
                    reachable = await self._check_reachability(username, platform, twitter_id=twitter_id)
                    if reachable:
                        # ID変更（スクリーンネーム変更）の検出を確認
                        if platform in ("twitter", ""):
                            renames = self.twitter_monitor.get_and_clear_detected_renames()
                            rename_info = renames.get(username.lower())
                            if rename_info:
                                new_username = rename_info["new_username"]
                                new_display = rename_info["display_name"]
                                tid = rename_info["twitter_id"]
                                self.logger.warning(
                                    f"スクリーンネーム変更を検出: @{username} → @{new_username} "
                                    f"(twitter_id={tid})"
                                )
                                self._update_account_in_csv(
                                    username,
                                    {"username": new_username, "twitter_id": str(tid)},
                                )
                                account["username"] = new_username
                                account["twitter_id"] = tid
                                username = new_username

                        self.account_status_tracker.record_recovery(username)
                        self.account_status_tracker.save()
                        self.logger.info(
                            f"Previously flagged account recovered: {username} ({platform}), resuming processing"
                        )
                    else:
                        self.account_status_tracker.flag_account(
                            username=username,
                            platform=platform,
                            account_type=account.get("account_type", ""),
                            display_name=account.get("display_name", username),
                            error_msg="still unreachable (periodic recheck)",
                        )
                        self.account_status_tracker.save()
                        self.logger.info(
                            f"Flagged account still unreachable: {username} ({platform}), skipping"
                        )
                        return

                if platform in self.ARTWORK_PLATFORM_SPECS:
                    if account.get("account_type") == "log":
                        await self._process_artwork_log_only_account(account, platform)
                    else:
                        await self._process_artwork_monitor_account(account, platform)
                elif platform == "discord":
                    await self._process_discord_account(account)
                elif account.get("account_type") == "log":
                    await self._process_log_only_account(account)
                else:
                    await self._process_monitor_account(account)
            finally:
                if platform == "discord":
                    self.status_notifier.increment_completed_discord_servers()
                else:
                    self.status_notifier.increment_completed_accounts()

    async def _check_reachability(self, username: str, platform: str, twitter_id: Optional[int] = None) -> bool:
        """プラットフォーム別の軽量到達性チェック"""
        try:
            if platform == "pixiv":
                if not self.pixiv_extractor:
                    return False
                return await asyncio.to_thread(
                    self.pixiv_extractor.check_account_reachable, username
                )
            elif platform == "kemono":
                if not self.kemono_extractor:
                    return False
                return await asyncio.to_thread(
                    self.kemono_extractor.check_account_reachable, username
                )
            elif platform == "tinami":
                if not self.tinami_extractor:
                    return False
                return await asyncio.to_thread(
                    self.tinami_extractor.check_account_reachable, username
                )
            elif platform == "poipiku":
                if not self.poipiku_extractor:
                    return False
                return await asyncio.to_thread(
                    self.poipiku_extractor.check_account_reachable, username
                )
            elif platform == "fantia":
                if not self.fantia_extractor:
                    return False
                return await asyncio.to_thread(
                    self.fantia_extractor.check_account_reachable, username
                )
            elif platform == "nijie":
                if not self.nijie_extractor:
                    return False
                return await asyncio.to_thread(
                    self.nijie_extractor.check_account_reachable, username
                )
            elif platform == "skeb":
                if not self.skeb_extractor:
                    return False
                return await asyncio.to_thread(
                    self.skeb_extractor.check_account_reachable, username
                )
            elif platform == "bilibili":
                if not self.bilibili_extractor:
                    return False
                return await asyncio.to_thread(
                    self.bilibili_extractor.check_account_reachable, username
                )
            elif platform == "misskey":
                if not self.misskey_extractor:
                    return False
                return await asyncio.to_thread(
                    self.misskey_extractor.check_account_reachable, username
                )
            elif platform == "gelbooru":
                if not self.gelbooru_extractor:
                    return False
                return await asyncio.to_thread(
                    self.gelbooru_extractor.check_account_reachable, username
                )
            elif platform == "fanbox":
                if not self.fanbox_extractor:
                    return False
                return await asyncio.to_thread(
                    self.fanbox_extractor.check_account_reachable, username
                )
            elif platform == "bluesky":
                if not self.bluesky_extractor:
                    return False
                return await asyncio.to_thread(
                    self.bluesky_extractor.check_account_reachable, username
                )
            elif platform == "privatter":
                if not self.privatter_extractor:
                    return False
                return await asyncio.to_thread(
                    self.privatter_extractor.check_account_reachable, username
                )
            else:
                return await self.twitter_monitor.check_account_reachable(username, twitter_id=twitter_id)
        except Exception as e:
            self.logger.error(f"Reachability check error for {username} ({platform}): {e}")
            return True  # エラーは一時的として到達可能扱い

    async def _process_monitor_account(self, account: Dict[str, Any]) -> None:
        username = account["username"]
        display_name = account.get("display_name", username)

        try:
            # Notify start of account processing
            self.status_notifier.notify_running(current_account=username)
            type_display = "通常監視"
            self.logger.info(f"Checking account: {display_name} (@{username}) - Type: {type_display}")

            retry_tweets = self._get_twitter_retry_tweets(username)
            if retry_tweets:
                self.logger.info(f"Loaded {len(retry_tweets)} pending twitter retry tweets for @{username}")

            tweets, _gallery_event_tweets = await self.twitter_monitor.get_user_tweets_with_gallery_dl_first(
                username,
                days_lookback=self.config["tweet_settings"]["days_lookback"],
                event_detection_enabled=False,
            )

            if not tweets and not retry_tweets:
                self.logger.info(f"No tweets found for @{username}")
                self._record_unreachable_twitter(username, account)
                return

            for tweet in tweets:
                tweet["username"] = username
                tweet["display_name"] = display_name
                tweet["custom_tags"] = account.get("custom_tags", [])
                tweet["rank"] = account.get("rank", 3)
            for tweet in retry_tweets:
                tweet["username"] = username
                tweet["display_name"] = display_name
                tweet["custom_tags"] = account.get("custom_tags", [])
                tweet["rank"] = account.get("rank", 3)

            fresh_tweets = self._filter_new_tweets(tweets, username) if tweets else []
            retry_tweets = self._deduplicate_tweets(retry_tweets, username)
            retry_ids = {tweet.get("id") for tweet in retry_tweets if tweet.get("id")}
            fresh_tweets = [tweet for tweet in fresh_tweets if tweet.get("id") not in retry_ids]
            new_tweets = retry_tweets + self._deduplicate_tweets(fresh_tweets, username)
            if not new_tweets:
                self.logger.info(f"No new tweets for @{username} (all already in DB)")
                return

            new_tweets = self._deduplicate_tweets(new_tweets, username)
            
            # Notify new tweets count
            if new_tweets:
                self.logger.info(f"Found {len(new_tweets)} new tweets for @{username}")
                self.status_notifier.add_new_tweets(len(new_tweets))

            media_paths = await self._download_media(username, new_tweets)
            for tweet in new_tweets:
                tweet["local_media"] = media_paths.get(tweet["id"], [])

            valid_tweets: List[Dict[str, Any]] = []
            for tweet in new_tweets:
                if self._validate_tweet_download(username, tweet):
                    valid_tweets.append(tweet)
                else:
                    self._enqueue_twitter_retry(
                        username,
                        tweet,
                        error="download_incomplete",
                    )

            if len(valid_tweets) != len(new_tweets):
                skipped_count = len(new_tweets) - len(valid_tweets)
                self.logger.warning(
                    f"Queued {skipped_count} incomplete tweets for @{username} for retry"
                )

            if not valid_tweets:
                self.logger.info(f"No fully downloaded tweets remain for @{username}")
                return

            if self._shutdown_requested:
                self.logger.info(f"シャットダウン要求のため @{username} の保存前に処理を中断します")
                return

            new_tweets = valid_tweets

            saved_count = self.db_manager.save_all_tweets(new_tweets, username)
            self.logger.info(f"Saved {saved_count} tweets to all_tweets table for @{username}")
            if saved_count:
                for tweet in new_tweets:
                    self._clear_twitter_retry(username, tweet["id"])

            event_tweets: List[Dict[str, Any]] = []
            if saved_count:
                self.schedule_pending_event_detection(f"new tweets for @{username}")

            await self._run_backup_pipeline(username, new_tweets, event_tweets)

            # Increment processed accounts count
            self.status_notifier.increment_processed_accounts()

            # twitter_id が未保存なら取得してCSVに書き戻す
            self._try_save_twitter_id(username, account)

        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error(f"Error processing account @{username}: {exc}", exc_info=True)
            self.status_notifier.notify_error(str(exc), str(exc))
            self._record_unreachable_twitter(username, account)

    def _filter_new_tweets(self, tweets: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
        force_full_fetch = self.config['tweet_settings'].get('twscrape', {}).get('force_full_fetch', False)
        if force_full_fetch:
            self.logger.warning(
                f"Force full fetch enabled - processing ALL {len(tweets)} tweets without duplicate check"
            )
            return tweets
        return self.db_manager.filter_new_tweets(tweets, username)

    def _deduplicate_tweets(self, tweets: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen_ids = set()
        for tweet in tweets:
            tweet_id = tweet.get("id")
            if tweet_id in seen_ids:
                self.logger.debug(f"Duplicate tweet entry detected for @{username}: {tweet_id}")
                continue
            seen_ids.add(tweet_id)
            deduped.append(tweet)

        if len(deduped) != len(tweets):
            removed_count = len(tweets) - len(deduped)
            self.logger.warning(f"Removed {removed_count} duplicate tweet entries for @{username}")
        return deduped

    def _get_twitter_retry_tweets(
        self,
        username: str,
        *,
        is_log_only: bool = False,
    ) -> List[Dict[str, Any]]:
        getter = getattr(self.db_manager, "get_twitter_retry_tweets", None)
        if not callable(getter):
            return []
        return getter(username, is_log_only=is_log_only)

    def _enqueue_twitter_retry(
        self,
        username: str,
        tweet: Dict[str, Any],
        *,
        is_log_only: bool = False,
        error: Optional[str] = None,
    ) -> None:
        saver = getattr(self.db_manager, "upsert_twitter_retry", None)
        if not callable(saver):
            return
        saver(username, tweet, is_log_only=is_log_only, error=error)

    def _clear_twitter_retry(
        self,
        username: str,
        tweet_id: str,
        *,
        is_log_only: bool = False,
    ) -> None:
        clearer = getattr(self.db_manager, "clear_twitter_retry", None)
        if not callable(clearer):
            return
        clearer(username, tweet_id, is_log_only=is_log_only)

    async def _download_media(self, username: str, tweets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        tweets_with_media = [tweet for tweet in tweets if tweet.get("media") or tweet.get("videos")]
        tweet_ids_with_media = [tweet['id'] for tweet in tweets_with_media]

        if not tweet_ids_with_media:
            return {}

        is_private_account = self.twitter_monitor.is_account_private(username)
        media_paths = await asyncio.to_thread(
            self.twitter_monitor.gallery_dl_extractor.download_media_for_tweets,
            username,
            tweet_ids_with_media,
            move_to_images=True,
            is_private_account=is_private_account,
        )
        if not media_paths:
            return {}

        return {
            tweet_id: convert_paths_to_relative(paths, self.config)
            for tweet_id, paths in media_paths.items()
        }

    async def _detect_events(
        self,
        account: Dict[str, Any],
        new_tweets: List[Dict[str, Any]],
        gallery_event_tweets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        config_enabled = self.config['event_detection'].get('enabled', True)
        if not config_enabled or not account.get('event_detection_enabled', True):
            if not config_enabled:
                self.logger.info("Event detection is globally disabled (crawler mode only)")
            else:
                self.logger.info(f"Event detection is disabled for @{account['username']}, skipping LLM analysis")
            return []

        event_tweets: List[Dict[str, Any]] = []
        if gallery_event_tweets:
            new_ids = {tweet['id'] for tweet in new_tweets}
            gallery_event_in_new = [tweet for tweet in gallery_event_tweets if tweet['id'] in new_ids]
            event_tweets.extend(gallery_event_in_new)
            self.logger.info(
                f"Added {len(gallery_event_in_new)} gallery-dl event tweets for @{account['username']}"
            )

        remaining_tweets = [tweet for tweet in new_tweets if tweet.get('source') != 'gallery-dl']
        if remaining_tweets:
            self.logger.info(
                f"Running LLM event detection on {len(remaining_tweets)} twscrape tweets for @{account['username']}"
            )
            additional_event_tweets = await self.event_detector.detect_event_tweets(remaining_tweets)
            event_tweets.extend(additional_event_tweets)

        if not event_tweets:
            self.logger.info(f"No event-related tweets found for @{account['username']}")
            return []

        deduped_events, removed_count = self._deduplicate_event_tweets(event_tweets, account['username'])
        if removed_count:
            self.logger.warning(
                f"Removed {removed_count} duplicate event tweet entries for @{account['username']}"
            )
        return deduped_events

    def _deduplicate_event_tweets(
        self, event_tweets: List[Dict[str, Any]], username: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        deduped: List[Dict[str, Any]] = []
        seen_ids = set()
        for tweet in event_tweets:
            tweet_id = tweet.get('id')
            if tweet_id in seen_ids:
                self.logger.debug(f"Duplicate event tweet detected for @{username}: {tweet_id}")
                continue
            seen_ids.add(tweet_id)
            deduped.append(tweet)
        return deduped, len(event_tweets) - len(deduped)

    def _get_twitter_monitor_account(self, username: str) -> Dict[str, Any]:
        for account in self.config.get("monitored_accounts", []):
            if account.get("platform", "twitter") != "twitter":
                continue
            if account.get("account_type") == "log":
                continue
            if account.get("username") == username:
                return account
        return {
            "username": username,
            "display_name": username,
            "custom_tags": [],
            "rank": 3,
            "event_detection_enabled": True,
        }

    @staticmethod
    def _expected_twitter_media_count(tweet: Dict[str, Any]) -> int:
        return len(tweet.get("media", []) or []) + len(tweet.get("videos", []) or [])

    def _apply_twitter_account_metadata(
        self,
        tweet: Dict[str, Any],
        account: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not account:
            return tweet

        tweet.setdefault("username", account.get("username", tweet.get("username", "")))
        tweet.setdefault(
            "display_name",
            account.get("display_name", tweet.get("display_name", tweet.get("username", ""))),
        )
        tweet["custom_tags"] = account.get("custom_tags", tweet.get("custom_tags", []))
        tweet["rank"] = account.get("rank", tweet.get("rank", 3))
        return tweet

    def _validate_tweet_download(self, username: str, tweet: Dict[str, Any]) -> bool:
        expected = self._expected_twitter_media_count(tweet)
        if expected <= 0:
            return True

        actual = len(tweet.get("local_media", []) or [])
        if actual < expected:
            self.logger.warning(
                f"@{username} tweet {tweet.get('id')}: incomplete download ({actual}/{expected}), skipping DB save"
            )
            return False

        return True

    async def resume_pending_twitter_work(self) -> None:
        """前回中断で取りこぼした Twitter 処理を再開"""
        if not self.db_manager:
            return

        await self.wait_for_pending_event_detection()
        await self._resume_pending_event_checks(notify_and_import=True)
        await self._resume_unnotified_events()
        await self._resume_pending_twitter_hydrus_imports()

    def schedule_pending_event_detection(self, reason: str = "") -> None:
        """未判定ツイートのイベント判定をバックグラウンドで起動する"""
        if not self.db_manager:
            return

        self._event_detection_requested = True
        if self._event_detection_task and not self._event_detection_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.warning("No running event loop; pending event detection was not scheduled")
            return

        suffix = f" ({reason})" if reason else ""
        self.logger.info(f"Scheduling background event detection{suffix}")
        self._event_detection_task = loop.create_task(self._run_pending_event_detection_worker())
        self._event_detection_task.add_done_callback(self._log_event_detection_task_result)

    async def wait_for_pending_event_detection(self) -> None:
        """起動済みのバックグラウンドイベント判定があれば完了を待つ"""
        task = self._event_detection_task
        if task and not task.done():
            self.logger.info("Waiting for background event detection to finish")
            await task

    async def _run_pending_event_detection_worker(self) -> None:
        while not self._shutdown_requested:
            self._event_detection_requested = False
            processed = await self._resume_pending_event_checks(notify_and_import=True)
            if not processed and not self._event_detection_requested:
                break

    def _log_event_detection_task_result(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            self.logger.info("Background event detection was cancelled")
        except Exception as exc:
            self.logger.error(f"Background event detection failed: {exc}", exc_info=True)

    async def _resume_pending_event_checks(self, notify_and_import: bool = False) -> bool:
        max_age_days = self._get_pending_event_max_age_days()
        since_date = None
        if max_age_days is not None:
            since_date = datetime.utcnow() - timedelta(days=max_age_days)
            stale_count = self.db_manager.mark_stale_tweets_checked_for_event(since_date)
            if stale_count:
                self.logger.info(
                    f"Marked {stale_count} stale unchecked tweets older than "
                    f"{max_age_days} day(s) as checked without LLM analysis"
                )

        pending_tweets = self.db_manager.get_tweets_pending_event_check(since_date=since_date)
        if not pending_tweets:
            return False

        self.logger.info(f"Resuming event detection for {len(pending_tweets)} unchecked tweets")
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for tweet in pending_tweets:
            grouped.setdefault(tweet["username"], []).append(tweet)

        processed_any = False
        for username, tweets in grouped.items():
            if self._shutdown_requested:
                self.logger.info("シャットダウン要求のため未完了イベント判定の再開を中断します")
                return processed_any

            account = self._get_twitter_monitor_account(username)
            tweet_ids = [tweet["id"] for tweet in tweets]
            for tweet in tweets:
                self._apply_twitter_account_metadata(tweet, account)

            detection_enabled = (
                self.config.get("event_detection", {}).get("enabled", True)
                and account.get("event_detection_enabled", True)
                and self.event_detector is not None
                and getattr(self.event_detector, "enabled", True)
            )
            if not detection_enabled:
                updated = self.db_manager.mark_tweets_checked_for_event(tweet_ids)
                processed_any = processed_any or updated > 0
                self.logger.info(
                    f"Event detection disabled for @{username}; marked {updated} tweets as checked"
                )
                continue

            try:
                event_tweets = await self.event_detector.detect_event_tweets(tweets)
                if event_tweets:
                    if self.status_notifier:
                        self.status_notifier.add_event_tweets(len(event_tweets))
                    self.db_manager.save_event_tweets(event_tweets, username)
                updated = self.db_manager.mark_tweets_checked_for_event(tweet_ids)
                processed_any = processed_any or updated > 0
                self.logger.info(
                    f"Recovered event detection for @{username}: {len(event_tweets)} events, {updated} checked"
                )
                if notify_and_import and event_tweets and self.discord_notifier:
                    display_name = account.get("display_name", username)
                    await self._notify_and_import(event_tweets, username, display_name)
            except Exception as exc:
                self.logger.error(
                    f"Failed to resume event detection for @{username}: {exc}",
                    exc_info=True,
                )
        return processed_any

    def _get_pending_event_max_age_days(self) -> Optional[int]:
        raw_value = self.config.get("tweet_settings", {}).get("pending_event_max_age_days")
        if raw_value is None:
            return self.DEFAULT_PENDING_EVENT_MAX_AGE_DAYS

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            self.logger.warning(
                "Invalid tweet_settings.pending_event_max_age_days=%r; falling back to %s days",
                raw_value,
                self.DEFAULT_PENDING_EVENT_MAX_AGE_DAYS,
            )
            return self.DEFAULT_PENDING_EVENT_MAX_AGE_DAYS

        if value <= 0:
            return None

        return value

    def _build_event_retry_payload(self, event_tweet: Any) -> Dict[str, Any]:
        analysis_result: Dict[str, Any] = {}
        if getattr(event_tweet, "analysis_result", None):
            try:
                analysis_result = json.loads(event_tweet.analysis_result)
            except (TypeError, json.JSONDecodeError):
                analysis_result = {}

        analysis_result.setdefault("event_type", getattr(event_tweet, "event_type", None))
        analysis_result.setdefault("event_date", getattr(event_tweet, "event_date", None))
        analysis_result.setdefault("participation_type", getattr(event_tweet, "participation_type", None))
        if getattr(event_tweet, "confidence_score", None) is not None:
            try:
                analysis_result.setdefault("confidence", float(event_tweet.confidence_score))
            except (TypeError, ValueError):
                pass

        return {
            "id": event_tweet.id,
            "username": event_tweet.username,
            "display_name": event_tweet.display_name,
            "text": event_tweet.tweet_text,
            "date": event_tweet.tweet_date.isoformat(),
            "url": event_tweet.tweet_url,
            "media": json.loads(event_tweet.media_urls) if event_tweet.media_urls else [],
            "local_media": json.loads(event_tweet.local_media) if event_tweet.local_media else [],
            "sensitive": bool(getattr(event_tweet, "sensitive", False)),
            "event_analysis": analysis_result,
            "space_number": getattr(event_tweet, "space_number", None),
            "circle_name": getattr(event_tweet, "circle_name", None),
        }

    async def _resume_unnotified_events(self) -> None:
        if not self.discord_notifier:
            return

        max_age_days = self._get_unnotified_resume_max_age_days()
        since_date = None
        if max_age_days is not None:
            since_date = datetime.utcnow() - timedelta(days=max_age_days)

        pending_events = self.db_manager.get_unnotified_tweets(since_date=since_date)
        if not pending_events:
            return

        if max_age_days is None:
            self.logger.info(f"Resuming notification for {len(pending_events)} event tweets")
        else:
            self.logger.info(
                f"Resuming notification for {len(pending_events)} event tweets "
                f"from the last {max_age_days} day(s)"
            )
        event_only = bool(
            self.hydrus_client
            and self.hydrus_client.enabled
            and self.hydrus_client.import_settings.get("event_tweets_only", True)
        )

        for event_tweet in pending_events:
            if self._shutdown_requested:
                self.logger.info("シャットダウン要求のため未通知イベントの再送を中断します")
                return

            account = self._get_twitter_monitor_account(event_tweet.username)
            payload = self._build_event_retry_payload(event_tweet)
            self._apply_twitter_account_metadata(payload, account)

            try:
                await self.discord_notifier.send_notification(
                    payload,
                    payload["username"],
                    payload.get("display_name", payload["username"]),
                )
                if getattr(self.discord_notifier, "enabled", False):
                    self.db_manager.mark_as_notified(payload["id"])

                if event_only and self.hydrus_client.enabled and payload.get("local_media"):
                    imported = await self.hydrus_client.import_tweet_images(payload, payload["local_media"])
                    expected_count = self.db_manager.estimate_hydrus_expected_count(payload.get("local_media", []))
                    self.db_manager.update_hydrus_import_status(
                        tweet_id=payload["id"],
                        imported_count=len(imported),
                        expected_count=expected_count,
                    )
            except Exception as exc:
                self.logger.error(
                    f"Failed to resend notification for tweet {payload['id']}: {exc}",
                    exc_info=True,
                )

    def _get_unnotified_resume_max_age_days(self) -> Optional[int]:
        raw_value = self.config.get("tweet_settings", {}).get("resume_unnotified_max_age_days")
        if raw_value is None:
            return self.DEFAULT_UNNOTIFIED_RESUME_MAX_AGE_DAYS

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            self.logger.warning(
                "Invalid tweet_settings.resume_unnotified_max_age_days=%r; falling back to %s days",
                raw_value,
                self.DEFAULT_UNNOTIFIED_RESUME_MAX_AGE_DAYS,
            )
            return self.DEFAULT_UNNOTIFIED_RESUME_MAX_AGE_DAYS

        if value <= 0:
            return None

        return value

    async def _resume_pending_twitter_hydrus_imports(self) -> None:
        if not self.hydrus_client or not self.hydrus_client.enabled:
            return

        pending_tweets = self.db_manager.get_pending_hydrus_tweets()
        if not pending_tweets:
            return

        event_only = self.hydrus_client.import_settings.get("event_tweets_only", True)
        self.logger.info(f"Retrying Hydrus import for {len(pending_tweets)} tweets")

        for tweet in pending_tweets:
            if self._shutdown_requested:
                self.logger.info("シャットダウン要求のため未完了 Hydrus インポート再開を中断します")
                return

            if event_only and not self.db_manager.is_event_tweet(tweet["id"]):
                continue

            account = self._get_twitter_monitor_account(tweet["username"])
            self._apply_twitter_account_metadata(tweet, account)
            existing = [
                path for path in tweet.get("local_media", [])
                if to_absolute_path(path, self.config).exists()
            ]
            if not existing:
                self.logger.warning(
                    f"[Retry] Tweet {tweet['id']}: local files are missing, skipping Hydrus retry"
                )
                continue

            imported = await self.hydrus_client.import_tweet_images(tweet, existing)
            expected_count = self.db_manager.estimate_hydrus_expected_count(existing)
            self.db_manager.update_hydrus_import_status(
                tweet_id=tweet["id"],
                imported_count=len(imported),
                expected_count=expected_count,
            )

    def _get_artwork_platform_spec(self, platform: str) -> Dict[str, str]:
        try:
            return self.ARTWORK_PLATFORM_SPECS[platform]
        except KeyError as exc:
            raise ValueError(f"Unsupported artwork platform: {platform}") from exc

    def _get_artwork_extractor(self, platform: str):
        spec = self._get_artwork_platform_spec(platform)
        return getattr(self, spec["extractor_attr"])

    def _artwork_force_full_fetch(self, platform: str) -> bool:
        platform_cfg = self.config.get(platform, {})
        if platform_cfg.get("force_full_fetch", False):
            return True

        gallery_cfg = platform_cfg.get("gallery_dl", {})
        return gallery_cfg.get("force_full_fetch", False)

    async def _fetch_incremental_artworks(self, platform: str, identifier: str, extractor) -> List[Dict[str, Any]]:
        if self._artwork_force_full_fetch(platform):
            self.logger.info(f"{platform}:{identifier} force_full_fetch enabled, running full crawl")
            return await asyncio.to_thread(extractor.fetch_user_works, identifier)

        if not self.db_manager or not self.db_manager.has_any_posts(identifier, platform):
            self.logger.info(f"{platform}:{identifier} has no DB baseline, running initial full crawl")
            return await asyncio.to_thread(extractor.fetch_user_works, identifier)

        existing_ids = self.db_manager.get_existing_post_ids(identifier, platform)
        latest_known_id = self.db_manager.get_latest_post_id(identifier, platform)
        if not existing_ids:
            self.logger.info(f"{platform}:{identifier} has no known IDs, running initial full crawl")
            return await asyncio.to_thread(extractor.fetch_user_works, identifier)

        # FANBOX / bilibili: gallery-dlは各投稿の詳細をAPI往復で取得するため
        # --range 1-N でも実質 N×(待機+API往復) かかる。
        # 軽量API(FANBOX: listCreator / bilibili: feed/space)で新着IDだけ特定し、
        # 新着だけを個別取得する高速パスを使う。
        if hasattr(extractor, "check_new_post_ids"):
            new_ids = await asyncio.to_thread(
                extractor.check_new_post_ids, identifier, existing_ids
            )
            if new_ids is not None:
                if not new_ids:
                    self.logger.info(
                        f"fanbox:{identifier} shallow check found no new posts"
                    )
                    return []
                self.logger.info(
                    f"fanbox:{identifier} shallow check found {len(new_ids)} "
                    f"new posts; fetching metadata for those only"
                )
                works = await asyncio.to_thread(
                    extractor.fetch_works_metadata_by_ids, identifier, new_ids
                )
                return works
            # None = 通信失敗 or max_pages到達 → 従来の--rangeループに委譲
            self.logger.info(
                f"fanbox:{identifier} shallow check unavailable, "
                f"falling back to range-based incremental fetch"
            )

        last_works: List[Dict[str, Any]] = []
        for limit in self.ARTWORK_INCREMENTAL_LIMITS:
            works = await asyncio.to_thread(extractor.fetch_user_works, identifier, limit)
            if not works:
                return works

            last_works = works
            returned_ids = [work.get("id") for work in works if work.get("id")]
            hit_known = any(work_id in existing_ids for work_id in returned_ids)
            hit_latest = latest_known_id is not None and latest_known_id in returned_ids
            exhausted = len(works) < limit

            if hit_known or hit_latest or exhausted:
                reason = "known-id" if (hit_known or hit_latest) else "exhausted"
                self.logger.info(
                    f"{platform}:{identifier} incremental fetch stopped at limit={limit} ({reason})"
                )
                return works

        self.logger.info(
            f"{platform}:{identifier} exceeded incremental limits without overlap; falling back to full crawl"
        )
        return await asyncio.to_thread(extractor.fetch_user_works, identifier)

    @staticmethod
    def _dedupe_artworks(works: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen_ids = set()
        deduped: List[Dict[str, Any]] = []
        for work in works:
            work_id = work.get("id")
            if work_id and work_id in seen_ids:
                continue
            if work_id:
                seen_ids.add(work_id)
            deduped.append(work)
        return deduped

    @staticmethod
    def _sort_artworks_oldest_first(works: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def _sort_key(item: Dict[str, Any]):
            date = item.get("date", "")
            # ID をタイブレーカーに使用。date が空文字の場合（Poipiku等、
            # 投稿日時を取得できないプラットフォーム）は ID 昇順で古い順を保証。
            try:
                id_num = int(item.get("id", 0))
            except (TypeError, ValueError):
                id_num = 0
            return (date, id_num)
        return sorted(works, key=_sort_key)

    def _annotate_artworks(
        self,
        works: List[Dict[str, Any]],
        identifier: str,
        display_name: str,
        account: Dict[str, Any],
    ) -> None:
        for work in works:
            work["username"] = identifier
            work["display_name"] = display_name
            work["custom_tags"] = account.get("custom_tags", [])
            work["rank"] = account.get("rank", 3)

    async def _download_artwork_media(
        self,
        platform: str,
        identifier: str,
        work: Dict[str, Any],
        extractor,
    ) -> List[str]:
        work_id = work.get("id")
        if not work_id or not work.get("media"):
            return []

        download_kwargs: Dict[str, Any] = {}
        if platform == "kemono" and work.get("media_hashes"):
            download_kwargs["hash_map"] = {
                work_id: work["media_hashes"]
            }

        media_paths = await asyncio.to_thread(
            extractor.download_media_for_works,
            identifier,
            [work_id],
            **download_kwargs,
        )

        # extractorがDL時にページから抽出した実URL数を持っていれば
        # work["file_count"]を更新（リトライワークのmedia陳腐化対策）
        expected_counts = getattr(extractor, "_expected_counts", None)
        if expected_counts and work_id in expected_counts:
            work["file_count"] = expected_counts[work_id]

        if not media_paths:
            return []

        return convert_paths_to_relative(
            media_paths.get(work_id, []),
            self.config,
        )

    def _validate_artwork_download(
        self,
        platform: str,
        identifier: str,
        work: Dict[str, Any],
    ) -> bool:
        expected = work.get("file_count") or len(work.get("media", []))
        local_media = work.get("local_media", [])

        # 0バイトファイルを除外（gallery-dlダウンロード失敗時に発生）
        if local_media:
            valid_media = []
            for media_path in local_media:
                abs_path = to_absolute_path(media_path, self.config)
                try:
                    if Path(abs_path).stat().st_size == 0:
                        self.logger.warning(
                            f"{platform} {work['id']}: 0バイトファイルを除外: {media_path}"
                        )
                        continue
                except OSError:
                    pass  # ファイルが存在しない場合はそのまま含める（後続で処理）
                valid_media.append(media_path)
            if len(valid_media) < len(local_media):
                work["local_media"] = valid_media
                local_media = valid_media

        actual = len(local_media)

        if platform == "kemono" and expected > 0 and actual < expected:
            service, user_id = identifier.split("/", 1)
            existing_on_disk = self._find_existing_kemono_files(work, service, user_id)
            if existing_on_disk:
                merged = list(dict.fromkeys(work.get("local_media", []) + existing_on_disk))
                work["local_media"] = merged
                actual = len(merged)

        if expected > 0 and actual < expected:
            self.logger.warning(
                f"{platform} {work['id']}: incomplete download ({actual}/{expected}), skipping DB save"
            )
            return False

        return True

    async def _import_artwork_to_hydrus(
        self,
        platform: str,
        spec: Dict[str, str],
        work: Dict[str, Any],
    ) -> int:
        if not self.hydrus_client.enabled:
            return 0

        event_only = self.hydrus_client.import_settings.get("event_tweets_only", True)
        if not work.get("local_media") or event_only:
            return 0

        import_method = getattr(self.hydrus_client, spec["hydrus_import"])
        update_method = getattr(self.db_manager, spec["hydrus_update"])
        imported = await import_method(work, work["local_media"])
        expected_count = self.db_manager.estimate_hydrus_expected_count(work.get("local_media", []))
        update_method(
            work_id=work["id"],
            imported_count=len(imported),
            expected_count=expected_count,
        )

        if imported:
            self.logger.info(
                f"Imported {len(imported)} files to Hydrus for {platform} work {work['id']}"
            )
        return len(imported)

    async def _run_artwork_backup(
        self,
        platform: str,
        identifier: str,
        work: Dict[str, Any],
    ) -> None:
        if not self.backup_manager or not self.backup_manager.backup_config.get("enabled", False):
            return
        if not work.get("local_media"):
            return

        await self.backup_manager.backup_and_cleanup_works(
            works=[work],
            platform=platform,
            user_id=identifier,
        )

    def _get_artwork_retry_works(
        self,
        platform: str,
        identifier: str,
        *,
        is_log_only: bool = False,
    ) -> List[Dict[str, Any]]:
        getter = getattr(self.db_manager, "get_artwork_retry_works", None)
        if not callable(getter):
            return []
        return getter(platform, identifier, is_log_only=is_log_only)

    def _enqueue_artwork_retry(
        self,
        platform: str,
        identifier: str,
        work: Dict[str, Any],
        *,
        is_log_only: bool = False,
        error: Optional[str] = None,
    ) -> None:
        saver = getattr(self.db_manager, "upsert_artwork_retry", None)
        if not callable(saver):
            return
        saver(platform, identifier, work, is_log_only=is_log_only, error=error)

    def _clear_artwork_retry(
        self,
        platform: str,
        identifier: str,
        work_id: str,
        *,
        is_log_only: bool = False,
    ) -> None:
        clearer = getattr(self.db_manager, "clear_artwork_retry", None)
        if not callable(clearer):
            return
        clearer(platform, identifier, work_id, is_log_only=is_log_only)

    async def _process_monitor_artwork_result(
        self,
        platform: str,
        spec: Dict[str, str],
        identifier: str,
        work: Dict[str, Any],
        save_method,
        index: int,
        total_count: int,
    ) -> Tuple[int, int]:
        self.logger.info(f"{platform} {identifier}: {index}/{total_count} processing")

        if not self._validate_artwork_download(platform, identifier, work):
            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                error="download_incomplete",
            )
            return 0, 0

        try:
            saved = save_method([work], identifier)
        except Exception as exc:
            self.logger.error(f"Error saving {platform} work {work['id']}: {exc}")
            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                error=f"save_error:{exc}",
            )
            return 0, 0

        if not saved:
            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                error="save_returned_zero",
            )
            return 0, 0

        self._clear_artwork_retry(platform, identifier, work["id"])
        imported = await self._import_artwork_to_hydrus(platform, spec, work)
        await self._run_artwork_backup(platform, identifier, work)
        return saved, imported

    async def _process_log_only_artwork_result(
        self,
        platform: str,
        identifier: str,
        work: Dict[str, Any],
        save_method,
        index: int,
        total_count: int,
    ) -> Tuple[int, int]:
        self.logger.info(f"{platform} log-only {identifier}: {index}/{total_count} processing")

        if not self._validate_artwork_download(platform, identifier, work):
            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                is_log_only=True,
                error="download_incomplete",
            )
            return 0, 1

        try:
            if save_method(work, identifier):
                self._clear_artwork_retry(
                    platform,
                    identifier,
                    work["id"],
                    is_log_only=True,
                )
                return 1, 0

            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                is_log_only=True,
                error="save_returned_false",
            )
            return 0, 1
        except Exception as exc:
            self.logger.error(f"Error saving {platform} work {work['id']}: {exc}")
            self._enqueue_artwork_retry(
                platform,
                identifier,
                work,
                is_log_only=True,
                error=f"save_error:{exc}",
            )
            return 0, 1

    async def _recheck_incomplete_media(
        self,
        platform: str,
        spec: dict,
        identifier: str,
        extractor,
        account: Dict[str, Any],
        save_method,
    ) -> Tuple[int, int]:
        """メディア不完全投稿を個別に再チェックし、メディア増加時は再DL・再保存する

        有料コンテンツ系プラットフォーム（FANBOX/Fantia）で、
        無料会員時にサムネだけDLされた投稿を自動検出・更新する。
        """
        fetch_by_ids_name = spec.get("fetch_by_ids")
        if not fetch_by_ids_name or not self.db_manager:
            return 0, 0

        low_media_ids = self.db_manager.get_low_media_work_ids(identifier, platform)
        if not low_media_ids:
            return 0, 0

        self.logger.info(
            f"{platform} {identifier}: rechecking {len(low_media_ids)} works with low media count"
        )

        fetch_by_ids = getattr(extractor, fetch_by_ids_name)
        recheck_works = await asyncio.to_thread(fetch_by_ids, identifier, low_media_ids)
        if not recheck_works:
            return 0, 0

        display_name = account.get("display_name", identifier)
        self._annotate_artworks(recheck_works, identifier, display_name, account)

        # filterを通して実際にメディア増加したものだけ抽出
        filter_method = getattr(self.db_manager, spec["filter_monitor"])
        increased_works = filter_method(recheck_works, identifier)
        if not increased_works:
            return 0, 0

        increased_works = self._sort_artworks_oldest_first(increased_works)
        self.logger.info(
            f"{platform} {identifier}: {len(increased_works)} works have increased media, re-downloading"
        )

        total_saved = 0
        total_imported = 0
        for idx, work in enumerate(increased_works, 1):
            if self._shutdown_requested:
                break

            work["local_media"] = await self._download_artwork_media(
                platform, identifier, work, extractor,
            )
            saved, imported = await self._process_monitor_artwork_result(
                platform, spec, identifier, work, save_method, idx, len(increased_works),
            )
            total_saved += saved
            total_imported += imported

        return total_saved, total_imported

    async def _recheck_incomplete_media_log_only(
        self,
        platform: str,
        spec: dict,
        identifier: str,
        extractor,
        account: Dict[str, Any],
        save_method,
    ) -> Tuple[int, int]:
        """ログ専用アカウント版のメディア不完全投稿再チェック"""
        fetch_by_ids_name = spec.get("fetch_by_ids")
        if not fetch_by_ids_name or not self.db_manager:
            return 0, 0

        low_media_ids = self.db_manager.get_low_media_work_ids(identifier, platform)
        if not low_media_ids:
            return 0, 0

        self.logger.info(
            f"{platform} log-only {identifier}: rechecking {len(low_media_ids)} works with low media count"
        )

        fetch_by_ids = getattr(extractor, fetch_by_ids_name)
        recheck_works = await asyncio.to_thread(fetch_by_ids, identifier, low_media_ids)
        if not recheck_works:
            return 0, 0

        display_name = account.get("display_name", identifier)
        self._annotate_artworks(recheck_works, identifier, display_name, account)

        filter_method = getattr(self.db_manager, spec["filter_log_only"])
        increased_works = filter_method(recheck_works, identifier)
        if not increased_works:
            return 0, 0

        increased_works = self._sort_artworks_oldest_first(increased_works)
        self.logger.info(
            f"{platform} log-only {identifier}: {len(increased_works)} works have increased media, re-downloading"
        )

        total_saved = 0
        total_failed = 0
        for idx, work in enumerate(increased_works, 1):
            if self._shutdown_requested:
                break

            work["local_media"] = await self._download_artwork_media(
                platform, identifier, work, extractor,
            )
            saved_delta, failed_delta = await self._process_log_only_artwork_result(
                platform, identifier, work, save_method, idx, len(increased_works),
            )
            total_saved += saved_delta
            total_failed += failed_delta

        return total_saved, total_failed

    async def _process_artwork_monitor_account(self, account: Dict[str, Any], platform: str) -> None:
        spec = self._get_artwork_platform_spec(platform)
        extractor = self._get_artwork_extractor(platform)
        identifier = account["username"]
        display_name = account.get("display_name", identifier)

        try:
            if not extractor:
                self.logger.error(f"{platform} extractor not initialized, skipping account")
                return

            self.status_notifier.notify_running(current_account=f"{platform}:{identifier}")
            self.logger.info(f"Checking {platform} account: {display_name} ({identifier})")

            retry_works = self._get_artwork_retry_works(platform, identifier)
            if retry_works:
                self.logger.info(f"Loaded {len(retry_works)} pending {platform} retry works for {identifier}")

            works = await self._fetch_incremental_artworks(platform, identifier, extractor)
            if not works and not retry_works:
                self.logger.info(f"No {platform} works found for {identifier}")
                getattr(self, spec["unreachable"])(identifier, account)
                return

            if works:
                self._annotate_artworks(works, identifier, display_name, account)
            if retry_works:
                self._annotate_artworks(retry_works, identifier, display_name, account)

            filter_method = getattr(self.db_manager, spec["filter_monitor"])
            fresh_works = filter_method(works, identifier) if works else []
            retry_works = self._sort_artworks_oldest_first(self._dedupe_artworks(retry_works))
            retry_ids = {work.get("id") for work in retry_works if work.get("id")}
            fresh_works = [work for work in fresh_works if work.get("id") not in retry_ids]
            new_works = retry_works + self._sort_artworks_oldest_first(self._dedupe_artworks(fresh_works))
            if not new_works:
                self.logger.info(f"No new {platform} works for {identifier}")
                return

            self.logger.info(f"Found {len(new_works)} new {platform} works for {identifier}")
            self.status_notifier.add_new_tweets(len(new_works))

            save_method = getattr(self.db_manager, spec["save_monitor"])
            total_saved = 0
            total_imported = 0
            for idx, work in enumerate(new_works, 1):
                if self._shutdown_requested:
                    self.logger.info(f"Shutdown requested, stopping {platform} processing for {identifier}")
                    break

                work["local_media"] = await self._download_artwork_media(
                    platform,
                    identifier,
                    work,
                    extractor,
                )
                saved, imported = await self._process_monitor_artwork_result(
                    platform,
                    spec,
                    identifier,
                    work,
                    save_method,
                    idx,
                    len(new_works),
                )
                total_saved += saved
                total_imported += imported

            self.logger.info(
                f"{platform} {identifier}: completed - {total_saved} saved, {total_imported} imported"
            )

            # 有料コンテンツ系プラットフォーム: メディア不完全投稿の自動再チェック
            recheck_saved, recheck_imported = await self._recheck_incomplete_media(
                platform, spec, identifier, extractor, account, save_method,
            )
            if recheck_saved or recheck_imported:
                self.logger.info(
                    f"{platform} {identifier}: media recheck - {recheck_saved} saved, "
                    f"{recheck_imported} imported"
                )

            self.status_notifier.increment_processed_accounts()

        except Exception as exc:
            self.logger.error(f"Error processing {platform} account {identifier}: {exc}", exc_info=True)
            self.status_notifier.notify_error(str(exc), str(exc))
            getattr(self, spec["unreachable"])(identifier, account)

    async def _process_artwork_log_only_account(self, account: Dict[str, Any], platform: str) -> None:
        spec = self._get_artwork_platform_spec(platform)
        extractor = self._get_artwork_extractor(platform)
        identifier = account["username"]
        display_name = account.get("display_name", identifier)

        try:
            if not extractor:
                self.logger.error(f"{platform} extractor not initialized, skipping account")
                return

            self.logger.info(f"Processing {platform} log-only account: {display_name} ({identifier})")

            retry_works = self._get_artwork_retry_works(platform, identifier, is_log_only=True)
            if retry_works:
                self.logger.info(f"Loaded {len(retry_works)} pending {platform} log-only retry works for {identifier}")

            works = await self._fetch_incremental_artworks(platform, identifier, extractor)
            if not works and not retry_works:
                self.logger.info(f"No {platform} works found for log-only {identifier}")
                getattr(self, spec["unreachable"])(identifier, account)
                return

            if works:
                self._annotate_artworks(works, identifier, display_name, account)
            if retry_works:
                self._annotate_artworks(retry_works, identifier, display_name, account)

            filter_method = getattr(self.db_manager, spec["filter_log_only"])
            fresh_works = filter_method(works, identifier) if works else []
            retry_works = self._sort_artworks_oldest_first(self._dedupe_artworks(retry_works))
            retry_ids = {work.get("id") for work in retry_works if work.get("id")}
            fresh_works = [work for work in fresh_works if work.get("id") not in retry_ids]
            new_works = retry_works + self._sort_artworks_oldest_first(self._dedupe_artworks(fresh_works))
            if not new_works:
                self.logger.info(f"No new {platform} works for log-only {identifier}")
                return

            save_method = getattr(self.db_manager, spec["save_log_only"])
            saved_count = 0
            failed_count = 0
            for idx, work in enumerate(new_works, 1):
                if self._shutdown_requested:
                    self.logger.info(f"Shutdown requested, stopping {platform} log-only processing for {identifier}")
                    break

                work["local_media"] = await self._download_artwork_media(
                    platform,
                    identifier,
                    work,
                    extractor,
                )
                saved_delta, failed_delta = await self._process_log_only_artwork_result(
                    platform,
                    identifier,
                    work,
                    save_method,
                    idx,
                    len(new_works),
                )
                saved_count += saved_delta
                failed_count += failed_delta

            self.logger.info(
                f"{platform} log-only {identifier}: {saved_count} saved, {failed_count} failed"
            )

            # 有料コンテンツ系プラットフォーム: メディア不完全投稿の自動再チェック
            recheck_saved, _ = await self._recheck_incomplete_media_log_only(
                platform, spec, identifier, extractor, account, save_method,
            )
            if recheck_saved:
                self.logger.info(
                    f"{platform} log-only {identifier}: media recheck - {recheck_saved} updated"
                )

        except Exception as exc:
            self.logger.error(f"Error processing {platform} log-only account {identifier}: {exc}", exc_info=True)
            getattr(self, spec["unreachable"])(identifier, account)

    async def _notify_and_import(self, event_tweets: List[Dict[str, Any]], username: str, display_name: str) -> None:
        sorted_event_tweets = sorted(event_tweets, key=lambda x: x.get('date', ''))
        for tweet in sorted_event_tweets:
            if self._shutdown_requested:
                self.logger.info(f"シャットダウン要求のため @{username} の通知・インポートを中断します")
                break
            await self.discord_notifier.send_notification(tweet, username, display_name)
            if getattr(self.discord_notifier, "enabled", False):
                self.db_manager.mark_as_notified(tweet['id'])
            if self.hydrus_client.enabled and tweet.get('local_media'):
                if self.hydrus_client.import_settings.get('event_tweets_only', True):
                    imported = await self.hydrus_client.import_tweet_images(tweet, tweet['local_media'])
                    expected_count = self.db_manager.estimate_hydrus_expected_count(tweet.get('local_media', []))
                    self.db_manager.update_hydrus_import_status(
                        tweet_id=tweet['id'],
                        imported_count=len(imported),
                        expected_count=expected_count,
                    )
                    if imported:
                        self.logger.info(f"Imported {len(imported)} images to Hydrus for tweet {tweet['id']}")

    async def _run_backup_pipeline(
        self,
        username: str,
        new_tweets: List[Dict[str, Any]],
        event_tweets: List[Dict[str, Any]],
    ) -> None:
        saved_count = 0
        failed_count = 0

        if self.backup_manager.backup_config.get('enabled', False):
            self.backup_manager._ensure_repo_exists()

        existing_count = self.db_manager.get_tweet_count_for_user(username)
        is_first_run = existing_count == 0
        if is_first_run:
            self.logger.info(f"First time processing @{username} (no existing tweets in DB)")

        if self.backup_manager.should_use_batch_mode(is_first_run=is_first_run):
            await self._run_batch_backup(username, new_tweets, event_tweets)
            return

        sorted_tweets = sorted(new_tweets, key=lambda x: x.get('date', ''))
        for tweet in sorted_tweets:
            try:
                success = await self.backup_manager.backup_tweet_and_save(
                    tweet,
                    username,
                    is_log_only=False,
                    hydrus_client=self.hydrus_client,
                    is_first_run=is_first_run,
                )
                if success:
                    saved_count += 1
                    self.logger.debug(f"Successfully processed tweet {tweet['id']}")
                else:
                    failed_count += 1
                    self.logger.warning(f"Failed to process tweet {tweet['id']}")
            except Exception as exc:
                self.logger.error(f"Error processing tweet {tweet['id']}: {exc}")
                failed_count += 1

        self.logger.info(
            f"Processed {saved_count} tweets successfully, {failed_count} failed for @{username}"
        )

        if is_first_run and self.backup_manager.backup_config.get('enabled', False):
            self.logger.info(f"First run in immediate mode - executing batch upload for @{username}")
            await self.backup_manager.batch_upload_folder(
                folder_path=Path('.'),
                account_type='monitoring',
                encrypt=self.backup_manager.rclone_client is not None,
                delete_after=False,
                username=username,
            )

    async def _run_batch_backup(
        self,
        username: str,
        new_tweets: List[Dict[str, Any]],
        event_tweets: List[Dict[str, Any]],
    ) -> None:
        self.logger.info(f"Using batch mode for monitoring account @{username}")
        sorted_tweets = sorted(new_tweets, key=lambda x: x.get('date', ''))
        for tweet in sorted_tweets:
            if self._shutdown_requested:
                self.logger.info(f"シャットダウン要求のため @{username} のバッチバックアップを中断します")
                break
            if self.hydrus_client.enabled and tweet.get('local_media'):
                event_only = self.hydrus_client.import_settings.get('event_tweets_only', True)
                if not event_only or tweet['id'] in {et['id'] for et in event_tweets}:
                    imported = await self.hydrus_client.import_tweet_images(tweet, tweet['local_media'])
                    expected_count = self.db_manager.estimate_hydrus_expected_count(tweet.get('local_media', []))
                    self.db_manager.update_hydrus_import_status(
                        tweet_id=tweet['id'],
                        imported_count=len(imported),
                        expected_count=expected_count,
                    )
                    if imported:
                        self.logger.info(f"Imported {len(imported)} images to Hydrus for tweet {tweet['id']}")

        await self.backup_manager.batch_upload_folder(
            folder_path=Path('.'),
            account_type='monitoring',
            encrypt=self.backup_manager.rclone_client is not None,
            delete_after=False,
            username=username,
        )
        self.logger.info(f"Batch upload completed for @{username}")

    async def _process_log_only_account(self, account: Dict[str, Any]) -> None:
        username = account['username']
        display_name = account.get('display_name', username)
        self.logger.info(f"Processing log-only account: {display_name} (@{username})")

        retry_tweets = self._get_twitter_retry_tweets(username, is_log_only=True)
        if retry_tweets:
            self.logger.info(f"Loaded {len(retry_tweets)} pending twitter log-only retry tweets for @{username}")

        tweets, _ = await self.twitter_monitor.get_user_tweets_with_gallery_dl_first(
            username,
            days_lookback=self.config['tweet_settings']['days_lookback'],
            event_detection_enabled=False,
        )

        if not tweets and not retry_tweets:
            self._record_unreachable_twitter(username, account)
            self.logger.info(f"No tweets found for log-only account @{username}")
            return

        for tweet in tweets:
            tweet["username"] = username
            tweet["display_name"] = display_name
            tweet["custom_tags"] = account.get("custom_tags", [])
            tweet["rank"] = account.get("rank", 3)
        for tweet in retry_tweets:
            tweet["username"] = username
            tweet["display_name"] = display_name
            tweet["custom_tags"] = account.get("custom_tags", [])
            tweet["rank"] = account.get("rank", 3)

        fresh_tweets = self.db_manager.filter_log_only_tweets(tweets, username) if tweets else []
        retry_tweets = self._deduplicate_tweets(retry_tweets, username)
        retry_ids = {tweet.get("id") for tweet in retry_tweets if tweet.get("id")}
        fresh_tweets = [tweet for tweet in fresh_tweets if tweet.get("id") not in retry_ids]
        new_tweets = retry_tweets + self._deduplicate_tweets(fresh_tweets, username)
        if not new_tweets:
            self.logger.info(f"No new tweets for log-only account @{username}")
            return

        tweet_ids_with_media = [
            tweet['id']
            for tweet in new_tweets
            if tweet.get('media') or tweet.get('videos')
        ]
        self.logger.info(f"Found {len(tweet_ids_with_media)} tweets with media for @{username}")

        if self.backup_manager.backup_config.get('enabled', False):
            self.backup_manager._ensure_repo_exists()

        media_paths = {}
        if tweet_ids_with_media:
            self.logger.info(f"Downloading media for {len(tweet_ids_with_media)} tweets in batch...")
            is_private_account = self.twitter_monitor.is_account_private(username)
            media_paths = await asyncio.to_thread(
                self.twitter_monitor.gallery_dl_extractor.download_media_for_tweets,
                username,
                tweet_ids_with_media,
                move_to_images=True,
                is_private_account=is_private_account,
            )
            self.logger.info(f"Downloaded media for {len(media_paths)} tweets")

        for tweet in new_tweets:
            tweet['local_media'] = media_paths.get(tweet['id'], [])

        valid_tweets: List[Dict[str, Any]] = []
        for tweet in new_tweets:
            if self._validate_tweet_download(username, tweet):
                valid_tweets.append(tweet)
            else:
                self._enqueue_twitter_retry(
                    username,
                    tweet,
                    is_log_only=True,
                    error="download_incomplete",
                )

        if len(valid_tweets) != len(new_tweets):
            skipped_count = len(new_tweets) - len(valid_tweets)
            self.logger.warning(
                f"Queued {skipped_count} incomplete log-only tweets for @{username} for retry"
            )

        if not valid_tweets:
            self.logger.info(f"No fully downloaded log-only tweets remain for @{username}")
            return

        new_tweets = valid_tweets

        existing_count = self.db_manager.get_log_only_tweet_count_for_user(username)
        is_first_run = existing_count == 0
        if is_first_run:
            self.logger.info(f"First time processing log account @{username} (no existing tweets in DB)")

        log_accounts_enabled = self.config.get('log_only_accounts', {}).get('enabled', False)
        saved_count = 0
        failed_count = 0
        processed_media_count = sum(len(paths) for paths in media_paths.values())

        for tweet in new_tweets:
            if self._shutdown_requested:
                self.logger.info(f"シャットダウン要求のため @{username} のログ保存を中断します")
                break
            try:
                if log_accounts_enabled:
                    success = await self.backup_manager.backup_tweet_and_save(tweet, username, is_log_only=True)
                else:
                    success = self.db_manager.save_single_log_only_tweet(tweet, username)
                if success:
                    saved_count += 1
                    self._clear_twitter_retry(username, tweet["id"], is_log_only=True)
                else:
                    failed_count += 1
                    self.logger.warning(f"Failed to process tweet {tweet['id']}")
                    self._enqueue_twitter_retry(
                        username,
                        tweet,
                        is_log_only=True,
                        error="save_failed",
                    )
            except Exception as exc:
                self.logger.error(f"Error processing tweet {tweet['id']}: {exc}")
                failed_count += 1
                self._enqueue_twitter_retry(
                    username,
                    tweet,
                    is_log_only=True,
                    error=f"save_error:{exc}",
                )

        self.logger.info(
            f"Completed processing for @{username}: {saved_count} tweets saved, "
            f"{failed_count} failed, {processed_media_count} media files processed"
        )

        # twitter_id が未保存なら取得してCSVに書き戻す
        self._try_save_twitter_id(username, account)

    # ------------------------------------------------------------------
    # Pixiv processing
    # ------------------------------------------------------------------

    async def _process_pixiv_monitor_account(self, account: Dict[str, Any]) -> None:
        """Pixiv通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "pixiv")

    async def _process_pixiv_log_only_account(self, account: Dict[str, Any]) -> None:
        """Pixivログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "pixiv")

    def _try_save_twitter_id(self, username: str, account: Dict[str, Any]) -> None:
        """twitter_id が未保存かつキャッシュにある場合、CSVに書き戻す"""
        if account.get("twitter_id"):
            return  # 既に保存済み
        platform = account.get("platform", "twitter")
        if platform not in ("twitter", ""):
            return
        resolved_id = self.twitter_monitor.get_resolved_twitter_id(username)
        if resolved_id is None:
            return
        self._update_account_in_csv(username, {"twitter_id": str(resolved_id)})
        account["twitter_id"] = resolved_id
        self.logger.info(f"twitter_id を保存: @{username} → {resolved_id}")

    def _update_account_in_csv(self, target_username: str, updates: Dict[str, str]) -> None:
        """monitored_accounts.csv の指定usernameの行を更新する"""
        import csv as _csv
        csv_path = Path("monitored_accounts.csv")
        if not csv_path.exists():
            self.logger.error("monitored_accounts.csv が見つかりません")
            return

        try:
            rows = []
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = _csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for row in reader:
                    rows.append(row)

            # twitter_id カラムがなければ追加
            if "twitter_id" not in fieldnames:
                fieldnames.append("twitter_id")

            updated = False
            old_username = target_username
            for row in rows:
                if row.get("username") == target_username:
                    for key, value in updates.items():
                        if key in fieldnames:
                            row[key] = value
                    updated = True
                    break

            if not updated:
                self.logger.warning(f"CSV内に @{target_username} が見つかりません")
                return

            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            new_username = updates.get("username")
            if new_username and new_username != old_username:
                self.logger.info(f"CSV更新完了: @{old_username} → @{new_username}")
            else:
                self.logger.debug(f"CSV更新完了: @{target_username}")

        except Exception as e:
            self.logger.error(f"CSV更新失敗 @{target_username}: {e}", exc_info=True)

    def _record_unreachable_twitter(self, username: str, account: Dict[str, Any]) -> None:
        """Twitter _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if self.twitter_monitor._account_reachable.get(username.lower()) is not False:
            return  # 到達不能シグナルなし（正常な「新着なし」等）
        self.account_status_tracker.flag_account(
            username=username,
            platform="twitter",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", username),
            error_msg="user_by_login returned None",
        )
        self.account_status_tracker.save()

    def _record_unreachable_pixiv(self, user_id: str, account: Dict[str, Any]) -> None:
        """Pixiv _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.pixiv_extractor:
            return
        if self.pixiv_extractor._account_reachable.get(user_id) is not False:
            return  # 到達不能シグナルなし（正常な「作品なし」等）
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="pixiv",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="gallery-dl returned not-found error",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Kemono processing
    # ------------------------------------------------------------------

    def _find_existing_kemono_files(
        self, work: Dict[str, Any], service: str, user_id: str,
    ) -> List[str]:
        """ディスク上の既存ファイルを探して相対パスリストを返す

        DL不完全判定された作品に対して、imagesディレクトリ内の既存ファイル
        ({post_id}_*.{ext}パターン) を検索し、local_mediaの補完に使う。
        """
        try:
            images_base, _ = get_media_base_paths(self.config)
            dir_name = f"{service}_{user_id}"
            images_dir = images_base / dir_name
            if not images_dir.exists():
                return []

            post_id = work.get('post_id', '')
            if not post_id:
                # work_id (e.g. "fanbox_9929392") から post_id を抽出
                wid = work.get('id', '')
                parts = wid.split('_', 1)
                post_id = parts[1] if len(parts) == 2 else wid

            existing = []
            for f in images_dir.iterdir():
                if f.is_file() and (
                    f.name.startswith(f"{post_id}_")
                    or f.name.startswith(f"{post_id}.")
                ):
                    # 相対パスに変換 (images/{dir_name}/{filename})
                    rel = f"images/{dir_name}/{f.name}"
                    existing.append(rel)

            if existing:
                existing.sort()
                self.logger.info(
                    f"ディスク上に既存ファイル {len(existing)}件を検出: "
                    f"{work.get('id', '?')}"
                )
            return existing
        except Exception as e:
            self.logger.warning(f"既存ファイル検索でエラー: {e}")
            return []

    async def _process_kemono_monitor_account(self, account: Dict[str, Any]) -> None:
        """Kemono通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "kemono")

    async def _process_kemono_log_only_account(self, account: Dict[str, Any]) -> None:
        """Kemonoログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "kemono")

    def _record_unreachable_kemono(self, user_id: str, account: Dict[str, Any]) -> None:
        """Kemono _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.kemono_extractor:
            return
        if self.kemono_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="kemono",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="gallery-dl returned not-found error",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # TINAMI processing
    # ------------------------------------------------------------------

    async def _process_tinami_monitor_account(self, account: Dict[str, Any]) -> None:
        """TINAMI通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "tinami")

    async def _process_tinami_log_only_account(self, account: Dict[str, Any]) -> None:
        """TINAMIログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "tinami")

    def _record_unreachable_tinami(self, prof_id: str, account: Dict[str, Any]) -> None:
        """TINAMI _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.tinami_extractor:
            return
        if self.tinami_extractor._account_reachable.get(prof_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=prof_id,
            platform="tinami",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", prof_id),
            error_msg="TINAMI profile unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Poipiku processing
    # ------------------------------------------------------------------

    async def _process_poipiku_monitor_account(self, account: Dict[str, Any]) -> None:
        """Poipiku通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "poipiku")

    async def _process_poipiku_log_only_account(self, account: Dict[str, Any]) -> None:
        """Poipikuログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "poipiku")

    def _record_unreachable_poipiku(self, user_id: str, account: Dict[str, Any]) -> None:
        """Poipiku _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.poipiku_extractor:
            return
        if self.poipiku_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="poipiku",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Poipiku profile unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Fantia processing
    # ------------------------------------------------------------------

    async def _process_fantia_monitor_account(self, account: Dict[str, Any]) -> None:
        """Fantia通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "fantia")

    async def _process_fantia_log_only_account(self, account: Dict[str, Any]) -> None:
        """Fantiaログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "fantia")

    def _record_unreachable_fantia(self, fanclub_id: str, account: Dict[str, Any]) -> None:
        """Fantia _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.fantia_extractor:
            return
        if self.fantia_extractor._account_reachable.get(fanclub_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=fanclub_id,
            platform="fantia",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", fanclub_id),
            error_msg="Fantia fanclub unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Nijie processing
    # ------------------------------------------------------------------

    async def _process_nijie_monitor_account(self, account: Dict[str, Any]) -> None:
        """ニジエ通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "nijie")

    async def _process_nijie_log_only_account(self, account: Dict[str, Any]) -> None:
        """ニジエログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "nijie")

    def _record_unreachable_nijie(self, user_id: str, account: Dict[str, Any]) -> None:
        """Nijie _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.nijie_extractor:
            return
        if self.nijie_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="nijie",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Nijie user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Skeb processing
    # ------------------------------------------------------------------

    async def _process_skeb_monitor_account(self, account: Dict[str, Any]) -> None:
        """Skeb通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "skeb")

    async def _process_skeb_log_only_account(self, account: Dict[str, Any]) -> None:
        """Skebログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "skeb")

    def _record_unreachable_skeb(self, user_id: str, account: Dict[str, Any]) -> None:
        """Skeb _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.skeb_extractor:
            return
        if self.skeb_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="skeb",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Skeb user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # bilibili processing
    # ------------------------------------------------------------------

    def _record_unreachable_bilibili(self, user_id: str, account: Dict[str, Any]) -> None:
        """bilibili _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.bilibili_extractor:
            return
        if self.bilibili_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="bilibili",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="bilibili user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Misskey processing
    # ------------------------------------------------------------------

    async def _process_misskey_monitor_account(self, account: Dict[str, Any]) -> None:
        """Misskey通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "misskey")

    async def _process_misskey_log_only_account(self, account: Dict[str, Any]) -> None:
        """Misskeyログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "misskey")

    def _record_unreachable_misskey(self, user_id: str, account: Dict[str, Any]) -> None:
        """Misskey _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.misskey_extractor:
            return
        if self.misskey_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="misskey",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Misskey user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Gelbooru processing
    # ------------------------------------------------------------------

    async def _process_gelbooru_monitor_account(self, account: Dict[str, Any]) -> None:
        """Gelbooru通常監視アカウントを処理（タグ検索、バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "gelbooru")

    async def _process_gelbooru_log_only_account(self, account: Dict[str, Any]) -> None:
        """Gelbooruログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "gelbooru")

    def _record_unreachable_gelbooru(self, user_id: str, account: Dict[str, Any]) -> None:
        """Gelbooru _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.gelbooru_extractor:
            return
        if self.gelbooru_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="gelbooru",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Gelbooru search query unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # FANBOX processing
    # ------------------------------------------------------------------

    async def _process_fanbox_monitor_account(self, account: Dict[str, Any]) -> None:
        """通常FANBOXモニタリングアカウントを処理（バッチ逐次）"""
        return await self._process_artwork_monitor_account(account, "fanbox")

    async def _process_fanbox_log_only_account(self, account: Dict[str, Any]) -> None:
        """ログ専用FANBOXアカウントを処理（バッチ逐次）"""
        return await self._process_artwork_log_only_account(account, "fanbox")

    def _record_unreachable_fanbox(self, creator_id: str, account: Dict[str, Any]) -> None:
        """FANBOX _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.fanbox_extractor:
            return
        if self.fanbox_extractor._account_reachable.get(creator_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=creator_id,
            platform="fanbox",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", creator_id),
            error_msg="FANBOX creator unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Bluesky processing
    # ------------------------------------------------------------------

    async def _process_bluesky_monitor_account(self, account: Dict[str, Any]) -> None:
        """通常Blueskyモニタリングアカウントを処理（バッチ逐次）"""
        return await self._process_artwork_monitor_account(account, "bluesky")

    async def _process_bluesky_log_only_account(self, account: Dict[str, Any]) -> None:
        """ログ専用Blueskyアカウントを処理（バッチ逐次）"""
        return await self._process_artwork_log_only_account(account, "bluesky")

    def _record_unreachable_bluesky(self, handle: str, account: Dict[str, Any]) -> None:
        """Bluesky _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.bluesky_extractor:
            return
        if self.bluesky_extractor._account_reachable.get(handle) is not False:
            return
        self.account_status_tracker.flag_account(
            username=handle,
            platform="bluesky",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", handle),
            error_msg="Bluesky user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Privatter processing
    # ------------------------------------------------------------------

    async def _process_privatter_monitor_account(self, account: Dict[str, Any]) -> None:
        """Privatter通常監視アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_monitor_account(account, "privatter")
    async def _process_privatter_log_only_account(self, account: Dict[str, Any]) -> None:
        """Privatterログ専用アカウントを処理（バッチ逐次処理）"""
        return await self._process_artwork_log_only_account(account, "privatter")
    def _record_unreachable_privatter(self, user_id: str, account: Dict[str, Any]) -> None:
        """Privatter _account_reachable が False の場合のみフラグ記録"""
        if not self.account_status_tracker:
            return
        if not self.privatter_extractor:
            return
        if self.privatter_extractor._account_reachable.get(user_id) is not False:
            return
        self.account_status_tracker.flag_account(
            username=user_id,
            platform="privatter",
            account_type=account.get("account_type", ""),
            display_name=account.get("display_name", user_id),
            error_msg="Privatter user unreachable",
        )
        self.account_status_tracker.save()

    # ------------------------------------------------------------------
    # Discord processing
    # ------------------------------------------------------------------

    async def _process_discord_account(self, account: Dict[str, Any]) -> None:
        """Discordサーバーのエクスポートを処理"""
        guild_id = str(account['username'])  # usernameカラムにserver_idが入る
        server_name = account.get('display_name', guild_id)

        try:
            if not self.discord_exporter:
                self.logger.error("DiscordExporter not initialized, skipping Discord account")
                return

            self.logger.info(f"Exporting Discord server: {server_name} ({guild_id})")
            self.status_notifier.notify_running(current_account=f"Discord: {server_name}")
            self.status_notifier.current_discord_server = server_name

            success, channel_count = await self.discord_exporter.export_guild(guild_id)

            if success:
                self.logger.info(f"Discord export completed: {server_name}")
                self.status_notifier.increment_processed_discord_servers()
                self.status_notifier.add_discord_channels(channel_count)
            else:
                self.logger.error(f"Discord export failed: {server_name}")

        except Exception as e:
            self.logger.error(f"Discord export error for {server_name}: {e}", exc_info=True)
        finally:
            self.status_notifier.current_discord_server = None

    # ------------------------------------------------------------------
    # Hydrus未インポート作品のリトライ
    # ------------------------------------------------------------------

    async def retry_pending_hydrus_imports(self) -> None:
        """Retry incomplete Hydrus imports for all artwork platforms."""
        if not self.hydrus_client.enabled:
            return

        event_only = self.hydrus_client.import_settings.get('event_tweets_only', True)
        if event_only:
            return

        generic_pending_getter = getattr(self.db_manager, "get_pending_hydrus_works", None)

        for platform, spec in self.ARTWORK_PLATFORM_SPECS.items():
            if self._shutdown_requested:
                self.logger.info("Shutdown requested; stopping pending Hydrus retry")
                return

            if callable(generic_pending_getter):
                pending = generic_pending_getter(platform)
            else:
                pending_getter = getattr(self.db_manager, f"get_pending_hydrus_{platform}_works", None)
                if pending_getter is None:
                    continue
                pending = pending_getter()

            if not pending:
                continue

            import_method = getattr(self.hydrus_client, spec["hydrus_import"])
            update_method = getattr(self.db_manager, spec["hydrus_update"])
            self.logger.info(
                f"Retrying Hydrus import for {len(pending)} {platform} works"
            )

            for work in pending:
                if self._shutdown_requested:
                    self.logger.info("Shutdown requested; stopping pending Hydrus retry")
                    return

                local_media = work.get("local_media") or []
                existing = [
                    media_path for media_path in local_media
                    if to_absolute_path(media_path, self.config).exists()
                ]
                if not existing:
                    self.logger.info(
                        f"[Retry] {platform} work {work['id']}: no local media found; "
                        "marking Hydrus import complete with zero expected files"
                    )
                    update_method(
                        work_id=work["id"],
                        imported_count=0,
                        expected_count=0,
                        force=True,
                    )
                    continue

                imported = await import_method(work, existing)
                expected = self.db_manager.estimate_hydrus_expected_count(existing)
                update_method(
                    work_id=work["id"],
                    imported_count=len(imported),
                    expected_count=expected,
                    force=True,
                )
                if imported:
                    self.logger.info(
                        f"[Retry] Imported {len(imported)} images to Hydrus for "
                        f"{platform} work {work['id']}"
                    )
