import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set
import re
import time

from dotenv import load_dotenv
import aiohttp
import httpx

from .path_utils import convert_paths_to_relative
from .twscrape_compat import apply_twscrape_compat_patches

apply_twscrape_compat_patches()

from twscrape import API, Tweet
from twscrape.utils import parse_cookies


class TwitterMonitor:
    def __init__(self, config: dict, db_manager=None, event_detector=None):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.TwitterMonitor")
        self.api = API()
        self._accounts_initialized = False
        self._session = None
        self.db_manager = db_manager
        self._timeout_seconds = 300  # 5分のタイムアウト
        
        # リトライカウンタを初期化
        self._tweet_retry_count = 0
        self._http_retry_count = 0
        
        # gallery-dl extractorを初期化
        from .gallery_dl_extractor import GalleryDLExtractor
        self.gallery_dl_extractor = GalleryDLExtractor(config, event_detector)
        self._private_account_cache: Dict[str, bool] = {}
        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}
        self._account_sensitive_cache: Dict[str, bool] = {}
        # ID変更追跡用キャッシュ
        self._detected_renames: Dict[str, Dict] = {}  # old_username.lower() -> rename info
        self._resolved_twitter_ids: Dict[str, int] = {}  # username.lower() -> numeric twitter_id
        self._quick_check_cache: Dict[tuple, bool] = {}

    def _get_incremental_overlap_delta(self) -> timedelta:
        """増分取得時に再走査する重なり期間を返す。"""
        raw_value = self.config.get('tweet_settings', {}).get('incremental_overlap_hours', 48)
        try:
            hours = max(0, int(raw_value))
        except (TypeError, ValueError):
            hours = 48
        return timedelta(hours=hours)

    def _get_quick_check_scan_count(self) -> int:
        """クイックチェックで確認する最大ツイート数。"""
        raw_value = self.config.get('tweet_settings', {}).get('quick_check_scan_count', 20)
        try:
            count = max(1, int(raw_value))
        except (TypeError, ValueError):
            count = 20
        return count

    def _get_quick_check_fallback_mode(self) -> str:
        """クイックチェックで追加確認するendpointの範囲。"""
        raw_value = self.config.get('tweet_settings', {}).get(
            'quick_check_fallback_mode',
            'media',
        )
        mode = str(raw_value).strip().lower()
        aliases = {
            'off': 'none',
            'false': 'none',
            'disabled': 'none',
            'media_only': 'media',
            'full': 'replies_and_media',
            'all': 'replies_and_media',
            'true': 'replies_and_media',
        }
        mode = aliases.get(mode, mode)
        if mode not in {'none', 'media', 'replies_and_media'}:
            self.logger.warning(
                f"Invalid quick_check_fallback_mode={raw_value!r}; using 'media'"
            )
            return 'media'
        return mode

    def _get_consecutive_known_stop_count(self) -> int:
        """既知ツイートが連続したときに増分取得を打ち切る件数。"""
        raw_value = self.config.get('tweet_settings', {}).get('consecutive_known_stop_count', 20)
        try:
            count = max(1, int(raw_value))
        except (TypeError, ValueError):
            count = 20
        return count

    async def resolve_display_name(self, username: str) -> Optional[str]:
        """指定usernameの表示名を取得"""
        if not username:
            return None

        await self._initialize_accounts()
        try:
            user = await self.api.user_by_login(username)
        except Exception as exc:
            self.logger.warning(f"Failed to resolve @{username} via twscrape: {exc}")
            return None

        if not user:
            self.logger.warning(f"User @{username} not found while resolving display name")
            return None

        return user.displayname or username

    async def check_account_reachable(self, username: str, twitter_id: Optional[int] = None) -> bool:
        """軽量リチェック: user_by_login() でアカウントの存在を確認。
        twitter_id が指定されている場合、user_by_login() が失敗しても
        user_by_id() でフォールバック検索を行い、ID変更を検出する
        （twscrape 0.18.0以降は user_by_id 削除のためフォールバックなし）。
        """
        await self._initialize_accounts()
        try:
            # タイムアウト付きで呼び出す（twscrapeが全アカウントロック時に
            # 無限待機するのを防止し、到達可能扱いにする）
            user = await asyncio.wait_for(
                self.api.user_by_login(username),
                timeout=60,
            )
            if user is not None:
                # twitter_id をキャッシュ（初回取得時にCSVへ書き戻すため）
                self._resolved_twitter_ids[username.lower()] = user.id
                return True

            # user is None: twitter_id があれば user_by_id() でフォールバック
            # twscrape 0.18.0 で user_by_id は削除済み（X側エンドポイント廃止）。
            # 残っているバージョンでだけフォールバックを試す。
            if twitter_id is not None and hasattr(self.api, "user_by_id"):
                try:
                    user_by_id = await asyncio.wait_for(
                        self.api.user_by_id(twitter_id),
                        timeout=60,
                    )
                    if user_by_id is not None:
                        new_username = user_by_id.username
                        self._resolved_twitter_ids[new_username.lower()] = user_by_id.id
                        if new_username.lower() != username.lower():
                            # ID変更（スクリーンネーム変更）を検出
                            self.logger.warning(
                                f"ID変更検出: @{username} → @{new_username} "
                                f"(twitter_id={twitter_id}, display_name={user_by_id.displayname})"
                            )
                            self._detected_renames[username.lower()] = {
                                "old_username": username,
                                "new_username": new_username,
                                "twitter_id": twitter_id,
                                "display_name": user_by_id.displayname,
                            }
                        return True
                except asyncio.TimeoutError:
                    self.logger.warning(
                        f"user_by_id fallback timed out for @{username} (twitter_id={twitter_id})"
                    )
                except Exception as e:
                    self.logger.debug(
                        f"user_by_id fallback failed for @{username} (twitter_id={twitter_id}): {e}"
                    )

            # user_by_id でも見つからない or twitter_id 未指定: twscrape側の問題かアカウント削除かを区別
            pool_ok = await self._has_active_pool_accounts()
            if not pool_ok:
                self.logger.warning(
                    f"user_by_login returned None for @{username} but no active pool accounts. "
                    "Treating as reachable (pool issue, not account issue)."
                )
                return True
            return False  # プールは正常だがユーザーが見つからない → 本当に削除
        except asyncio.TimeoutError:
            self.logger.warning(
                f"Reachability check timed out for @{username} "
                "(twscrape may have locked all accounts). Treating as reachable."
            )
            return True
        except Exception as e:
            self.logger.debug(f"Reachability check failed for @{username}: {e}")
            # 例外は一時的エラーの可能性 → 到達可能扱い（保守的）
            return True

    def get_and_clear_detected_renames(self) -> Dict[str, Dict]:
        """検出されたID変更情報を取得してクリア"""
        renames = dict(self._detected_renames)
        self._detected_renames.clear()
        return renames

    def get_resolved_twitter_id(self, username: str) -> Optional[int]:
        """キャッシュからtwitter_idを取得（user_by_login成功時に保存される）"""
        return self._resolved_twitter_ids.get(username.lower())

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア（デーモンモード用）"""
        self._account_reachable.clear()
        self._quick_check_cache.clear()

    @staticmethod
    def _extract_account_sensitive_from_raw_user(raw_response: Any) -> Optional[bool]:
        """user_by_login_raw() から account-level possibly_sensitive を抜き出す。"""
        if raw_response is None:
            return None

        payload = raw_response
        if hasattr(raw_response, "json"):
            try:
                payload = raw_response.json()
            except Exception:
                return None

        if not isinstance(payload, dict):
            return None

        result = payload.get("data", {}).get("user", {}).get("result", {})
        if not isinstance(result, dict):
            return None

        legacy = result.get("legacy", {})
        if not isinstance(legacy, dict):
            return None

        value = legacy.get("possibly_sensitive")
        if isinstance(value, bool):
            return value
        return None

    async def _resolve_account_sensitive(self, username: str) -> bool:
        """tweet.user では落ちるため user_by_login_raw() から補完する。"""
        normalized_username = username.lower()
        cached = self._account_sensitive_cache.get(normalized_username)
        if cached is not None:
            return cached

        await self._initialize_accounts()

        account_sensitive = False
        try:
            raw_user = await self.api.user_by_login_raw(username)
            account_sensitive = bool(self._extract_account_sensitive_from_raw_user(raw_user))
        except Exception as exc:
            self.logger.debug(f"Failed to resolve account_sensitive for @{username}: {exc}")

        self._account_sensitive_cache[normalized_username] = account_sensitive
        return account_sensitive

    @staticmethod
    def _apply_account_sensitive(tweets: List[Dict[str, Any]], account_sensitive: bool) -> None:
        if not account_sensitive:
            return

        for tweet in tweets:
            tweet["account_sensitive"] = True

    async def _initialize_accounts(self):
        """Twitter認証アカウントを初期化"""
        if self._accounts_initialized:
            return
            
        try:
            # 既存のアカウントをチェック
            existing_accounts = await self.api.pool.accounts_info()
            existing_usernames = {acc['username'] for acc in existing_accounts}

            total_accounts = 0
            updated_accounts = 0

            # アカウント一覧を構築（優先順位: cookies/フォルダ → .env）
            accounts_to_configure: list[tuple[str, str, str]] = []

            # 1. まずcookies/フォルダからクレデンシャルを読み込み
            from .cookie_manager import CookieManager
            cookie_manager = CookieManager()
            cookie_credentials = cookie_manager.get_all_cookie_credentials()
            
            if cookie_credentials:
                self.logger.info(f"Loaded {len(cookie_credentials)} accounts from cookies/ folder")
                accounts_to_configure.extend(cookie_credentials)
            else:
                # 2. cookies/フォルダにファイルがない場合は.envにフォールバック
                self.logger.info("No cookie files found in cookies/ folder, falling back to .env")
                
                main_token = os.getenv('TWITTER_AUTH_TOKEN')
                main_ct0 = os.getenv('TWITTER_CT0')
                if main_token and main_ct0 and main_token != "your_auth_token_here":
                    accounts_to_configure.append(("twitter_main", main_token, main_ct0))

                account_index = 1
                while True:
                    token_key = f'TWITTER_ACCOUNT_{account_index}_TOKEN'
                    ct0_key = f'TWITTER_ACCOUNT_{account_index}_CT0'

                    token = os.getenv(token_key)
                    ct0 = os.getenv(ct0_key)

                    if not token or not ct0:
                        break

                    username = f"twitter_user_{account_index}"
                    accounts_to_configure.append((username, token, ct0))
                    account_index += 1

            desired_usernames = {user for user, _, _ in accounts_to_configure}

            # 設定されていないアカウントを削除（古いCookieの掃除）
            stale_usernames = sorted(existing_usernames - desired_usernames)
            if stale_usernames:
                try:
                    await self.api.pool.delete_accounts(stale_usernames)
                    self.logger.info(
                        "Removed %d stale Twitter account(s): %s",
                        len(stale_usernames),
                        ", ".join(stale_usernames)
                    )
                    existing_usernames -= set(stale_usernames)
                except Exception as cleanup_error:
                    self.logger.error(f"Failed to remove stale Twitter accounts: {cleanup_error}")

            for username, token, ct0 in accounts_to_configure:
                cookies = f"auth_token={token}; ct0={ct0}"

                if username not in existing_usernames:
                    await self.api.pool.add_account(
                        username=username,
                        password="dummy_password",
                        email=f"dummy_{username}@example.com",
                        email_password="dummy_email_password",
                        cookies=cookies
                    )
                    existing_usernames.add(username)
                    account_label = "main" if username == "twitter_main" else username
                    self.logger.info(f"Added Twitter account {account_label}")
                else:
                    try:
                        existing_account = await self.api.pool.get_account(username)
                    except Exception as load_error:
                        self.logger.warning(f"Could not load existing account {username}: {load_error}")
                        existing_account = None

                    if existing_account is not None:
                        try:
                            new_cookie_dict = parse_cookies(cookies)
                        except Exception as cookie_error:
                            self.logger.warning(f"Failed to parse cookies for {username}: {cookie_error}")
                            new_cookie_dict = {
                                "auth_token": token,
                                "ct0": ct0
                            }

                        stored_cookies = existing_account.cookies or {}
                        needs_cookie_update = any(
                            stored_cookies.get(key) != new_cookie_dict.get(key)
                            for key in ("auth_token", "ct0")
                        )

                        existing_csrf = None
                        if isinstance(existing_account.headers, dict):
                            existing_csrf = existing_account.headers.get("x-csrf-token")

                        if needs_cookie_update or not existing_account.active or (
                            existing_csrf and existing_csrf != new_cookie_dict.get("ct0")
                        ):
                            if not isinstance(existing_account.cookies, dict):
                                existing_account.cookies = {}
                            existing_account.cookies.update(new_cookie_dict)
                            if isinstance(existing_account.headers, dict):
                                existing_account.headers.pop("x-csrf-token", None)
                            else:
                                existing_account.headers = {}

                            existing_account.active = True
                            existing_account.error_msg = None
                            existing_account.locks = {}
                            existing_account.stats = {}
                            existing_account.last_used = None

                            try:
                                await self.api.pool.save(existing_account)
                                updated_accounts += 1
                                self.logger.info(f"Updated cookies for {username}")
                            except Exception as save_error:
                                self.logger.error(f"Failed to update stored cookies for {username}: {save_error}")
                        else:
                            self.logger.debug(f"Twitter account {username} cookies are up to date")

                total_accounts += 1

            # アカウントが1つも設定されていない場合はエラー
            if total_accounts == 0:
                raise ValueError("No Twitter accounts configured. Please set at least one account.")
            
            # すべてのアカウントでログイン
            await self.api.pool.login_all()
            
            # ログイン後、実際に利用可能なアカウントがあるか確認
            try:
                pool_stats = await self.api.pool.stats()
                self.logger.info(f"Initial pool stats: {pool_stats}")
                
                # 各アカウントの状態を確認（簡易的なテスト）
                accounts_info = await self.api.pool.accounts_info()
                active_count = sum(1 for acc in accounts_info if acc.get('active', False))
                available_count = sum(1 for acc in accounts_info 
                                     if acc.get('active', False) 
                                     and acc.get('locks', {}).get('UserTweets', 0) < time.time())
                self.logger.info(f"Active accounts: {active_count}/{len(accounts_info)}, Available for UserTweets: {available_count}")
                
            except Exception as e:
                self.logger.warning(f"Could not verify account status: {e}")
            
            self._accounts_initialized = True
            if updated_accounts:
                self.logger.info(f"Initialized {total_accounts} Twitter account(s) (updated {updated_accounts} stored cookie set(s))")
            else:
                self.logger.info(f"Initialized {total_accounts} Twitter account(s)")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Twitter accounts: {e}")
            raise
    
    def _is_retweet(self, tweet: Tweet, username: str) -> bool:
        """リツイート/リポストかどうかを判定"""
        # 方法1: retweetedTweet属性をチェック
        if hasattr(tweet, 'retweetedTweet') and tweet.retweetedTweet is not None:
            self.logger.debug(f"Tweet {tweet.id} is a retweet (has retweetedTweet)")
            return True
        
        # 方法2: ユーザーIDが異なる場合
        if hasattr(tweet, 'user') and hasattr(tweet.user, 'id'):
            if str(tweet.user.username).lower() != username.lower():
                self.logger.debug(f"Tweet {tweet.id} is a retweet (different user)")
                return True
        
        # 方法3: URLからユーザー名を抽出して比較
        if hasattr(tweet, 'url'):
            url_match = re.search(r'twitter\.com/([^/]+)/status/', tweet.url)
            if url_match:
                url_username = url_match.group(1).lower()
                if url_username != username.lower():
                    self.logger.debug(f"Tweet {tweet.id} is a retweet (URL mismatch)")
                    return True
        
        return False
    
    async def download_tweet_images(self, tweet_data: Dict[str, Any]) -> List[str]:
        """gallery-dlが全メディアを処理するため、この関数は不要"""
        return []
    
    async def download_tweet_videos(self, tweet_data: Dict[str, Any]) -> List[str]:
        """gallery-dlが全メディアを処理するため、この関数は不要"""
        return []
    
    async def _quick_check_single_endpoint(
        self,
        tweet_gen,
        username: str,
        endpoint_name: str,
        overlap_cutoff: Optional[datetime],
        max_checked: int,
        timeout_seconds: float = 10,
    ) -> Optional[bool]:
        """
        単一エンドポイントでの新着チェック内部実装。

        Returns:
            True  = 新着あり確定
            False = このエンドポイントでは新着なし
            None  = エラー発生（呼び出し元で判断）
        """
        start_time = time.time()
        checked = 0

        try:
            async for tweet in tweet_gen:
                if time.time() - start_time > timeout_seconds:
                    self.logger.warning(
                        f"Quick check [{endpoint_name}] timeout for @{username}, assuming new tweets exist"
                    )
                    return True

                # リツイートはクイックチェック対象から除外
                if self._is_retweet(tweet, username):
                    self.logger.debug(f"Quick check [{endpoint_name}]: Skipping retweet {tweet.id}")
                    continue

                checked += 1

                tweet_date = getattr(tweet, 'date', None)
                if overlap_cutoff and tweet_date and tweet_date < overlap_cutoff:
                    self.logger.info(
                        f"Quick check [{endpoint_name}]: Reached overlap cutoff for @{username} "
                        f"after checking {checked - 1} tweets"
                    )
                    return False

                # DBに存在するかチェック
                is_exists = False
                if self.db_manager:
                    is_exists = self.db_manager.check_tweet_exists(tweet.id)

                if not is_exists:
                    self.logger.info(
                        f"Quick check [{endpoint_name}]: New tweet detected for @{username} "
                        f"(ID: {tweet.id} is not in DB)"
                    )
                    return True

                self.logger.debug(f"Quick check [{endpoint_name}]: Tweet {tweet.id} already exists in DB")

                if checked >= max_checked:
                    self.logger.info(
                        f"Quick check [{endpoint_name}]: Checked latest {checked} tweets, all exist in DB"
                    )
                    return False

            self.logger.info(
                f"Quick check [{endpoint_name}]: Checked all available tweets (count: {checked}), "
                f"no new tweets found"
            )
            return False

        except Exception as e:
            self.logger.warning(f"Quick check [{endpoint_name}] error for @{username}: {e}")
            return None

    async def check_for_new_tweets(
        self,
        username: str,
        latest_tweet_id: str = None,
        latest_tweet_date: Optional[datetime] = None,
    ) -> bool:
        """
        新着ツイートがあるか簡易チェック（独立した機能）
        IDの大小ではなく「DBに存在しないか」で判定する。

        Twitter の UserTweets エンドポイントが特定ツイートを返さないケースがあるため、
        フォールバックとして UserTweetsAndReplies → UserMedia の順に追加チェックを行う。

        Args:
            username: Twitter username
            latest_tweet_id: DBに保存されている最新ツイートID（互換性のために残すが、判定には使用しない）
            latest_tweet_date: DBに保存されている最新ツイート日時

        Returns:
            新着ツイートがある場合True、ない場合False
        """
        await self._initialize_accounts()

        latest_date_key = latest_tweet_date.isoformat() if latest_tweet_date else None
        cache_key = (username.lower(), str(latest_tweet_id or ""), latest_date_key)
        if cache_key in self._quick_check_cache:
            cached = self._quick_check_cache[cache_key]
            self.logger.debug(f"Quick check: Reusing cached result for @{username}: {cached}")
            return cached

        def finish(result: bool) -> bool:
            self._quick_check_cache[cache_key] = result
            return result

        try:
            # ユーザー情報を取得（タイムアウト付き）
            try:
                user = await asyncio.wait_for(
                    self.api.user_by_login(username),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"Quick check: user_by_login timed out for @{username}, "
                    "treating as has new tweets (safe fallback)"
                )
                return finish(True)
            if not user:
                # プール障害時は全ユーザーがNoneになるため、プール状態を確認して安全側に倒す
                pool_ok = await self._has_active_pool_accounts()
                if not pool_ok:
                    self.logger.warning(
                        f"Quick check: User @{username} not found but no active pool accounts. "
                        "Treating as has new tweets (pool issue, not account issue)."
                    )
                    return finish(True)
                self.logger.error(f"Quick check: User @{username} not found (pool is healthy)")
                return finish(False)

            self.logger.info(f"Quick check: Checking for new tweets for @{username}")

            max_checked = self._get_quick_check_scan_count()
            if latest_tweet_date is None and self.db_manager:
                latest_tweet_date = self.db_manager.get_latest_tweet_date(username)
            overlap_cutoff = None
            if latest_tweet_date is not None:
                overlap_cutoff = latest_tweet_date - self._get_incremental_overlap_delta()

            # --- 第1チェック: UserTweets (通常タイムライン) ---
            result = await self._quick_check_single_endpoint(
                tweet_gen=self.api.user_tweets(user.id),
                username=username,
                endpoint_name="user_tweets",
                overlap_cutoff=overlap_cutoff,
                max_checked=max_checked,
            )
            if result is True:
                return finish(True)
            if result is None:
                # エラー時は安全のため新着ありとして扱う
                return finish(True)

            # --- フォールバック: UserTweetsが一部ツイートを返さない既知の問題への対策 ---
            # UserMedia は常にフォールバックとして実行する（画像付き投稿の見逃し防止）。
            # UserTweetsAndReplies は直近投稿があるアカウントのみ（API節約）。
            fallback_days = self.config.get('tweet_settings', {}).get(
                'quick_check_fallback_days', 7
            )
            is_recent_poster = (
                latest_tweet_date is not None
                and (datetime.now(timezone.utc) - latest_tweet_date).days <= fallback_days
            )
            fallback_timeout = 5  # フォールバックは短めのタイムアウト
            fallback_mode = self._get_quick_check_fallback_mode()

            if fallback_mode == 'replies_and_media' and is_recent_poster:
                # --- 第2チェック: UserTweetsAndReplies (リプライ含むタイムライン) ---
                # 直近投稿があるアカウントのみ実行（API節約）
                self.logger.debug(
                    f"Quick check: Trying user_tweets_and_replies fallback for @{username}"
                )
                result = await self._quick_check_single_endpoint(
                    tweet_gen=self.api.user_tweets_and_replies(user.id),
                    username=username,
                    endpoint_name="user_tweets_and_replies",
                    overlap_cutoff=overlap_cutoff,
                    max_checked=max_checked,
                    timeout_seconds=fallback_timeout,
                )
                if result is True:
                    self.logger.info(
                        f"Quick check: Fallback user_tweets_and_replies found new tweets "
                        f"for @{username}"
                    )
                    return finish(True)

            # --- 最終チェック: UserMedia (メディアタブ) ---
            # UserTweetsが画像付きツイートを返さないケースを必要に応じて補完する。
            # latest_tweet_dateがNone（DB未登録）の場合はスキップ。
            if fallback_mode in {'media', 'replies_and_media'} and latest_tweet_date is not None:
                self.logger.debug(
                    f"Quick check: Trying user_media fallback for @{username}"
                )
                result = await self._quick_check_single_endpoint(
                    tweet_gen=self.api.user_media(user.id),
                    username=username,
                    endpoint_name="user_media",
                    overlap_cutoff=overlap_cutoff,
                    max_checked=max_checked,
                    timeout_seconds=fallback_timeout,
                )
                if result is True:
                    self.logger.info(
                        f"Quick check: Fallback user_media found new tweets for @{username}"
                    )
                    return finish(True)

                self.logger.info(
                    f"Quick check: All endpoints confirm no new tweets for @{username}"
                )
            elif fallback_mode == 'none':
                self.logger.debug(
                    f"Quick check: Skipping fallback endpoints for @{username} "
                    "(quick_check_fallback_mode=none)"
                )
            else:
                self.logger.debug(
                    f"Quick check: Skipping all fallbacks for @{username} (no previous tweets in DB)"
                )
            return finish(False)

        except Exception as e:
            self.logger.error(f"Quick check failed for @{username}: {e}")
            # エラーの場合は安全のため新着ありとして扱う
            return finish(True)
    
    async def get_user_tweets_with_gallery_dl_first(self, username: str, days_lookback: int = 365, event_detection_enabled: bool = True) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        gallery-dl優先でツイートを取得

        Args:
            username: Twitter username
            days_lookback: 過去何日分を取得するか
            event_detection_enabled: このアカウントでイベント検知を行うか

        Returns:
            (全ツイート, イベント関連ツイート)のタプル
        """
        all_tweets = []
        all_event_tweets = []
        
        # 鍵アカウントかどうかを事前にチェック
        is_private_account = await self._check_if_private_account(username)

        # 到達不能アカウントはスキップ（gallery-dl/twscrapeの無駄な呼び出しを回避）
        if self._account_reachable.get(username.lower()) is False:
            self.logger.warning(f"User @{username} appears unreachable (deleted/suspended), skipping tweet fetch")
            return [], []

        account_sensitive = await self._resolve_account_sensitive(username)

        # Gallery-dlの有効性をチェック
        gallery_dl_config = self.config.get('tweet_settings', {}).get('gallery_dl', {})
        gallery_dl_enabled = gallery_dl_config.get('enabled', True)
        gallery_dl_force_full = gallery_dl_config.get('force_full_fetch', False)
        
        twscrape_config = self.config.get('tweet_settings', {}).get('twscrape', {})
        twscrape_enabled = twscrape_config.get('enabled', True)
        twscrape_force_full = twscrape_config.get('force_full_fetch', False)
        
        # DB から今回のクロール実行前の最新ツイート日時を記録（twscrape用）
        pre_crawl_latest_date = None
        pre_crawl_latest_id = None
        if self.db_manager:
            pre_crawl_latest_date = self.db_manager.get_latest_tweet_date(username)
            pre_crawl_latest_id = self.db_manager.get_latest_tweet_id(username)
        
        # 効率的な新着処理ロジック
        should_skip_all = False
        processed_with_adaptive_fetch = False

        # 新着チェック（force_full_fetchが無効で、両方のツールが有効、かつ既存データがある場合）
        if (not gallery_dl_force_full and not twscrape_force_full and
            gallery_dl_enabled and twscrape_enabled and pre_crawl_latest_id):

            # 新着チェック
            has_new_tweets = await self.check_for_new_tweets(
                username,
                pre_crawl_latest_id,
                pre_crawl_latest_date,
            )
            if not has_new_tweets:
                self.logger.info(f"No new tweets detected for @{username}, skipping all processing")
                should_skip_all = True
            else:
                self.logger.info(f"New tweets detected for @{username}, running adaptive chunk fetch (20 -> 20 -> unlimited)")

                chunk_size = 20
                chunk_counts: List[int] = []
                chunk_known_ids: Set[str] = set()
                combined_twscrape_tweets: List[Dict[str, Any]] = []
                gallery_full_required = False

                async def fetch_chunk(limit: Optional[int]) -> List[Dict[str, Any]]:
                    return await self._get_user_tweets_twscrape_only(
                        username,
                        days_lookback,
                        twscrape_force_full,
                        latest_date_override=pre_crawl_latest_date,
                        latest_id_override=pre_crawl_latest_id,
                        is_private_account=is_private_account,
                        limit_override=limit,
                        additional_existing_ids=chunk_known_ids if chunk_known_ids else None
                    )

                try:
                    # フェーズ1: 新規20件まで取得
                    first_chunk = await fetch_chunk(chunk_size)
                    chunk_counts.append(len(first_chunk))
                    if first_chunk:
                        combined_twscrape_tweets.extend(first_chunk)
                        chunk_known_ids.update(tweet['id'] for tweet in first_chunk)
                    first_full = len(first_chunk) >= chunk_size

                    # フェーズ2: さらに20件取得（フェーズ1が上限に到達した場合のみ）
                    second_full = False
                    if first_full:
                        second_chunk = await fetch_chunk(chunk_size)
                        chunk_counts.append(len(second_chunk))
                        if second_chunk:
                            combined_twscrape_tweets.extend(second_chunk)
                            chunk_known_ids.update(tweet['id'] for tweet in second_chunk)
                        second_full = len(second_chunk) >= chunk_size

                    # フェーズ3: 40件を超える場合は制限なしで残りを取得 + gallery-dl強制実行
                    if first_full and second_full:
                        unlimited_chunk = await fetch_chunk(None)
                        chunk_counts.append(len(unlimited_chunk))
                        if unlimited_chunk:
                            combined_twscrape_tweets.extend(unlimited_chunk)
                            chunk_known_ids.update(tweet['id'] for tweet in unlimited_chunk)
                            gallery_full_required = True

                    if combined_twscrape_tweets:
                        processed_with_adaptive_fetch = True
                        all_tweets.extend(combined_twscrape_tweets)
                        phase_summary = ', '.join(
                            f"chunk{idx+1}:{count}" if idx < 2 else f"unlimited:{count}"
                            for idx, count in enumerate(chunk_counts)
                        )
                        self.logger.info(
                            f"Adaptive twscrape retrieved {len(combined_twscrape_tweets)} tweets for @{username} ({phase_summary})"
                        )
                    else:
                        self.logger.warning(
                            f"Adaptive twscrape expected new tweets for @{username} but received none; falling back"
                        )

                    if processed_with_adaptive_fetch and gallery_full_required:
                        if gallery_dl_enabled:
                            self.logger.info(
                                f"More than 40 new tweets detected for @{username}; triggering full gallery-dl fetch"
                            )
                            try:
                                gallery_tweets, gallery_event_tweets = await self.gallery_dl_extractor.fetch_and_analyze_tweets(
                                    username,
                                    event_detection_enabled=event_detection_enabled,
                                    is_private_account=is_private_account
                                )

                                if gallery_tweets:
                                    existing_ids = {tweet['id'] for tweet in all_tweets}
                                    new_gallery_tweets = [
                                        tweet for tweet in gallery_tweets
                                        if tweet['id'] not in existing_ids
                                    ]
                                    all_tweets.extend(new_gallery_tweets)
                                    self.logger.info(
                                        f"Gallery-dl retrieved {len(new_gallery_tweets)} additional media tweets for @{username}"
                                    )

                                if gallery_event_tweets:
                                    all_event_tweets.extend(gallery_event_tweets)
                                    self.logger.info(
                                        f"Gallery-dl found {len(gallery_event_tweets)} event tweets for @{username}"
                                    )
                            except Exception as e:
                                self.logger.error(f"Gallery-dl failed for @{username}: {e}")
                        else:
                            self.logger.info(
                                f"Gallery-dl disabled, skipping media fetch even though @{username} has >40 new tweets"
                            )

                except Exception as e:
                    processed_with_adaptive_fetch = False
                    self.logger.error(f"Adaptive fetch failed for @{username}: {e}")

        # 簡易新着チェック（複雑なロジックがスキップされた場合）
        if (not should_skip_all and not processed_with_adaptive_fetch and
            not gallery_dl_force_full and not twscrape_force_full and
            pre_crawl_latest_id and (gallery_dl_enabled or twscrape_enabled)):
            # 既存データがあり、どちらかのツールが有効で、複雑なロジックがスキップされた場合
            has_new_tweets = await self.check_for_new_tweets(
                username,
                pre_crawl_latest_id,
                pre_crawl_latest_date,
            )
            if not has_new_tweets:
                self.logger.info(f"Simple check: No new tweets detected for @{username}, skipping all processing")
                should_skip_all = True

        # フォールバック処理（従来のロジック）
        # DBにデータがない場合や、force_full_fetchが有効な場合、ツールが有効な場合は処理する
        if (not should_skip_all and not processed_with_adaptive_fetch and
            (gallery_dl_force_full or twscrape_force_full or pre_crawl_latest_id is None or (gallery_dl_enabled or twscrape_enabled))):
            # 1. Gallery-dlでメディア付きツイートを優先取得
            if gallery_dl_enabled:
                should_fetch_gallery = True
                if not gallery_dl_force_full and pre_crawl_latest_id:
                    has_new_tweets = await self.check_for_new_tweets(
                        username,
                        pre_crawl_latest_id,
                        pre_crawl_latest_date,
                    )
                    self.logger.info(f"Gallery-dl new tweets check for @{username}: {has_new_tweets}")
                    if not has_new_tweets:
                        self.logger.info(f"Skipping gallery-dl for @{username} (no new tweets detected)")
                        should_fetch_gallery = False

                if should_fetch_gallery:
                    self.logger.info(f"Fetching media tweets with gallery-dl for @{username}")
                    try:
                        gallery_tweets, gallery_event_tweets = await self.gallery_dl_extractor.fetch_and_analyze_tweets(
                            username,
                            event_detection_enabled=event_detection_enabled,
                            is_private_account=is_private_account
                        )

                        if gallery_tweets:
                            all_tweets.extend(gallery_tweets)
                            self.logger.info(f"Gallery-dl retrieved {len(gallery_tweets)} media tweets for @{username}")

                        if gallery_event_tweets:
                            all_event_tweets.extend(gallery_event_tweets)
                            self.logger.info(f"Gallery-dl found {len(gallery_event_tweets)} event tweets for @{username}")

                    except Exception as e:
                        self.logger.error(f"Gallery-dl failed for @{username}: {e}")

            # 2. twscrapeでテキストのみツイートを補完取得
            if twscrape_enabled:
                should_fetch_twscrape = True
                if not twscrape_force_full and pre_crawl_latest_id:
                    has_new_tweets = await self.check_for_new_tweets(
                        username,
                        pre_crawl_latest_id,
                        pre_crawl_latest_date,
                    )
                    self.logger.info(f"Twscrape new tweets check for @{username}: {has_new_tweets}")
                    if not has_new_tweets:
                        self.logger.info(f"Skipping twscrape for @{username} (no new tweets detected)")
                        should_fetch_twscrape = False

                if should_fetch_twscrape:
                    self.logger.info(f"Fetching remaining tweets with twscrape for @{username}")
                    try:
                        twscrape_tweets = await self._get_user_tweets_twscrape_only(
                            username,
                            days_lookback,
                            twscrape_force_full,
                            latest_date_override=pre_crawl_latest_date,
                            latest_id_override=pre_crawl_latest_id,
                            is_private_account=is_private_account
                        )

                        if twscrape_tweets:
                            gallery_tweet_ids = {tweet['id'] for tweet in all_tweets}
                            merged_tweets = self.gallery_dl_extractor.merge_with_twscrape(
                                all_tweets,
                                twscrape_tweets,
                            )
                            new_twscrape_count = sum(
                                1 for tweet in twscrape_tweets if tweet['id'] not in gallery_tweet_ids
                            )

                            all_tweets = merged_tweets
                            self.logger.info(
                                f"twscrape added {new_twscrape_count} additional tweets for @{username} "
                                f"(filtered {len(twscrape_tweets) - new_twscrape_count} duplicates)"
                            )

                    except Exception as e:
                        self.logger.error(f"twscrape failed for @{username}: {e}")
            else:
                self.logger.info("twscrape is disabled, skipping text-only tweet fetching")
        
        # 日付でソート（新しい順）
        self._apply_account_sensitive(all_tweets, account_sensitive)
        self._apply_account_sensitive(all_event_tweets, account_sensitive)
        all_tweets.sort(key=lambda x: x['date'], reverse=True)
        all_event_tweets.sort(key=lambda x: x['date'], reverse=True)
        
        self.logger.info(f"Total tweets retrieved for @{username}: {len(all_tweets)} (including {len(all_event_tweets)} event tweets)")
        
        return all_tweets, all_event_tweets
    
    async def _check_if_private_account(self, username: str) -> bool:
        """ユーザーが鍵アカウントかどうかをチェック"""
        normalized_username = username.lower()
        try:
            await self._initialize_accounts()
            # タイムアウト付きで呼び出す（twscrapeが全アカウントロック時に
            # 無限待機するのを防止する）
            user = await asyncio.wait_for(
                self.api.user_by_login(username),
                timeout=60,
            )
            if user and hasattr(user, 'protected'):
                self._account_reachable[normalized_username] = True
                is_private = bool(user.protected)
                self._private_account_cache[normalized_username] = is_private
                if is_private:
                    self.logger.info(f"User @{username} is a protected (private) account")
                else:
                    self.logger.debug(f"User @{username} is public")
                return is_private  # Noneの場合もFalseにする
            else:
                # user is None だが、twscrape側の問題かアカウント削除かを区別する
                # アクティブなcookieアカウントがなければtwscrape側の問題
                pool_ok = await self._has_active_pool_accounts()
                if pool_ok:
                    # APIリクエストが成功した上でNone → 本当に削除/凍結
                    self._account_reachable[normalized_username] = False
                    self._private_account_cache[normalized_username] = False
                else:
                    # twscrapeアカウント不足 → 判定を保留（誤フラグ防止）
                    self.logger.warning(
                        f"user_by_login returned None for @{username} but no active pool accounts. "
                        "Skipping reachability judgment."
                    )
                    self._private_account_cache.setdefault(normalized_username, False)
        except asyncio.TimeoutError:
            self.logger.warning(
                f"Private account check timed out for @{username} "
                "(twscrape may have locked all accounts). Skipping reachability judgment."
            )
            self._private_account_cache.setdefault(normalized_username, False)
            # タイムアウトは一時的エラー → reachable は設定しない（誤フラグ防止）
        except Exception as e:
            self.logger.debug(f"Could not check if @{username} is private: {e}")
            self._private_account_cache.setdefault(normalized_username, False)
            # 例外は一時的エラーの可能性 → reachable は設定しない
        return False

    async def _has_active_pool_accounts(self) -> bool:
        """twscrapeにアクティブなアカウントが1つ以上あるか"""
        try:
            accounts = await self.api.pool.accounts_info()
            return any(acc.get("active", False) for acc in accounts)
        except Exception:
            return False

    def is_account_private(self, username: str) -> bool:
        """直近の鍵アカウント判定結果を返す"""
        return self._private_account_cache.get(username.lower(), False)
    
    async def _get_user_tweets_twscrape_only(
        self,
        username: str,
        days_lookback: int = 365,
        force_full_fetch: bool = False,
        latest_date_override=None,
        latest_id_override=None,
        is_private_account: bool = False,
        limit_override: Optional[int] = None,
        additional_existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        twscrapeのみでツイートを取得（gallery-dl優先処理用）
        
        Args:
            latest_date_override: 効率化のため外部から指定された最新日時
            latest_id_override: 効率化のため外部から指定された最新ID
            additional_existing_ids: 直前のフェッチで既に取得済みのツイートID集合
        """
        # リトライ処理（最大3回）
        max_retries = 3
        retry_count = 0
        use_specific_account = is_private_account  # 鍵アカウントなら最初から指定アカウントを使用
        
        if is_private_account:
            await self._use_specific_twscrape_account()
        
        while retry_count < max_retries:
            try:
                return await self._get_user_tweets_twscrape_internal(
                    username, days_lookback, force_full_fetch,
                    latest_date_override, latest_id_override,
                    use_specific_account=use_specific_account,
                    limit_override=limit_override,
                    additional_existing_ids=additional_existing_ids
                )
            except TimeoutError as e:
                retry_count += 1
                if retry_count < max_retries:
                    self.logger.warning(f"twscrape: Timeout for @{username}, retry {retry_count}/{max_retries}")
                    
                    # アカウントローテーションを試行
                    try:
                        await self._rotate_account()
                        self.logger.info(f"twscrape: Rotated to next account for retry {retry_count}")
                    except Exception as rotate_error:
                        self.logger.warning(f"twscrape: Failed to rotate account: {rotate_error}")
                    
                    await asyncio.sleep(10 * retry_count)  # 10秒, 20秒, 30秒
                else:
                    self.logger.error(f"twscrape: Max retries reached for @{username}")
                    raise e
            except Exception as e:
                self.logger.error(f"twscrape: Error for @{username}: {e}")
                return []
                
        return []
    
    async def _use_specific_twscrape_account(self):
        """鍵アカウント用の指定twscrapeアカウントを使用"""
        import os
        private_config = self.config.get('tweet_settings', {}).get('private_account_cookies', {})
        account_num = private_config.get('twscrape_account', 14)
        
        # 環境変数から指定アカウントの情報を取得
        auth_token = os.getenv(f'TWITTER_ACCOUNT_{account_num}_TOKEN')
        ct0 = os.getenv(f'TWITTER_ACCOUNT_{account_num}_CT0')
        
        if auth_token and ct0:
            self.logger.info(f"Using specific twscrape account {account_num} for private account")
            # アカウントプールをクリアして指定アカウントのみ追加
            await self.api.pool.reset_locks()
            # 特定アカウントを優先的に使用するようにマーク
            # (twscrapeの内部実装によってはこの部分の調整が必要)
        else:
            self.logger.warning(f"Specific twscrape account {account_num} not configured in .env")
    
    async def _get_user_tweets_twscrape_internal(
        self,
        username: str,
        days_lookback: int = 365,
        force_full_fetch: bool = False,
        latest_date_override=None,
        latest_id_override=None,
        use_specific_account: bool = False,
        limit_override: Optional[int] = None,
        additional_existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        twscrapeの内部実装（タイムアウトエラーを投げる）
        """
        # 既存のget_user_tweetsロジックを流用し、DB取得部分のみoverride値を使用
        await self._initialize_accounts()
        
        tweets = []
        since_date = datetime.now(timezone.utc) - timedelta(days=days_lookback)
        
        # データベース接続と既存データの確認
        from .database import DatabaseManager
        db_manager = DatabaseManager(self.config)
        
        # 既存のツイートIDセットを取得（重複チェック用）
        existing_tweet_ids = set()
        if db_manager:
            existing_tweet_ids = db_manager.get_existing_tweet_ids(username)
            self.logger.debug(f"Found {len(existing_tweet_ids)} existing tweets in database for @{username}")
        if additional_existing_ids:
            existing_tweet_ids.update(additional_existing_ids)
            self.logger.debug(
                f"Added {len(additional_existing_ids)} adaptive-known tweets to skip list for @{username}"
            )
        
        # override値があればそれを使用、なければDBから取得
        latest_tweet_date = latest_date_override
        latest_tweet_id = latest_id_override
        
        if latest_tweet_date is None and not force_full_fetch:
            latest_tweet_date = db_manager.get_latest_tweet_date(username)
            latest_tweet_id = db_manager.get_latest_tweet_id(username)
        
        # force_full_fetchがfalseで既存データがある場合、新着チェックを実行
        should_fetch_tweets = True
        if not force_full_fetch and latest_tweet_id is not None:
            # 独立した新着チェック機能を使用
            has_new_tweets = await self.check_for_new_tweets(
                username,
                latest_tweet_id,
                latest_tweet_date,
            )
            if not has_new_tweets:
                self.logger.info(f"twscrape: Skipping fetch for @{username} (no new tweets detected)")
                return []  # 新着がない場合は早期リターン
        
        if not force_full_fetch and latest_tweet_date:
            overlap_start = latest_tweet_date - self._get_incremental_overlap_delta()
            since_date = max(since_date, overlap_start)
            self.logger.info(
                f"twscrape: Found existing tweets for @{username}, fetching since {since_date.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(with overlap from {latest_tweet_date.strftime('%Y-%m-%d %H:%M:%S')})"
            )
            if latest_tweet_id:
                self.logger.debug(f"twscrape: Latest tweet ID: {latest_tweet_id}, will check for new tweets only")
        elif not force_full_fetch:
            self.logger.info(f"twscrape: No existing tweets for @{username}, fetching since {since_date.strftime('%Y-%m-%d')}")
        else:
            self.logger.info(f"twscrape: Force full fetch enabled for @{username}, fetching ALL tweets since {since_date.strftime('%Y-%m-%d')} with duplicate checking")
        
        # collected_tweets処理を削除（簡素化）
        
        try:
            # アカウント利用可能性をチェック（簡易版）
            try:
                pool_stats = await self.api.pool.stats()
                self.logger.debug(f"twscrape: Account pool stats: {pool_stats}")
                
                # アクティブなアカウントが1つもない場合のみ早期リターン
                accounts_info = await self.api.pool.accounts_info()
                active_accounts = [acc for acc in accounts_info if acc.get('active', False)]
                if not active_accounts:
                    self.logger.error(f"twscrape: No active accounts found. All {len(accounts_info)} accounts are invalid.")
                    return []
                
                # 利用可能なアカウント数を確認（情報のみ）
                available_now = [acc for acc in active_accounts if acc.get('locks', {}).get('UserTweets', 0) < time.time()]
                if not available_now:
                    self.logger.info(f"twscrape: All {len(active_accounts)} active accounts are rate-limited. Will wait for next available slot.")
                else:
                    self.logger.debug(f"twscrape: {len(available_now)}/{len(active_accounts)} accounts available now")
                    
            except Exception as e:
                self.logger.warning(f"twscrape: Could not check pool stats: {e}")
            
            # First, resolve username to user ID
            user = await self.api.user_by_login(username)
            if not user:
                self.logger.error(f"twscrape: User @{username} not found or suspended")
                return []
            
            display_name = user.displayname
            account_sensitive = await self._resolve_account_sensitive(username)
            self.logger.info(f"twscrape: Resolved @{username} to user ID: {user.id} (Name: {display_name})")
            
            tweet_count = 0
            total_fetched = 0
            old_tweets_count = 0
            consecutive_old_tweets = 0
            max_consecutive_old = 20
            consecutive_known_tweets = 0
            max_consecutive_known = self._get_consecutive_known_stop_count()
            
            # kvパラメータで日付フィルタリング
            kv = None
            if latest_tweet_date and not force_full_fetch:
                kv = {"since_time": latest_tweet_date.isoformat()}
                self.logger.debug(f"twscrape: Using kv parameter: {kv}")
            
            # 新着チェックは既に実施済みなので、ここでは通常取得を実行
            self.logger.info(f"twscrape: Fetching tweets for @{username} (new tweets confirmed)")
            
            start_time = time.time()

            # イテレータ自体にタイムアウトを設定
            tweet_iterator = self.api.user_tweets(user.id).__aiter__()
            
            while True:
                try:
                    # 各ツイート取得に30秒のタイムアウトを設定
                    tweet = await asyncio.wait_for(tweet_iterator.__anext__(), timeout=30.0)
                    
                    # 全体のタイムアウトチェック
                    if time.time() - start_time > self._timeout_seconds:
                        self.logger.error(f"twscrape: Overall timeout after {self._timeout_seconds}s while fetching tweets for @{username}")
                        raise TimeoutError(f"Overall timeout while fetching tweets for @{username}")
                    
                    total_fetched += 1
                    self.logger.debug(f"twscrape: Tweet {total_fetched}: ID={tweet.id}, Date={tweet.date}")
                    
                    # 日付チェック
                    if tweet.date < since_date:
                        old_tweets_count += 1
                        consecutive_old_tweets += 1
                        self.logger.debug(f"twscrape: Skipping old tweet: {tweet.id}")
                        
                        if not force_full_fetch and consecutive_old_tweets >= max_consecutive_old:
                            self.logger.debug(f"twscrape: Reached {max_consecutive_old} consecutive old tweets, stopping")
                            break
                        continue
                    else:
                        consecutive_old_tweets = 0
                    
                    # リツイートをスキップ
                    if self._is_retweet(tweet, username):
                        self.logger.debug(f"twscrape: Skipping retweet: {tweet.id}")
                        continue
                    
                    # 既存ツイートとの重複チェック
                    tweet_id_str = str(tweet.id)
                    if tweet_id_str in existing_tweet_ids:
                        self.logger.debug(f"twscrape: Skipping duplicate tweet: {tweet.id}")
                        consecutive_known_tweets += 1
                        if not force_full_fetch and consecutive_known_tweets >= max_consecutive_known:
                            self.logger.debug(
                                f"twscrape: Reached {max_consecutive_known} consecutive known tweets, stopping"
                            )
                            break
                        continue
                    consecutive_known_tweets = 0
                    
                    # ツイートデータを抽出
                    tweet_data = {
                        'id': str(tweet.id),
                        'text': tweet.rawContent,
                        'date': tweet.date.isoformat(),
                        'url': f"https://twitter.com/{username}/status/{tweet.id}",
                        'username': username,
                        'media': [],
                        'videos': [],
                        'sensitive': bool(getattr(tweet, 'possibly_sensitive', None) or False),
                        'account_sensitive': account_sensitive,
                    }
                    
                    # メディア（画像）URLを抽出
                    if hasattr(tweet, 'media') and hasattr(tweet.media, 'photos'):
                        tweet_data['media'] = [photo.url for photo in tweet.media.photos]
                    
                    # 動画URLを抽出
                    if hasattr(tweet, 'media') and hasattr(tweet.media, 'videos'):
                        for video in tweet.media.videos:
                            best_variant = None
                            best_bitrate = 0
                            
                            if hasattr(video, 'variants'):
                                for variant in video.variants:
                                    if hasattr(variant, 'bitrate') and variant.bitrate:
                                        if variant.bitrate > best_bitrate:
                                            best_bitrate = variant.bitrate
                                            best_variant = variant
                            
                            if best_variant and hasattr(best_variant, 'url'):
                                tweet_data['videos'].append(best_variant.url)
                            elif hasattr(video, 'url'):
                                tweet_data['videos'].append(video.url)
                    
                    tweets.append(tweet_data)
                    tweet_count += 1

                    # limit_overrideが指定されている場合、その件数に達したら終了
                    if limit_override is not None and tweet_count >= limit_override:
                        self.logger.info(f"twscrape: Reached limit_override ({limit_override}), stopping fetch for @{username}")
                        break

                    # ツイート取得成功時にリトライカウンタをリセット
                    self._tweet_retry_count = 0
                    self._http_retry_count = 0
                    
                    if tweet_count % 100 == 0:
                        self.logger.debug(f"twscrape: Fetched {tweet_count} tweets so far...")
                        
                except StopAsyncIteration:
                    # イテレータ終了
                    self.logger.debug(f"twscrape: Reached end of tweets for @{username}")
                    break
                    
                except asyncio.TimeoutError:
                    # 個別ツイート取得のタイムアウト - ローテーションしてリトライ
                    self.logger.warning(f"twscrape: Tweet fetch timeout after 30s")
                    
                    # 個別ツイートレベルでのリトライ（最大2回）
                    tweet_retry_count = getattr(self, '_tweet_retry_count', 0)
                    if tweet_retry_count < 2:
                        self._tweet_retry_count = tweet_retry_count + 1
                        self.logger.info(f"twscrape: Individual tweet retry {self._tweet_retry_count}/2")
                        
                        # アカウントローテーション
                        try:
                            await self._rotate_account()
                            self.logger.info(f"twscrape: Rotated account for individual tweet retry")
                        except Exception as rotate_error:
                            self.logger.warning(f"twscrape: Failed to rotate account for tweet retry: {rotate_error}")
                        
                        # 短時間待機後にリトライ（同じツイートを再試行）
                        await asyncio.sleep(5)
                        continue
                    else:
                        # 最大リトライ数に達したら次のツイートに進む
                        self.logger.warning(f"twscrape: Max individual tweet retries reached, skipping to next tweet")
                        self._tweet_retry_count = 0  # カウンタリセット
                        continue
                    
                except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                    # HTTPタイムアウト - ローテーションしてリトライ
                    self.logger.warning(f"twscrape: HTTP timeout: {e}")
                    
                    # HTTPレベルでのリトライ（最大2回）
                    http_retry_count = getattr(self, '_http_retry_count', 0)
                    if http_retry_count < 2:
                        self._http_retry_count = http_retry_count + 1
                        self.logger.info(f"twscrape: HTTP retry {self._http_retry_count}/2")
                        
                        # アカウントローテーション
                        try:
                            await self._rotate_account()
                            self.logger.info(f"twscrape: Rotated account for HTTP retry")
                        except Exception as rotate_error:
                            self.logger.warning(f"twscrape: Failed to rotate account for HTTP retry: {rotate_error}")
                        
                        # 指数バックオフで待機
                        await asyncio.sleep(2 ** self._http_retry_count)  # 2秒, 4秒
                        continue
                    else:
                        # 最大リトライ数に達したら次のツイートに進む
                        self.logger.warning(f"twscrape: Max HTTP retries reached, skipping to next tweet") 
                        self._http_retry_count = 0  # カウンタリセット
                        continue
                        
                except Exception as e:
                    # その他のエラー
                    self.logger.warning(f"twscrape: Error processing tweet: {e}")
                    continue
            
            # 重複を除去
            unique_tweets = []
            seen_ids = set()
            for tweet in tweets:
                if tweet['id'] not in seen_ids:
                    unique_tweets.append(tweet)
                    seen_ids.add(tweet['id'])
            
            duplicate_count = len(tweets) - len(unique_tweets)
            if duplicate_count > 0:
                self.logger.warning(f"twscrape: Removed {duplicate_count} duplicate tweets")
            
            self.logger.info(f"twscrape: Fetched {len(unique_tweets)} unique tweets for @{username} (examined: {total_fetched}, old skipped: {old_tweets_count})")
            return unique_tweets
            
        except Exception as e:
            if "No account available" in str(e):
                self.logger.warning(f"twscrape: Rate limit reached for @{username}: {e}")
                # 次の利用可能時間を抽出してログ出力
                next_available_match = re.search(r'Next available at (\d{2}:\d{2}:\d{2})', str(e))
                if next_available_match:
                    next_time = next_available_match.group(1)
                    self.logger.info(f"twscrape: Next available time: {next_time}")
            else:
                self.logger.error(f"twscrape: Error fetching tweets for @{username}: {e}")
            # タイムアウトエラーは再スロー
            if isinstance(e, TimeoutError):
                raise e
            return []
    
    async def get_user_tweets(self, username: str, days_lookback: int = 365, force_full_fetch: bool = False) -> List[Dict[str, Any]]:
        """指定ユーザーのツイートを取得（リツイート除く）"""
        await self._initialize_accounts()
        
        tweets = []
        since_date = datetime.now(timezone.utc) - timedelta(days=days_lookback)
        
        # データベース接続と既存データの確認
        from .database import DatabaseManager
        db_manager = DatabaseManager(self.config)
        
        # 既存のツイートIDセットを取得（重複チェック用）
        existing_tweet_ids = set()
        if db_manager:
            existing_tweet_ids = db_manager.get_existing_tweet_ids(username)
            self.logger.debug(f"Found {len(existing_tweet_ids)} existing tweets in database for @{username}")
        
        # データベースから該当ユーザーの最新ツイート日付とIDを取得
        latest_tweet_date = None
        latest_tweet_id = None
        
        if not force_full_fetch:
            latest_tweet_date = db_manager.get_latest_tweet_date(username)
            latest_tweet_id = db_manager.get_latest_tweet_id(username)
            
            if latest_tweet_date:
                # 最新ツイート日付以降のみ取得（効率化）
                since_date = latest_tweet_date
                self.logger.info(f"Found existing tweets for @{username}, fetching since {since_date.strftime('%Y-%m-%d %H:%M:%S')}")
                if latest_tweet_id:
                    self.logger.debug(f"Latest tweet ID: {latest_tweet_id}")
            else:
                self.logger.info(f"No existing tweets for @{username}, fetching since {since_date.strftime('%Y-%m-%d')}")
        else:
            self.logger.info(f"Force full fetch enabled for @{username}, fetching ALL tweets since {since_date.strftime('%Y-%m-%d')} with duplicate checking")
        
        try:
            # アカウント利用可能性をチェック（簡易版）
            try:
                pool_stats = await self.api.pool.stats()
                self.logger.debug(f"Account pool stats: {pool_stats}")
                
                # アクティブなアカウントが1つもない場合のみ早期リターン
                accounts_info = await self.api.pool.accounts_info()
                active_accounts = [acc for acc in accounts_info if acc.get('active', False)]
                if not active_accounts:
                    self.logger.error(f"No active accounts found. All {len(accounts_info)} accounts are invalid.")
                    return []
                
                # 利用可能なアカウント数を確認（情報のみ）
                available_now = [acc for acc in active_accounts if acc.get('locks', {}).get('UserTweets', 0) < time.time()]
                if not available_now:
                    self.logger.info(f"All {len(active_accounts)} active accounts are rate-limited. Will wait for next available slot.")
                else:
                    self.logger.debug(f"{len(available_now)}/{len(active_accounts)} accounts available now")
                    
            except Exception as e:
                self.logger.warning(f"Could not check pool stats: {e}")
            
            # First, resolve username to user ID
            user = await self.api.user_by_login(username)
            if not user:
                self.logger.error(f"User @{username} not found or suspended")
                return []
            
            display_name = user.displayname  # display_nameを設定
            account_sensitive = await self._resolve_account_sensitive(username)
            self.logger.info(f"Resolved @{username} to user ID: {user.id} (Name: {display_name})")
            self.logger.info(f"@{username} の総ツイート数: {user.statusesCount}")
            
            tweet_count = 0
            total_fetched = 0
            old_tweets_count = 0
            consecutive_old_tweets = 0
            max_consecutive_old = 20  # 連続して古いツイートが20個続いたら終了
            
            # APIからは必要な分だけ取得（日付でフィルタリング）
            self.logger.debug(f"Starting to fetch tweets")
            
            # twscrapeのkvパラメータで日付フィルタリングを試験的に実装
            kv = None
            if latest_tweet_date and not force_full_fetch:
                # ISO形式の日時文字列でフィルタリング（試験的）
                kv = {"since_time": latest_tweet_date.isoformat()}
                self.logger.debug(f"Trying to filter tweets with kv parameter: {kv}")
            
            # limitの設定
            limit = -1  # デフォルトは-1（無制限）
            if not force_full_fetch:
                if latest_tweet_date and (datetime.now(timezone.utc) - latest_tweet_date).days < 7:
                    limit = 50  # 1週間以内に更新があった場合は50件まで
                    self.logger.debug(f"Recent update detected, limiting fetch to {limit} tweets")
                elif latest_tweet_date and (datetime.now(timezone.utc) - latest_tweet_date).days < 30:
                    limit = 100  # 1ヶ月以内の場合は100件まで
                    self.logger.debug(f"Recent month update detected, limiting fetch to {limit} tweets")
                else:
                    # 初回取得や古いデータの場合は無制限
                    limit = -1
                    self.logger.info(f"No recent updates, fetching all available tweets (limit=-1)")
            else:
                # force_full_fetchの場合は明示的に-1（無制限）を設定
                limit = -1
                self.logger.info(f"Force full fetch enabled, no limit set (fetching all available tweets)")
                
                # force_full_fetchの場合、kvパラメータをクリア（日付フィルタを無効化）
                kv = None
                self.logger.info(f"Clearing date filters for complete fetch")
            
            # force_full_fetchの場合、アカウントプールの状態を定期的に確認
            check_interval = 500  # 500ツイートごとにチェック
            
            start_time = time.time()
            async for tweet in self.api.user_tweets(user.id):
                # タイムアウトチェック
                if time.time() - start_time > self._timeout_seconds:
                    self.logger.error(f"Timeout after {self._timeout_seconds}s while fetching tweets for @{username}")
                    break
                    
                total_fetched += 1
                self.logger.debug(f"Tweet {total_fetched}: ID={tweet.id}, Date={tweet.date}, Username={getattr(tweet.user, 'username', 'N/A')}")
                
                # force_full_fetchの場合、定期的にアカウントプールの状態を確認
                if force_full_fetch and total_fetched % check_interval == 0:
                    try:
                        pool_stats = await self.api.pool.stats()
                        self.logger.info(f"Account pool stats after {total_fetched} tweets: {pool_stats}")
                    except:
                        pass
                
                # Tweet IDベースの早期終了（force_full_fetchが無効な場合のみ）
                if not force_full_fetch and latest_tweet_id and int(tweet.id) <= int(latest_tweet_id):
                    self.logger.debug(f"Reached known tweet {tweet.id}, stopping immediately")
                    break
                
                # 日付チェック - 古いツイートはスキップするが即座に終了はしない
                if tweet.date < since_date:
                    old_tweets_count += 1
                    consecutive_old_tweets += 1
                    self.logger.debug(f"Skipping old tweet: {tweet.id} (older than {days_lookback} days)")
                    
                    # force_full_fetchが無効な場合のみ、連続して古いツイートが続く場合に終了
                    if not force_full_fetch and consecutive_old_tweets >= max_consecutive_old:
                        self.logger.debug(f"Reached {max_consecutive_old} consecutive old tweets, stopping")
                        break
                    continue
                else:
                    consecutive_old_tweets = 0  # 新しいツイートが見つかったらリセット
                
                # リツイートをスキップ
                if self._is_retweet(tweet, username):
                    self.logger.debug(f"Skipping retweet: {tweet.id} (retweetedTweet: {hasattr(tweet, 'retweetedTweet') and tweet.retweetedTweet is not None})")
                    continue
                
                # 既存ツイートとの重複チェック（force_full_fetch時も実行）
                tweet_id_str = str(tweet.id)
                if tweet_id_str in existing_tweet_ids:
                    self.logger.debug(f"Skipping duplicate tweet: {tweet.id} (already in database)")
                    continue
                
                # ツイートデータを抽出
                tweet_data = {
                    'id': str(tweet.id),
                    'text': tweet.rawContent,
                    'date': tweet.date.isoformat(),
                    'url': f"https://twitter.com/{username}/status/{tweet.id}",
                    'username': username,  # Store the username for later use
                    'media': [],
                    'videos': [],  # 動画URLを格納
                    'sensitive': bool(getattr(tweet, 'possibly_sensitive', None) or False),
                    'account_sensitive': account_sensitive,
                }
                
                # メディア（画像）URLを抽出
                if hasattr(tweet, 'media') and hasattr(tweet.media, 'photos'):
                    tweet_data['media'] = [photo.url for photo in tweet.media.photos]
                
                # 動画URLを抽出
                if hasattr(tweet, 'media') and hasattr(tweet.media, 'videos'):
                    for video in tweet.media.videos:
                        # 最高画質のバリアントを選択
                        best_variant = None
                        best_bitrate = 0
                        
                        if hasattr(video, 'variants'):
                            for variant in video.variants:
                                if hasattr(variant, 'bitrate') and variant.bitrate:
                                    if variant.bitrate > best_bitrate:
                                        best_bitrate = variant.bitrate
                                        best_variant = variant
                        
                        if best_variant and hasattr(best_variant, 'url'):
                            tweet_data['videos'].append(best_variant.url)
                        elif hasattr(video, 'url'):
                            # variantsがない場合は直接URLを使用
                            tweet_data['videos'].append(video.url)
                
                # force_full_fetchでかつ動画がない場合はスキップ
                if force_full_fetch and not tweet_data['videos']:
                    continue
                
                tweets.append(tweet_data)
                tweet_count += 1
                
                if tweet_count % 100 == 0:
                    self.logger.debug(f"Fetched {tweet_count} tweets so far...")
            
            # 重複を除去（ツイートIDでユニークにする）
            unique_tweets = []
            seen_ids = set()
            for tweet in tweets:
                if tweet['id'] not in seen_ids:
                    unique_tweets.append(tweet)
                    seen_ids.add(tweet['id'])
            
            duplicate_count = len(tweets) - len(unique_tweets)
            if duplicate_count > 0:
                self.logger.warning(f"Removed {duplicate_count} duplicate tweets from current fetch")
            
            self.logger.info(f"Fetched {len(unique_tweets)} unique tweets for @{username} (total examined: {total_fetched}, old tweets skipped: {old_tweets_count}, duplicates removed: {duplicate_count})")
            
            # gallery-dl統合（設定で有効な場合）
            gallery_dl_config = self.config.get('tweet_settings', {}).get('gallery_dl', {})
            if gallery_dl_config.get('enabled', False):
                self.logger.info(f"gallery-dl integration enabled for @{username}")
                try:
                    from .gallery_dl_extractor import GalleryDLExtractor
                    gallery_extractor = GalleryDLExtractor(self.config)
                    
                    # gallery-dlでメディア付きツイートを取得（制限なし）
                    self.logger.info(f"Fetching all media tweets with gallery-dl")
                    is_private_account = self.is_account_private(username)
                    gallery_tweets = gallery_extractor.fetch_media_tweets(
                        username,
                        limit=None,
                        is_private_account=is_private_account
                    )
                    
                    if gallery_tweets:
                        # 既存のツイートIDセット（重複排除用）
                        existing_ids = {tweet['id'] for tweet in unique_tweets}
                        existing_ids.update(existing_tweet_ids)  # データベースの既存IDも含める
                        
                        # gallery-dlのツイートを追加（重複を除く）
                        new_from_gallery = []
                        new_tweet_ids = []  # 新規ツイートIDのリスト（ダウンロード用）
                        skipped_no_media = 0
                        for g_tweet in gallery_tweets:
                            if g_tweet['id'] not in existing_ids:
                                # display_nameを追加
                                g_tweet['display_name'] = display_name
                                new_from_gallery.append(g_tweet)
                                # メディアの無いツイートはDB記録のみ行い、gallery-dlには投げない
                                if g_tweet.get('media') or g_tweet.get('videos'):
                                    new_tweet_ids.append(g_tweet['id'])  # 新規ツイートIDを記録
                                else:
                                    skipped_no_media += 1
                                existing_ids.add(g_tweet['id'])

                        if new_from_gallery:
                            if skipped_no_media:
                                self.logger.info(
                                    f"Skipping gallery-dl download for {skipped_no_media} tweets without media attachments"
                                )
                            self.logger.info(f"Added {len(new_from_gallery)} new tweets from gallery-dl")

                            # 新規ツイートのメディアのみをダウンロード
                            if new_tweet_ids:
                                self.logger.info(f"Downloading media files for {len(new_tweet_ids)} new tweets")
                                tweet_media_paths = gallery_extractor.download_media_for_tweets(
                                    username,
                                    new_tweet_ids,
                                    is_private_account=is_private_account
                                )
                                
                                # 各ツイートにlocal_mediaを設定（絶対パス→相対パス変換）
                                for g_tweet in new_from_gallery:
                                    if g_tweet['id'] in tweet_media_paths:
                                        # 絶対パスを相対パスに変換
                                        absolute_paths = tweet_media_paths[g_tweet['id']]
                                        relative_paths = convert_paths_to_relative(absolute_paths, self.config)
                                        g_tweet['local_media'] = relative_paths
                                        self.logger.debug(f"Set local_media for tweet {g_tweet['id']}: {len(g_tweet['local_media'])} files")
                                    else:
                                        g_tweet['local_media'] = []
                            
                            unique_tweets.extend(new_from_gallery)
                            
                            # 日付でソート（新しい順）
                            unique_tweets.sort(key=lambda x: x['date'], reverse=True)
                        else:
                            self.logger.info(f"No new tweets from gallery-dl (all {len(gallery_tweets)} were duplicates)")
                    else:
                        self.logger.info("No media tweets found by gallery-dl")
                        
                except Exception as e:
                    self.logger.error(f"gallery-dl integration failed: {e}")
                    # エラーが発生してもtwscrapeの結果は返す
            
            return unique_tweets
            
        except Exception as e:
            if "No account available" in str(e):
                self.logger.warning(f"Rate limit reached for @{username}: {e}")
                # レート制限エラーから次の利用可能時間を抽出
                next_available_match = re.search(r'next available at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', str(e))
                if next_available_match:
                    next_time = next_available_match.group(1)
                    self.logger.info(f"Next available time: {next_time}")
            else:
                self.logger.error(f"Error fetching tweets for @{username}: {e}")
            
            # エラーが発生しても空のリストを返す（処理を継続）
            return tweets
    
    async def check_rate_limit_status(self):
        """レート制限の状態を確認"""
        try:
            # アカウントプールの状態を確認
            pool_stats = await self.api.pool.stats()
            self.logger.info(f"Twitter account pool stats: {pool_stats}")
            return pool_stats
        except Exception as e:
            self.logger.error(f"Failed to get rate limit status: {e}")
            return None
    
    def _sanitize_filename(self, name: str) -> str:
        """ファイル名として使えない文字を置換"""
        # Windowsで使えない文字を置換
        invalid_chars = '<>:"|?*\\/'  # バックスラッシュも含む
        for char in invalid_chars:
            name = name.replace(char, '_')
        # 先頭・末尾のスペースとピリオドを削除
        name = name.strip('. ')
        # 空の場合はデフォルト値
        return name or 'unknown'
    
    async def get_single_tweet(self, tweet_id: str) -> Optional[Dict[str, Any]]:
        """単一のツイートを取得"""
        await self._initialize_accounts()
        
        try:
            # ツイートIDで直接取得
            tweet = await self.api.tweet_details(int(tweet_id))
            if not tweet:
                self.logger.warning(f"Tweet {tweet_id} not found")
                return None
            
            # リツイート/リポストをチェック（ユーザー名が不明な場合は、URLから抽出）
            username_for_check = None
            if hasattr(tweet, 'url'):
                url_match = re.search(r'twitter\.com/([^/]+)/status/', tweet.url)
                if url_match:
                    username_for_check = url_match.group(1)
            
            if username_for_check and self._is_retweet(tweet, username_for_check):
                self.logger.debug(f"Tweet {tweet_id} is a retweet, skipping")
                return None
            
            # メディアを抽出
            images = []
            videos = []
            
            if tweet.media and tweet.media.photos:
                for photo in tweet.media.photos:
                    images.append(photo.url)
            
            if tweet.media and tweet.media.videos:
                for video in tweet.media.videos:
                    videos.append(video.bestVariant.url if video.bestVariant else None)
            
            # videosリストから None を除外
            videos = [v for v in videos if v]
            
            # ツイートデータを構築
            tweet_data = {
                'id': tweet.id,
                'username': tweet.user.username if tweet.user else 'unknown',
                'tweet_text': tweet.rawContent,
                'created_at': tweet.date.isoformat(),
                'media_urls': images + videos,
                'images': images,
                'videos': videos,
                'hashtags': [tag.text for tag in tweet.hashtags] if tweet.hashtags else [],
                'view_count': tweet.viewCount,
                'reply_count': tweet.replyCount,
                'retweet_count': tweet.retweetCount,
                'like_count': tweet.likeCount,
                'url': tweet.url
            }
            
            self.logger.info(f"Successfully fetched tweet {tweet_id} with {len(images)} images and {len(videos)} videos")
            return tweet_data
            
        except Exception as e:
            self.logger.error(f"Error fetching tweet {tweet_id}: {e}")
            return None
    
    async def _rotate_account(self):
        """次のアカウントにローテーション"""
        try:
            # 現在のアカウント情報を取得
            current_accounts = await self.api.pool.accounts_info()
            active_accounts = [acc for acc in current_accounts if acc.get('active', True)]

            if len(active_accounts) <= 1:
                self.logger.warning("twscrape: Only one active account available, cannot rotate")
                return

            # アカウントプールの統計を取得
            pool_stats = await self.api.pool.stats()
            self.logger.debug(f"twscrape: Current pool stats: {pool_stats}")

            # 失敗したアカウントを明示的にマークして次のアカウントを使用させる
            try:
                # 現在使用中のアカウントを一時的に無効化
                current_account = getattr(self.api.pool, '_current_account', None)
                if current_account:
                    # 短時間のクールダウンを設定
                    current_account.unlock_at = time.time() + 60  # 1分間のクールダウン
                    self.logger.info(f"twscrape: Set 1-minute cooldown for current account")
            except Exception as cooldown_error:
                self.logger.debug(f"twscrape: Could not set account cooldown: {cooldown_error}")

            self.logger.info(f"twscrape: Account rotation completed, {len(active_accounts)} accounts available")

        except Exception as e:
            self.logger.error(f"twscrape: Error during account rotation: {e}")
            # 代替手段：失敗したアカウントの再ログインを試行
            try:
                await self.api.pool.relogin_failed()
                self.logger.info("twscrape: Attempted relogin for failed accounts")
            except Exception as e2:
                self.logger.warning(f"twscrape: Could not relogin failed accounts: {e2}")
                # エラーを再発生させずに続行

    async def cleanup(self):
        """リソースのクリーンアップ"""
        try:
            # aiohttp セッションのクローズ
            if self._session and not self._session.closed:
                await self._session.close()
                
            # 少し待機してセッションが完全に閉じられるのを待つ
            await asyncio.sleep(0.1)
            
            self.logger.debug("TwitterMonitor cleanup completed")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
