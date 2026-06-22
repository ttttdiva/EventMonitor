import asyncio
import csv
import inspect
import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp


URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# Discord Snowflake ID: 17〜20桁の数字
DISCORD_ID_PATTERN = re.compile(r"^(\d{17,20})$")
# discord:プレフィックス形式
DISCORD_PREFIX_PATTERN = re.compile(r"^discord:(.+)$", re.IGNORECASE)
RESERVED_PATHS = {
    "home",
    "explore",
    "notifications",
    "messages",
    "compose",
    "search",
    "hashtag",
    "i",
    "intent",
    "settings",
    "privacy",
    "tos",
}


class DiscordAccountIngestor:
    def __init__(
        self,
        config: dict,
        twitter_monitor,
        csv_path: str = "monitored_accounts.csv",
        pixiv_extractor=None,
        bilibili_extractor=None,
        display_name_resolvers: Optional[Dict[str, Callable[[str], Optional[str]]]] = None,
    ) -> None:
        self.config = config
        self.twitter_monitor = twitter_monitor
        self.csv_path = Path(csv_path)
        self.pixiv_extractor = pixiv_extractor
        self.bilibili_extractor = bilibili_extractor
        self.display_name_resolvers = display_name_resolvers or {}
        self.logger = logging.getLogger("EventMonitor.DiscordIngest")
        misskey_config = config.get("misskey", {})
        self.default_misskey_instance = (
            str(misskey_config.get("default_instance", "misskey.io")).strip().lower()
            or "misskey.io"
        )
        self.misskey_hosts = {self.default_misskey_instance, "voskey.icalo.net"}
        for host in misskey_config.get("known_hosts", []):
            normalized = str(host).strip().lower()
            if normalized:
                self.misskey_hosts.add(normalized)

        ingest_config = config.get("discord_ingest", {})
        self.enabled = ingest_config.get("enabled", True)
        self.guild_id = str(ingest_config.get("guild_id", "710364576436191289"))
        self.channel_id = str(ingest_config.get("channel_id", "1452383265058193438"))
        self.max_messages = ingest_config.get("max_messages")
        timeout_cfg = ingest_config.get("display_name_timeout_seconds", 30)
        try:
            self.display_name_timeout_seconds = max(1.0, float(timeout_cfg))
        except (TypeError, ValueError):
            self.display_name_timeout_seconds = 30.0

        self.bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.api_base = "https://discord.com/api/v10"

    async def ingest_new_accounts(self) -> List[Dict[str, str]]:
        if not self.enabled:
            self.logger.info("Discord ingest is disabled")
            return []

        if not self.bot_token:
            self.logger.warning("DISCORD_BOT_TOKEN is not configured. Discord ingest skipped.")
            return []

        existing_usernames = self._load_existing_usernames()
        if not existing_usernames:
            self.logger.warning("No existing usernames found in CSV. Using empty set.")

        added_accounts: Dict[str, Dict[str, str]] = {}
        # 処理成功したメッセージIDを記録し、CSV書き込み成功後にのみ削除する
        processed_message_ids: List[str] = []

        headers = {"Authorization": f"Bot {self.bot_token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            messages = await self._fetch_all_messages(session)
            if not messages:
                self.logger.info("No Discord messages found for ingest")
                return []

            self.logger.info(f"Fetched {len(messages)} Discord message(s) for ingest")

            for message in messages:
                msg_id = message.get("id", "unknown")
                content = (message.get("content") or "").strip()
                try:
                    # 全メッセージの生テキストをログに記録（ロスト防止）
                    self.logger.info(f"Discord message {msg_id}: {content!r}")

                    account_entries = self._extract_accounts_from_message(message)

                    if not account_entries:
                        # 解析失敗メッセージは削除しない（手動確認用に残す）
                        self.logger.warning(
                            f"No accounts extracted from Discord message {msg_id}, "
                            f"keeping message for manual review"
                        )
                        continue

                    # Pixiv artwork URL → user_id の非同期解決
                    account_entries = await self._resolve_pixiv_artwork_accounts(account_entries)
                    # bilibili opus URL → mid の非同期解決
                    account_entries = await self._resolve_bilibili_opus_accounts(account_entries)

                    for username, notification, account_type, platform, explicit_name, rank in account_entries:
                        dedup_key = f"{platform}:{username}".lower()
                        if dedup_key in existing_usernames or dedup_key in added_accounts:
                            continue

                        # name:xxx が指定されていればそれを使用、なければ登録時点で解決する
                        if explicit_name:
                            display_name = self._sanitize_display_name(explicit_name)
                        elif platform == "discord":
                            # DiscordサーバーIDをそのまま使用（後で手動でサーバー名を設定）
                            display_name = username
                        else:
                            display_name = await self._resolve_display_name(username, platform)

                        added_accounts[dedup_key] = {
                            "username": username,
                            "display_name": display_name,
                            "notification": notification,
                            "account_type": account_type,
                            "platform": platform,
                            "rank": rank,
                        }

                    processed_message_ids.append(msg_id)
                except Exception as exc:
                    self.logger.error(
                        f"Error processing Discord message {msg_id}: {exc}",
                        exc_info=True
                    )
                    # エラーが発生しても次のメッセージを処理するため継続

            # CSV書き込み成功後にのみメッセージを削除する
            if added_accounts:
                self._sync_rank_from_existing(added_accounts)
                self._append_accounts_to_csv(added_accounts)
                self.logger.info(f"Added {len(added_accounts)} account(s) from Discord ingest")
            else:
                self.logger.info("No new accounts found in Discord messages")

            for msg_id in processed_message_ids:
                try:
                    await self._delete_message(session, msg_id)
                except Exception as exc:
                    self.logger.warning(f"Failed to delete Discord message {msg_id}: {exc}")

        return list(added_accounts.values())

    def _load_existing_usernames(self) -> Set[str]:
        """既存のアカウントを "platform:username" 形式のセットとして読み込む。"""
        usernames: Set[str] = set()
        if not self.csv_path.exists():
            return usernames

        with self.csv_path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                username = row.get("username")
                if not username:
                    continue
                platform = (row.get("platform") or "").strip()
                usernames.add(f"{platform}:{username}".strip().lower())
        return usernames

    async def _fetch_all_messages(self, session: aiohttp.ClientSession) -> List[dict]:
        messages: List[dict] = []
        before_id: Optional[str] = None
        fetched_total = 0

        while True:
            params = {"limit": 100}
            if before_id:
                params["before"] = before_id

            status, payload = await self._request_json(
                session,
                "GET",
                f"{self.api_base}/channels/{self.channel_id}/messages",
                params=params,
            )

            if status != 200:
                self.logger.error(f"Failed to fetch Discord messages: status={status}")
                break

            if not payload:
                break

            messages.extend(payload)
            fetched_total += len(payload)
            before_id = payload[-1].get("id")

            if self.max_messages and fetched_total >= self.max_messages:
                break

        return messages

    async def _delete_message(self, session: aiohttp.ClientSession, message_id: Optional[str]) -> None:
        if not message_id:
            return

        status, _ = await self._request_json(
            session,
            "DELETE",
            f"{self.api_base}/channels/{self.channel_id}/messages/{message_id}",
        )

        if status not in (204, 200):
            self.logger.warning(f"Failed to delete Discord message {message_id}: status={status}")

    async def _request_json(self, session: aiohttp.ClientSession, method: str, url: str, params=None):
        for _ in range(5):
            async with session.request(method, url, params=params) as response:
                if response.status == 429:
                    try:
                        payload = await response.json()
                    except Exception:
                        payload = {}
                    retry_after = payload.get("retry_after", 1)
                    await asyncio.sleep(float(retry_after) + 0.25)
                    continue

                if response.status in (200, 201, 204):
                    if response.status == 204:
                        return response.status, None
                    try:
                        return response.status, await response.json()
                    except Exception:
                        return response.status, None

                try:
                    return response.status, await response.json()
                except Exception:
                    return response.status, None

        return 429, None

    def _extract_accounts_from_message(
        self, message: dict
    ) -> List[Tuple[str, str, str, str, str, str]]:
        content = message.get("content", "") or ""
        if not content:
            return []

        return self._parse_accounts_from_content(content)

    def _parse_accounts_from_content(self, content: str) -> List[Tuple[str, str, str, str, str, str]]:
        """メッセージ本文からアカウント情報を抽出する。

        Returns:
            (username, notification, account_type, platform, display_name, rank) のリスト
            notification は空欄（通知なし＝デフォルト）または "notice"（通知あり）。
            display_name は "name:xxx" 行で指定された場合のみ値が入り、未指定なら ""。
            rank は "1"/"2"/"3" または空欄（デフォルト=3）。
        """
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return []

        accounts: List[Tuple[str, str, str, str, str, str]] = []
        current: Optional[Tuple[str, str, str, str, str, str]] = None

        def commit_current():
            if current:
                accounts.append(current)

        for line in lines:
            # --- DiscordサーバーID検出 ---
            # "discord:1094999323365875773" プレフィックス形式
            prefix_match = DISCORD_PREFIX_PATTERN.match(line)
            if prefix_match:
                server_id = prefix_match.group(1).strip()
                commit_current()
                current = (server_id, "", "", "discord", "", "")
                continue

            # 17桁以上の数字のみ = DiscordサーバーIDと判定
            bare_id_match = DISCORD_ID_PATTERN.match(line)
            if bare_id_match:
                commit_current()
                current = (line, "", "", "discord", "", "")
                continue

            # --- URLベースのアカウント抽出 ---
            urls = self._extract_urls(line)
            if urls:
                for url in urls:
                    result = self._extract_username_from_url(url)
                    if not result:
                        continue
                    username, platform = result
                    commit_current()
                    current = (username, "", "", platform, "", "")
                continue

            if not current:
                continue

            token = line.strip()
            username, notification, account_type, platform, display_name, rank = current
            token_lower = token.lower()
            if token_lower in {"1", "2", "3"}:
                rank = token_lower
            elif token_lower == "notice":
                notification = "notice"
            elif token_lower == "log":
                account_type = "log"
            elif token_lower.startswith("name:"):
                display_name = token[5:].strip()
            current = (username, notification, account_type, platform, display_name, rank)

        commit_current()
        return accounts

    def _extract_urls(self, content: str) -> List[str]:
        urls = []
        for raw_url in URL_PATTERN.findall(content):
            url = raw_url.strip("<>")
            url = url.rstrip(">),.!?]}")
            if url:
                urls.append(url)
        return urls

    def _extract_username_from_url(self, url: str) -> Optional[Tuple[str, str]]:
        """URLからユーザー名とplatformを抽出する。

        Returns:
            (username, platform) タプル。platformは "" (Twitter), "pixiv", "kemono", "poipiku", "tinami", "fantia", "nijie"。
            解析不能な場合は None。
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        # --- Twitter / X ---
        if netloc.endswith("x.com") or netloc.endswith("twitter.com"):
            return self._extract_twitter_username(parsed)

        # --- Pixiv ---
        if netloc.endswith("pixiv.net"):
            return self._extract_pixiv_username(parsed)

        # --- Kemono ---
        if netloc in ("kemono.cr", "kemono.su"):
            return self._extract_kemono_username(parsed)

        # --- Poipiku ---
        if netloc == "poipiku.com":
            return self._extract_poipiku_username(parsed)

        # --- TINAMI ---
        if netloc == "tinami.com":
            return self._extract_tinami_username(parsed)

        # --- Fantia ---
        if netloc == "fantia.jp":
            return self._extract_fantia_username(parsed)

        # --- Nijie ---
        if netloc == "nijie.info":
            return self._extract_nijie_username(parsed)

        # --- Skeb ---
        if netloc == "skeb.jp":
            return self._extract_skeb_username(parsed)

        # --- Misskey ---
        if netloc in self.misskey_hosts:
            return self._extract_misskey_username(parsed, netloc)

        # --- FANBOX ---
        # NOTE: netloc は上部で www. が除去済みなので "fanbox.cc" と比較する
        if netloc == "fanbox.cc" or netloc.endswith(".fanbox.cc"):
            return self._extract_fanbox_username(parsed)

        # --- Bluesky ---
        if netloc == "bsky.app":
            return self._extract_bluesky_username(parsed)

        # --- Privatter ---
        if netloc == "privatter.net":
            return self._extract_privatter_username(parsed)

        # --- bilibili ---
        # netloc は上部で www. が除去済み。space./t./m. サブドメインは残る
        if netloc == "bilibili.com" or netloc.endswith(".bilibili.com"):
            return self._extract_bilibili_username(parsed, netloc)

        return None

    def _extract_twitter_username(self, parsed) -> Optional[Tuple[str, str]]:
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        head = parts[0].lower()
        if head in RESERVED_PATHS:
            if head == "intent" and len(parts) > 1 and parts[1].lower() == "user":
                params = parse_qs(parsed.query)
                screen_name = params.get("screen_name", [""])[0]
                username = self._normalize_username(screen_name)
                return (username, "") if username else None
            return None

        username = self._normalize_username(parts[0])
        return (username, "") if username else None

    def _extract_pixiv_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://www.pixiv.net/users/12345 → ("12345", "pixiv")
        https://www.pixiv.net/artworks/142284673 → ("artworks:142284673", "pixiv")
        artwork URL の場合は仮の username として "artworks:{id}" を返し、
        後段の _resolve_pixiv_artwork_accounts() で実際の user_id に解決する。
        """
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # ロケールプレフィックス（/en/, /ja/ 等）をスキップ
        if len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha():
            parts = parts[1:]
        # /users/{user_id} パターン
        if len(parts) >= 2 and parts[0].lower() == "users" and parts[1].isdigit():
            return (parts[1], "pixiv")

        # /artworks/{artwork_id} パターン（後で user_id に解決する）
        if len(parts) >= 2 and parts[0].lower() == "artworks" and parts[1].isdigit():
            return (f"artworks:{parts[1]}", "pixiv")

        return None

    async def _resolve_pixiv_artwork_accounts(
        self, entries: List[Tuple[str, str, str, str, str, str]]
    ) -> List[Tuple[str, str, str, str, str, str]]:
        """account_entries 内の "artworks:NNNNN" を実際の Pixiv user_id に解決する。

        PixivExtractor が未設定の場合はログ出力のみで artworks: エントリを除去する。
        解決失敗のエントリも除去する（メッセージは processed 扱いにしない）。
        """
        resolved: List[Tuple[str, str, str, str, str, str]] = []
        for username, notification, account_type, platform, display_name, rank in entries:
            if platform != "pixiv" or not username.startswith("artworks:"):
                resolved.append((username, notification, account_type, platform, display_name, rank))
                continue

            artwork_id = username.split(":", 1)[1]
            if not self.pixiv_extractor:
                self.logger.warning(
                    f"PixivExtractor が未設定のため artwork {artwork_id} のユーザー解決をスキップ"
                )
                continue

            try:
                import asyncio
                works = await asyncio.to_thread(
                    self.pixiv_extractor.fetch_user_works_by_artwork_id, artwork_id
                )
                if works and works[0].get("username"):
                    user_id = str(works[0]["username"])
                    self.logger.info(
                        f"Pixiv artwork {artwork_id} → user_id {user_id} に解決"
                    )
                    resolved.append((user_id, notification, account_type, platform, display_name, rank))
                else:
                    self.logger.warning(
                        f"Pixiv artwork {artwork_id} からユーザーIDを取得できませんでした"
                    )
            except Exception as exc:
                self.logger.error(
                    f"Pixiv artwork {artwork_id} の解決中にエラー: {exc}",
                    exc_info=True,
                )

        return resolved

    def _extract_bilibili_username(self, parsed, netloc: str) -> Optional[Tuple[str, str]]:
        """bilibili URL から数値ユーザーID(mid)を抽出。

        - https://space.bilibili.com/289132019 → ("289132019", "bilibili")
        - https://space.bilibili.com/289132019/upload/opus → ("289132019", "bilibili")
        - https://www.bilibili.com/opus/120196... → ("opus:120196...", "bilibili")
        - https://t.bilibili.com/120196... → ("opus:120196...", "bilibili")
        opus URL の場合は仮の username として "opus:{id}" を返し、
        後段の _resolve_bilibili_opus_accounts() で実際の mid に解決する。
        """
        path = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]

        # space.bilibili.com/{mid}[/...]
        if netloc == "space.bilibili.com":
            if parts and parts[0].isdigit():
                return (parts[0], "bilibili")
            return None

        # t.bilibili.com/{dynamic_id}
        if netloc == "t.bilibili.com":
            if parts and parts[0].isdigit():
                return (f"opus:{parts[0]}", "bilibili")
            return None

        # (www.|m.)bilibili.com/opus/{opus_id}
        if len(parts) >= 2 and parts[0].lower() == "opus" and parts[1].isdigit():
            return (f"opus:{parts[1]}", "bilibili")

        return None

    async def _resolve_bilibili_opus_accounts(
        self, entries: List[Tuple[str, str, str, str, str, str]]
    ) -> List[Tuple[str, str, str, str, str, str]]:
        """account_entries 内の "opus:NNNNN" を実際の bilibili mid に解決する。

        BilibiliExtractor が未設定の場合はログ出力のみで opus: エントリを除去する。
        解決失敗のエントリも除去する。
        """
        resolved: List[Tuple[str, str, str, str, str, str]] = []
        for username, notification, account_type, platform, display_name, rank in entries:
            if platform != "bilibili" or not username.startswith("opus:"):
                resolved.append((username, notification, account_type, platform, display_name, rank))
                continue

            opus_id = username.split(":", 1)[1]
            if not self.bilibili_extractor:
                self.logger.warning(
                    f"BilibiliExtractor が未設定のため opus {opus_id} のユーザー解決をスキップ"
                )
                continue

            try:
                import asyncio
                mid = await asyncio.to_thread(
                    self.bilibili_extractor.fetch_user_id_by_opus, opus_id
                )
                if mid:
                    self.logger.info(f"bilibili opus {opus_id} → mid {mid} に解決")
                    resolved.append((str(mid), notification, account_type, platform, display_name, rank))
                else:
                    self.logger.warning(
                        f"bilibili opus {opus_id} からユーザーIDを取得できませんでした"
                    )
            except Exception as exc:
                self.logger.error(
                    f"bilibili opus {opus_id} の解決中にエラー: {exc}",
                    exc_info=True,
                )

        return resolved

    def _extract_kemono_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://kemono.cr/fanbox/user/3316400 → ("fanbox/3316400", "kemono")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # /{service}/user/{user_id} パターン
        if len(parts) >= 3 and parts[1].lower() == "user" and parts[2]:
            service = parts[0].lower()
            user_id = parts[2]
            return (f"{service}/{user_id}", "kemono")

        return None

    def _extract_poipiku_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://poipiku.com/8150331/ → ("8150331", "poipiku")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        if parts[0].isdigit():
            return (parts[0], "poipiku")

        return None

    def _extract_tinami_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://www.tinami.com/creator/profile/65154 → ("65154", "tinami")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # /creator/profile/{user_id} パターン
        if len(parts) >= 3 and parts[0].lower() == "creator" and parts[1].lower() == "profile" and parts[2].isdigit():
            return (parts[2], "tinami")

        return None

    def _extract_fantia_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://fantia.jp/fanclubs/12345 → ("12345", "fantia")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # /fanclubs/{fanclub_id} パターン
        if len(parts) >= 2 and parts[0].lower() == "fanclubs" and parts[1].isdigit():
            return (parts[1], "fantia")

        return None

    def _extract_nijie_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://nijie.info/members.php?id=45352 → ("45352", "nijie")"""
        path = parsed.path.strip("/").lower()
        if not path:
            return None

        # /members.php?id={user_id} or /members_illust.php?id={user_id}
        if path in ("members.php", "members_illust.php"):
            params = parse_qs(parsed.query)
            user_id = params.get("id", [""])[0]
            if user_id and user_id.isdigit():
                return (user_id, "nijie")

        return None

    def _extract_skeb_username(self, parsed) -> Optional[Tuple[str, str]]:
        """https://skeb.jp/@cone_huraku → ("cone_huraku", "skeb")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        # @username または @username/works/N パターン
        if path.startswith("@"):
            username = path[1:].split("/")[0]
            if username:
                return (username, "skeb")

        return None

    def _extract_misskey_username(
        self, parsed, instance_host: str
    ) -> Optional[Tuple[str, str]]:
        """https://misskey.io/@kashiwatoriniku → ("kashiwatoriniku", "misskey")"""
        path = parsed.path.strip("/")
        if not path:
            return None

        # @username または @username/notes パターン、notes/{noteId} は無視
        if path.startswith("@"):
            username = path[1:].split("/")[0]
            if username:
                if instance_host == self.default_misskey_instance:
                    return (username, "misskey")
                return (f"{username}@{instance_host}", "misskey")

        return None

    def _extract_fanbox_username(self, parsed) -> Optional[Tuple[str, str]]:
        """
        https://www.fanbox.cc/@ashiyama → ("ashiyama", "fanbox")
        https://ashiyama.fanbox.cc/ → ("ashiyama", "fanbox")
        https://www.fanbox.cc/@ashiyama/posts/12345 → ("ashiyama", "fanbox")
        """
        netloc = parsed.hostname or ""
        path = parsed.path.strip("/")

        # サブドメイン形式: {creatorId}.fanbox.cc
        if netloc != "www.fanbox.cc" and netloc.endswith(".fanbox.cc"):
            creator_id = netloc.replace(".fanbox.cc", "")
            if creator_id:
                return (creator_id, "fanbox")

        # パス形式: www.fanbox.cc/@{creatorId}
        if path.startswith("@"):
            creator_id = path[1:].split("/")[0]
            if creator_id:
                return (creator_id, "fanbox")

        return None

    def _extract_bluesky_username(self, parsed) -> Optional[Tuple[str, str]]:
        """
        https://bsky.app/profile/kongaricacao.bsky.social → ("kongaricacao.bsky.social", "bluesky")
        https://bsky.app/profile/kongaricacao.bsky.social/post/xxx → ("kongaricacao.bsky.social", "bluesky")
        """
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # /profile/{handle} パターン
        if len(parts) >= 2 and parts[0].lower() == "profile" and parts[1]:
            handle = parts[1]
            return (handle, "bluesky")

        return None

    def _extract_privatter_username(self, parsed) -> Optional[Tuple[str, str]]:
        """
        https://privatter.net/u/ebachi11 → ("ebachi11", "privatter")
        https://privatter.net/i/7851219 → None（個別投稿URLからはユーザーID取得不可）
        """
        path = parsed.path.strip("/")
        if not path:
            return None

        parts = path.split("/")
        # /u/{username} パターン
        if len(parts) >= 2 and parts[0].lower() == "u" and parts[1]:
            username = parts[1]
            return (username, "privatter")

        return None

    def _normalize_username(self, username: str) -> Optional[str]:
        if not username:
            return None
        username = username.strip().lstrip("@").lower()
        if not USERNAME_PATTERN.match(username):
            return None
        return username

    async def _resolve_display_name(self, username: str, platform: str = "") -> str:
        if not username:
            return ""

        platform_key = (platform or "twitter").strip().lower()
        try:
            if platform_key == "twitter":
                display_name = await self._resolve_twitter_display_name(username)
            else:
                display_name = await self._resolve_platform_display_name(
                    username,
                    platform_key,
                )
            if not display_name:
                return username
            # カンマを除去してCSV破損を防ぐ
            return self._sanitize_display_name(display_name)
        except Exception as exc:
            self.logger.warning(
                f"Failed to resolve display name for {platform_key}:{username}: {exc}"
            )
            return username

    async def _resolve_twitter_display_name(self, username: str) -> Optional[str]:
        return await asyncio.wait_for(
            self.twitter_monitor.resolve_display_name(username),
            timeout=self.display_name_timeout_seconds,
        )

    async def _resolve_platform_display_name(
        self,
        username: str,
        platform: str,
    ) -> Optional[str]:
        resolver = self.display_name_resolvers.get(platform)
        if resolver is None:
            self.logger.warning(
                f"No display name resolver configured for {platform}:{username}; "
                f"using username as display_name"
            )
            return None

        if inspect.iscoroutinefunction(resolver):
            return await asyncio.wait_for(
                resolver(username),
                timeout=self.display_name_timeout_seconds,
            )
        return await asyncio.wait_for(
            asyncio.to_thread(resolver, username),
            timeout=self.display_name_timeout_seconds,
        )

    @staticmethod
    def _sanitize_display_name(display_name: str) -> str:
        return " ".join(str(display_name).replace(",", " ").split())


    def _sync_rank_from_existing(self, accounts: Dict[str, Dict[str, str]]) -> None:
        """display_nameが既存アカウントと一致し、rankが未指定の場合、既存のrankをコピーする。"""
        if not self.csv_path.exists():
            return

        # 既存CSVから display_name → rank のマッピングを構築
        name_to_rank: Dict[str, str] = {}
        try:
            with self.csv_path.open("r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    dn = (row.get("display_name") or "").strip()
                    rank = (row.get("rank") or "").strip()
                    if dn and rank and dn not in name_to_rank:
                        name_to_rank[dn] = rank
        except Exception as exc:
            self.logger.warning(f"Failed to read CSV for rank sync: {exc}")
            return

        if not name_to_rank:
            return

        for _key, payload in accounts.items():
            if payload.get("rank"):
                continue
            dn = (payload.get("display_name") or "").strip()
            if dn and dn in name_to_rank:
                payload["rank"] = name_to_rank[dn]
                self.logger.info(
                    f"Rank synced for {payload.get('username')}: "
                    f"display_name={dn!r} → rank={name_to_rank[dn]}"
                )

    def _append_accounts_to_csv(self, accounts: Dict[str, Dict[str, str]]) -> None:
        if not accounts:
            return

        try:
            file_exists = self.csv_path.exists()
            with self.csv_path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if not file_exists:
                    writer.writerow(["username", "display_name", "notification", "account_type", "platform", "custom_tags", "rank", "twitter_id"])

                for _dedup_key, payload in accounts.items():
                    try:
                        display_name = self._sanitize_display_name(
                            payload.get("display_name", "")
                        )
                        writer.writerow(
                            [
                                payload.get("username", ""),
                                display_name,
                                payload.get("notification", ""),
                                payload.get("account_type", ""),
                                payload.get("platform", ""),
                                "",  # custom_tags
                                payload.get("rank", ""),
                                "",  # twitter_id（クローラーが初回取得時に埋める）
                            ]
                        )
                    except Exception as exc:
                        self.logger.error(f"Failed to write account {payload.get('username')} to CSV: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to open CSV file for writing: {exc}", exc_info=True)
            raise
