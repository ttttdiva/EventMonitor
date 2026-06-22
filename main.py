#!/usr/bin/env python3
import asyncio
import logging
import signal
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

import yaml
from dotenv import load_dotenv
import csv


class GracefulShutdown:
    """Ctrl+Cによるグレースフルシャットダウンを管理

    1回目のCtrl+C: シャットダウンフラグを立て、現在処理中の作品/ツイートの完了を待つ
    2回目のCtrl+C: KeyboardInterruptを発生させ即座に終了
    """

    def __init__(self):
        self.requested = False
        self._count = 0

    def request(self):
        self._count += 1
        if self._count == 1:
            self.requested = True
            print("\nシャットダウンを要求しました。現在処理中の作品/ツイートが完了したら停止します。")
            print("もう一度 Ctrl+C を押すと強制終了します。")
        else:
            raise KeyboardInterrupt

# twscrapeのloguruログを抑制（パースエラーをWARNINGに降格）
# Twitter APIのレスポンス形式変更により、一部のツイートがパース失敗することがあるが
# これは機能的には問題なく（スキップされる）、ERRORログが大量に出るのを防ぐ
try:
    from loguru import logger as loguru_logger
    
    def twscrape_log_filter(record):
        """twscrape.modelsからの不要ログをフィルタリング"""
        if record["name"].startswith("twscrape.models"):
            # Failed to parse...メッセージはWARNINGに降格
            if "Failed to parse" in record["message"]:
                record["level"] = loguru_logger.level("WARNING")
            # Unknown card type は無害なので抑制（DEBUGに降格）
            if "Unknown card type" in record["message"]:
                record["level"] = loguru_logger.level("DEBUG")
        return True
    
    # loguruのデフォルトハンドラを再設定してフィルターを適用
    loguru_logger.remove()
    loguru_logger.add(
        sys.stderr,
        filter=twscrape_log_filter,
        format="<level>{time:YYYY-MM-DD HH:mm:ss.SSS}</level> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO"
    )
except ImportError:
    pass  # loguruがインストールされていない場合は無視

from src.twitter_monitor import TwitterMonitor
from src.event_detector import EventDetector
from src.database import DatabaseManager
from src.discord_notifier import DiscordNotifier
from src.utils import setup_logging
from src.backup_manager import BackupManager
from src.hydrus_client import HydrusClient
from src.services.account_processor import AccountProcessor
from src.services.discord_account_ingest import DiscordAccountIngestor
from src.status_notifier import StatusNotifier
from src.pixiv_extractor import PixivExtractor
from src.kemono_extractor import KemonoExtractor
from src.tinami_extractor import TinamiExtractor
from src.poipiku_extractor import PoipikuExtractor
from src.fantia_extractor import FantiaExtractor
from src.nijie_extractor import NijieExtractor
from src.skeb_extractor import SkebExtractor
from src.bilibili_extractor import BilibiliExtractor
from src.misskey_extractor import MisskeyExtractor
from src.gelbooru_extractor import GelbooruExtractor
from src.fanbox_extractor import FanboxExtractor
from src.bluesky_extractor import BlueskyExtractor
from src.privatter_extractor import PrivatterExtractor
from src.hydrus_dedup import HydrusDedup
from src.account_status_tracker import AccountStatusTracker
from src.discord_exporter import DiscordExporter
from src.account_priority import build_account_key, sort_accounts_for_platform
from src.csv_git_sync import CsvGitSyncer


class EventMonitor:
    def __init__(self, config_path: str = "config.yaml", shutdown: GracefulShutdown = None):
        self.config = self._load_config(config_path)
        # ロガーは後で初期化（ログディレクトリが決まってから）
        self.logger = None
        # グレースフルシャットダウン管理
        self.shutdown = shutdown or GracefulShutdown()

        # コンポーネントの初期化
        self.db_manager = DatabaseManager(self.config)
        self.event_detector = EventDetector(self.config)
        self.twitter_monitor = TwitterMonitor(self.config, self.db_manager, self.event_detector)
        self.discord_notifier = DiscordNotifier(self.config)
        self.backup_manager = BackupManager(self.config, self.db_manager)
        self.hydrus_client = HydrusClient(self.config)
        self.hydrus_dedup = HydrusDedup(self.config)
        self.pixiv_extractor = PixivExtractor(self.config) if self.config.get('pixiv', {}).get('enabled', False) else None
        self.discord_ingest_done = False
        self.runtime_prioritized_accounts: Set[str] = set()
        self.csv_git_syncer = CsvGitSyncer(self.config)
        self.csv_git_sync_task: Optional[asyncio.Task] = None
        self.csv_git_sync_pending_reason: Optional[str] = None
        self.status_notifier = StatusNotifier(self.config)
        self.kemono_extractor = KemonoExtractor(self.config) if self.config.get('kemono', {}).get('enabled', False) else None
        self.tinami_extractor = TinamiExtractor(self.config) if self.config.get('tinami', {}).get('enabled', False) else None
        self.poipiku_extractor = PoipikuExtractor(self.config) if self.config.get('poipiku', {}).get('enabled', False) else None
        self.fantia_extractor = FantiaExtractor(self.config) if self.config.get('fantia', {}).get('enabled', False) else None
        self.nijie_extractor = NijieExtractor(self.config) if self.config.get('nijie', {}).get('enabled', False) else None
        self.skeb_extractor = SkebExtractor(self.config) if self.config.get('skeb', {}).get('enabled', False) else None
        self.bilibili_extractor = BilibiliExtractor(self.config) if self.config.get('bilibili', {}).get('enabled', False) else None
        self.misskey_extractor = MisskeyExtractor(self.config) if self.config.get('misskey', {}).get('enabled', False) else None
        self.gelbooru_extractor = GelbooruExtractor(self.config) if self.config.get('gelbooru', {}).get('enabled', False) else None
        self.fanbox_extractor = FanboxExtractor(self.config) if self.config.get('fanbox', {}).get('enabled', False) else None
        self.bluesky_extractor = BlueskyExtractor(self.config) if self.config.get('bluesky', {}).get('enabled', False) else None
        self.privatter_extractor = PrivatterExtractor(self.config) if self.config.get('privatter', {}).get('enabled', False) else None
        display_name_resolvers = {
            name: extractor.resolve_display_name
            for name, extractor in {
                'pixiv': self.pixiv_extractor,
                'kemono': self.kemono_extractor,
                'tinami': self.tinami_extractor,
                'poipiku': self.poipiku_extractor,
                'fantia': self.fantia_extractor,
                'nijie': self.nijie_extractor,
                'skeb': self.skeb_extractor,
                'bilibili': self.bilibili_extractor,
                'misskey': self.misskey_extractor,
                'gelbooru': self.gelbooru_extractor,
                'fanbox': self.fanbox_extractor,
                'bluesky': self.bluesky_extractor,
                'privatter': self.privatter_extractor,
            }.items()
            if extractor is not None
        }
        self.discord_account_ingestor = DiscordAccountIngestor(
            self.config,
            self.twitter_monitor,
            pixiv_extractor=self.pixiv_extractor,
            bilibili_extractor=self.bilibili_extractor,
            display_name_resolvers=display_name_resolvers,
        )
        # Discord Crawler
        self.discord_exporter = DiscordExporter(self.config) if self.config.get('discord_crawler', {}).get('enabled', False) else None
        if self.discord_exporter and not self.discord_exporter.enabled:
            self.discord_exporter = None  # 初期検証で無効化された場合
        # アカウント到達性トラッカー
        monitoring_config = self.config.get('account_monitoring', {})
        data_dir = self.config.get('system', {}).get('data_dir', 'data')
        self.account_status_tracker = AccountStatusTracker(
            path=str(Path(data_dir) / 'flagged_accounts.json'),
            expiry_days=monitoring_config.get('expiry_days', 30),
        )
        self.account_processor = AccountProcessor(
            self.config,
            self.db_manager,
            self.event_detector,
            self.twitter_monitor,
            self.discord_notifier,
            self.backup_manager,
            self.hydrus_client,
            self.status_notifier,
            pixiv_extractor=self.pixiv_extractor,
            kemono_extractor=self.kemono_extractor,
            tinami_extractor=self.tinami_extractor,
            poipiku_extractor=self.poipiku_extractor,
            fantia_extractor=self.fantia_extractor,
            nijie_extractor=self.nijie_extractor,
            skeb_extractor=self.skeb_extractor,
            bilibili_extractor=self.bilibili_extractor,
            misskey_extractor=self.misskey_extractor,
            gelbooru_extractor=self.gelbooru_extractor,
            fanbox_extractor=self.fanbox_extractor,
            bluesky_extractor=self.bluesky_extractor,
            privatter_extractor=self.privatter_extractor,
            discord_exporter=self.discord_exporter,
            account_status_tracker=self.account_status_tracker,
            shutdown=self.shutdown,
        )
        
    def _load_config(self, config_path: str) -> dict:
        """設定ファイルを読み込む"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # CSVファイルから監視対象アカウントを読み込む
        config['monitored_accounts'] = self._load_monitored_accounts_from_csv("monitored_accounts.csv")
        
        return config
    
    def _sort_accounts_by_priority(self, platform: str, accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """投稿頻度と実行中の追加アカウントを元にクロール順序をソート"""
        priority_config = self.config.get('system', {}).get('priority_sorting', {})
        sorted_accounts, log_message = sort_accounts_for_platform(
            platform=platform,
            accounts=accounts,
            db_manager=self.db_manager,
            priority_config=priority_config,
            runtime_prioritized_accounts=self.runtime_prioritized_accounts,
        )
        if log_message:
            self.logger.info(log_message)
        return sorted_accounts

    def _load_monitored_accounts_from_csv(self, csv_path: str) -> List[Dict[str, str]]:
        """CSVファイルから監視対象アカウントを読み込む"""
        accounts = []
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # account_typeの値を安全に取得
                    account_type = row.get('account_type')
                    if account_type is None:
                        account_type = ''
                    account_type = account_type.strip()
                    
                    # platformの値を安全に取得（空欄 = twitter）
                    platform = row.get('platform')
                    if platform is None:
                        platform = ''
                    platform = platform.strip() or 'twitter'

                    # custom_tagsの値を安全に取得（パイプ区切りでリスト化）
                    custom_tags_raw = row.get('custom_tags')
                    if custom_tags_raw is None:
                        custom_tags_raw = ''
                    custom_tags = [t.strip() for t in custom_tags_raw.strip().split('|') if t.strip()]

                    # rank: 空欄 or 未指定 = 3（デフォルト）, 1 = 最上位, 2 = 中位, 3 = 最下位
                    rank_raw = (row.get('rank') or '').strip()
                    rank = int(rank_raw) if rank_raw in ('1', '2', '3') else 3

                    # twitter_id: 数値IDを保存（ID変更追跡用）
                    twitter_id_raw = (row.get('twitter_id') or '').strip()
                    twitter_id = int(twitter_id_raw) if twitter_id_raw else None

                    accounts.append({
                        'username': row['username'],
                        'display_name': row['display_name'],
                        'event_detection_enabled': row.get('notification', '').strip().lower() == 'notice',
                        'account_type': account_type,  # 空欄がデフォルト（通常監視）
                        'platform': platform,  # 空欄 = twitter（後方互換性維持）
                        'custom_tags': custom_tags,  # 空欄 = タグなし
                        'rank': rank,  # 空欄 = 3（デフォルト）
                        'twitter_id': twitter_id,  # None = 未取得
                    })
        except FileNotFoundError:
            raise FileNotFoundError(f"監視対象アカウントのCSVファイルが見つかりません: {csv_path}")
        except Exception as e:
            raise Exception(f"CSVファイルの読み込みに失敗しました: {e}")
        
        return accounts

    async def _ingest_discord_accounts(self) -> None:
        """Discordチャンネルから新規アカウントを追加"""
        if self.discord_ingest_done:
            return

        try:
            added_accounts = await self.discord_account_ingestor.ingest_new_accounts()
            if added_accounts:
                self.config['monitored_accounts'] = self._load_monitored_accounts_from_csv("monitored_accounts.csv")
                self.runtime_prioritized_accounts = {
                    build_account_key(account.get("username", ""), account.get("platform", ""))
                    for account in added_accounts
                    if account.get("username")
                }
                self.logger.info(
                    f"Loaded {len(added_accounts)} new account(s) from Discord for this run"
                )
                self.logger.info(
                    "Runtime-prioritized accounts for this run: %s",
                    ", ".join(sorted(self.runtime_prioritized_accounts)),
                )
        except Exception as e:
            self.logger.error(f"Discord ingest failed: {e}", exc_info=True)
        finally:
            self.discord_ingest_done = True

    async def _resolve_missing_display_names(self) -> None:
        """display_nameが空のアカウントに対してプラットフォーム別に表示名を自動取得しCSVを更新"""
        accounts = self.config.get('monitored_accounts', [])
        missing = [
            acc for acc in accounts
            if not acc.get('display_name', '').strip()
        ]

        if not missing:
            self.logger.info("No missing display names found")
            return

        self.logger.info(f"Resolving {len(missing)} missing display names...")

        # プラットフォーム別にグループ化
        twitter_missing = [a for a in missing if a.get('platform', 'twitter') == 'twitter']
        pixiv_missing = [a for a in missing if a.get('platform') == 'pixiv']
        kemono_missing = [a for a in missing if a.get('platform') == 'kemono']
        tinami_missing = [a for a in missing if a.get('platform') == 'tinami']
        poipiku_missing = [a for a in missing if a.get('platform') == 'poipiku']
        fantia_missing = [a for a in missing if a.get('platform') == 'fantia']
        nijie_missing = [a for a in missing if a.get('platform') == 'nijie']
        skeb_missing = [a for a in missing if a.get('platform') == 'skeb']
        bilibili_missing = [a for a in missing if a.get('platform') == 'bilibili']
        misskey_missing = [a for a in missing if a.get('platform') == 'misskey']
        fanbox_missing = [a for a in missing if a.get('platform') == 'fanbox']
        bluesky_missing = [a for a in missing if a.get('platform') == 'bluesky']
        privatter_missing = [a for a in missing if a.get('platform') == 'privatter']

        # username -> display_name のマッピング
        updates: Dict[str, str] = {}
        resolved_count = 0
        failed_count = 0

        # Twitter display name解決
        if twitter_missing:
            self.logger.info(f"Resolving {len(twitter_missing)} Twitter display names...")
            for acc in twitter_missing:
                username = acc['username']
                try:
                    display_name = await self.twitter_monitor.resolve_display_name(username)
                    if display_name:
                        # カンマを除去してCSV破損を防ぐ
                        display_name = display_name.replace(',', ' ')
                        updates[username] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Twitter @{username}: {exc}")
                    failed_count += 1

        # Pixiv display name解決
        if pixiv_missing and self.pixiv_extractor:
            self.logger.info(f"Resolving {len(pixiv_missing)} Pixiv display names...")
            for i, acc in enumerate(pixiv_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.pixiv_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        # カンマを除去してCSV破損を防ぐ
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Pixiv user {user_id}: {exc}")
                    failed_count += 1

                # 進捗ログ（50件ごと）
                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Pixiv display name progress: {i + 1}/{len(pixiv_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Kemono display name解決
        if kemono_missing and self.kemono_extractor:
            self.logger.info(f"Resolving {len(kemono_missing)} Kemono display names...")
            for i, acc in enumerate(kemono_missing):
                username = acc['username']  # e.g. "fanbox/3316400"
                try:
                    display_name = await asyncio.to_thread(
                        self.kemono_extractor.resolve_display_name, username
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[username] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Kemono {username}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Kemono display name progress: {i + 1}/{len(kemono_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # TINAMI display name解決
        if tinami_missing and self.tinami_extractor:
            self.logger.info(f"Resolving {len(tinami_missing)} TINAMI display names...")
            for i, acc in enumerate(tinami_missing):
                prof_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.tinami_extractor.resolve_display_name, prof_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[prof_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve TINAMI prof_id {prof_id}: {exc}")
                    failed_count += 1

        # Poipiku display name解決
        if poipiku_missing and self.poipiku_extractor:
            self.logger.info(f"Resolving {len(poipiku_missing)} Poipiku display names...")
            for i, acc in enumerate(poipiku_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.poipiku_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Poipiku user_id {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Poipiku display name progress: {i + 1}/{len(poipiku_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Fantia display name解決
        if fantia_missing and self.fantia_extractor:
            self.logger.info(f"Resolving {len(fantia_missing)} Fantia display names...")
            for i, acc in enumerate(fantia_missing):
                fanclub_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.fantia_extractor.resolve_display_name, fanclub_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[fanclub_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Fantia fanclub {fanclub_id}: {exc}")
                    failed_count += 1

                await asyncio.sleep(3)  # Fantia API レート制限回避

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Fantia display name progress: {i + 1}/{len(fantia_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Nijie display name解決
        if nijie_missing and self.nijie_extractor:
            self.logger.info(f"Resolving {len(nijie_missing)} Nijie display names...")
            for i, acc in enumerate(nijie_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.nijie_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Nijie user_id {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Nijie display name progress: {i + 1}/{len(nijie_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Skeb display name解決
        if skeb_missing and self.skeb_extractor:
            self.logger.info(f"Resolving {len(skeb_missing)} Skeb display names...")
            for i, acc in enumerate(skeb_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.skeb_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Skeb user {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Skeb display name progress: {i + 1}/{len(skeb_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # bilibili display name解決
        if bilibili_missing and self.bilibili_extractor:
            self.logger.info(f"Resolving {len(bilibili_missing)} bilibili display names...")
            for i, acc in enumerate(bilibili_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.bilibili_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve bilibili user {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"bilibili display name progress: {i + 1}/{len(bilibili_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Misskey display name解決
        if misskey_missing and self.misskey_extractor:
            self.logger.info(f"Resolving {len(misskey_missing)} Misskey display names...")
            for i, acc in enumerate(misskey_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.misskey_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Misskey user {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Misskey display name progress: {i + 1}/{len(misskey_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # FANBOX display name解決
        if fanbox_missing and self.fanbox_extractor:
            self.logger.info(f"Resolving {len(fanbox_missing)} FANBOX display names...")
            for i, acc in enumerate(fanbox_missing):
                creator_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.fanbox_extractor.resolve_display_name, creator_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[creator_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve FANBOX creator {creator_id}: {exc}")
                    failed_count += 1

                await asyncio.sleep(3)  # FANBOX API レート制限回避

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"FANBOX display name progress: {i + 1}/{len(fanbox_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Bluesky display name解決
        if bluesky_missing and self.bluesky_extractor:
            self.logger.info(f"Resolving {len(bluesky_missing)} Bluesky display names...")
            for i, acc in enumerate(bluesky_missing):
                handle = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.bluesky_extractor.resolve_display_name, handle
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[handle] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Bluesky user {handle}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Bluesky display name progress: {i + 1}/{len(bluesky_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        # Privatter display name解決
        if privatter_missing and self.privatter_extractor:
            self.logger.info(f"Resolving {len(privatter_missing)} Privatter display names...")
            for i, acc in enumerate(privatter_missing):
                user_id = acc['username']
                try:
                    display_name = await asyncio.to_thread(
                        self.privatter_extractor.resolve_display_name, user_id
                    )
                    if display_name:
                        display_name = display_name.replace(',', ' ')
                        updates[user_id] = display_name
                        acc['display_name'] = display_name
                        resolved_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.warning(f"Failed to resolve Privatter user {user_id}: {exc}")
                    failed_count += 1

                if (i + 1) % 50 == 0:
                    self.logger.info(
                        f"Privatter display name progress: {i + 1}/{len(privatter_missing)} "
                        f"(resolved: {resolved_count}, failed: {failed_count})"
                    )

        if updates:
            self._update_csv_display_names(updates)
            self._sync_rank_by_display_name()
            self.logger.info(
                f"Display name resolution complete: {resolved_count} resolved, {failed_count} failed"
            )
        else:
            self.logger.warning(f"No display names could be resolved ({failed_count} failed)")

    def _sync_rank_by_display_name(self) -> None:
        """display_nameが既存アカウントと一致し、rankが未設定の行にrankをコピーする。"""
        csv_path = Path("monitored_accounts.csv")
        if not csv_path.exists():
            return

        try:
            rows = []
            with csv_path.open('r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    rows.append(row)

            if not fieldnames:
                return

            # display_name → rank のマッピングを構築（rank設定済みのもの）
            name_to_rank: Dict[str, str] = {}
            for row in rows:
                dn = (row.get('display_name') or '').strip()
                rank = (row.get('rank') or '').strip()
                if dn and rank and dn not in name_to_rank:
                    name_to_rank[dn] = rank

            if not name_to_rank:
                return

            # rankが空でdisplay_nameが一致する行を更新
            updated_count = 0
            for row in rows:
                current_rank = (row.get('rank') or '').strip()
                if current_rank:
                    continue
                dn = (row.get('display_name') or '').strip()
                if dn and dn in name_to_rank:
                    row['rank'] = name_to_rank[dn]
                    updated_count += 1
                    self.logger.info(
                        f"Rank synced for {row.get('username')}: "
                        f"display_name={dn!r} → rank={name_to_rank[dn]}"
                    )

            if updated_count:
                with csv_path.open('w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                self.logger.info(f"Synced rank for {updated_count} account(s) by display_name match")

        except Exception as e:
            self.logger.error(f"Failed to sync rank by display_name: {e}", exc_info=True)

    def _update_csv_display_names(self, updates: Dict[str, str]) -> None:
        """CSVファイル内のdisplay_nameを更新する（空欄のものだけ）"""
        csv_path = Path("monitored_accounts.csv")
        if not csv_path.exists():
            self.logger.error("CSV file not found for display name update")
            return

        try:
            # CSVを全行読み込み
            rows = []
            with csv_path.open('r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    rows.append(row)

            if not fieldnames:
                self.logger.error("CSV file has no headers")
                return

            # display_nameが空の行を更新
            updated_count = 0
            for row in rows:
                username = row.get('username', '')
                current_display = row.get('display_name', '')
                if not current_display.strip() and username in updates:
                    row['display_name'] = updates[username]
                    updated_count += 1

            # 書き戻し
            with csv_path.open('w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            self.logger.info(f"Updated {updated_count} display names in CSV")

        except Exception as e:
            self.logger.error(f"Failed to update CSV display names: {e}", exc_info=True)

    def _archive_expired_accounts(self) -> None:
        """30日超過フラグ済みアカウントを monitored_accounts.csv → deleted_accounts.csv へ移動"""
        try:
            expired = self.account_status_tracker.get_expired_accounts()
            if not expired:
                return

            usernames_to_remove = {acc["username"] for acc in expired}
            self.logger.warning(
                f"Archiving {len(expired)} account(s) flagged for >{self.account_status_tracker.expiry_days} days: "
                + ", ".join(usernames_to_remove)
            )

            csv_path = Path("monitored_accounts.csv")
            deleted_csv_path = Path("deleted_accounts.csv")

            if not csv_path.exists():
                self.logger.error("monitored_accounts.csv not found for archival")
                return

            # CSVを全行読み込み
            rows = []
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    rows.append(row)

            if not fieldnames:
                self.logger.error("CSV file has no headers")
                return

            # 残留行とアーカイブ行を分離
            rows_to_keep = []
            rows_to_archive = []
            for row in rows:
                if row.get("username", "") in usernames_to_remove:
                    rows_to_archive.append(row)
                else:
                    rows_to_keep.append(row)

            if not rows_to_archive:
                self.logger.info("No matching rows found in CSV to archive")
                return

            # deleted_accounts.csv に追記
            deleted_exists = deleted_csv_path.exists()
            deleted_fieldnames = list(fieldnames)
            if "deleted_at" not in deleted_fieldnames:
                deleted_fieldnames.append("deleted_at")
            if "deletion_reason" not in deleted_fieldnames:
                deleted_fieldnames.append("deletion_reason")

            now = datetime.now().isoformat()
            with deleted_csv_path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=deleted_fieldnames)
                if not deleted_exists:
                    writer.writeheader()
                for row in rows_to_archive:
                    row["deleted_at"] = now
                    row["deletion_reason"] = "unreachable_30_days"
                    writer.writerow(row)

            # monitored_accounts.csv を書き戻し（アーカイブ行を除去）
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows_to_keep)

            # トラッカーからエントリ削除
            for acc in expired:
                self.account_status_tracker.remove_account(acc["username"])
            self.account_status_tracker.save()

            # メモリ上のアカウントリストもリロード
            self.config['monitored_accounts'] = self._load_monitored_accounts_from_csv(
                "monitored_accounts.csv"
            )

            self.logger.info(
                f"Archived {len(rows_to_archive)} account(s) to deleted_accounts.csv: "
                + ", ".join(r.get("username", "?") for r in rows_to_archive)
            )
            self._schedule_csv_git_sync("archive")

        except Exception as e:
            self.logger.error(f"Failed to archive expired accounts: {e}", exc_info=True)

    def _schedule_csv_git_sync(self, reason: str) -> None:
        if not self.csv_git_syncer.enabled:
            return

        if self.csv_git_sync_task and not self.csv_git_sync_task.done():
            self.logger.info("CSV git sync already running; skip scheduling (%s)", reason)
            self.csv_git_sync_pending_reason = reason
            return

        self.csv_git_sync_task = asyncio.create_task(self._run_csv_git_sync(reason))

    async def _run_csv_git_sync(self, reason: str) -> None:
        try:
            ok = await self.csv_git_syncer.sync(reason)
            if not ok:
                self.logger.info("CSV git sync did not push changes (%s)", reason)
        except Exception as exc:
            self.logger.error("CSV git sync task failed: %s", exc, exc_info=True)
            self.status_notifier.notify_error(str(exc), "CSV git sync task failed")
        finally:
            next_reason = self.csv_git_sync_pending_reason
            self.csv_git_sync_pending_reason = None
            if next_reason:
                self.logger.info("Scheduling pending CSV git sync (%s)", next_reason)
                self.csv_git_sync_task = asyncio.create_task(self._run_csv_git_sync(next_reason))

    async def _wait_csv_git_sync(self) -> None:
        while self.csv_git_sync_task and not self.csv_git_sync_task.done():
            self.logger.info("Waiting for CSV git sync to finish...")
            task = self.csv_git_sync_task
            await task
            if self.csv_git_sync_task is task:
                break

    async def run_once(self, is_daemon: bool = False):
        """一度だけ実行する（手動実行用）
        
        Args:
            is_daemon: デーモンモードから呼ばれた場合はTrue
        """
        # temp_images_backupディレクトリが残っていたら削除
        import shutil
        temp_images_dir = Path("temp_images_backup")
        if temp_images_dir.exists():
            shutil.rmtree(temp_images_dir)
        
        # imagesディレクトリを作成
        images_dir = Path("images")
        images_dir.mkdir(exist_ok=True)
        
        # dataディレクトリを作成
        data_dir = Path("data")
        data_dir.mkdir(exist_ok=True)
        
        # ロガーを初期化（logsディレクトリにログを保存）
        if self.logger is None:
            self.logger = setup_logging(self.config['system']['log_level'])
        
        self.logger.info("EventMonitor started (single run)")
        self.runtime_prioritized_accounts.clear()

        # ステータス通知：開始
        self.status_notifier.notify_starting()

        # 到達性キャッシュをクリア（デーモンモードでのサイクル間キャリーオーバー防止）
        self.twitter_monitor.clear_reachability_cache()
        if self.pixiv_extractor:
            self.pixiv_extractor.clear_reachability_cache()
        if self.kemono_extractor:
            self.kemono_extractor.clear_reachability_cache()
        if self.tinami_extractor:
            self.tinami_extractor.clear_reachability_cache()
        if self.poipiku_extractor:
            self.poipiku_extractor.clear_reachability_cache()
        if self.fantia_extractor:
            self.fantia_extractor.clear_reachability_cache()
        if self.nijie_extractor:
            self.nijie_extractor.clear_reachability_cache()
        if self.skeb_extractor:
            self.skeb_extractor.clear_reachability_cache()
        if self.bilibili_extractor:
            self.bilibili_extractor.clear_reachability_cache()
        if self.misskey_extractor:
            self.misskey_extractor.clear_reachability_cache()
        if self.gelbooru_extractor:
            self.gelbooru_extractor.clear_reachability_cache()
        if self.fanbox_extractor:
            self.fanbox_extractor.clear_reachability_cache()
        if self.bluesky_extractor:
            self.bluesky_extractor.clear_reachability_cache()
        if self.privatter_extractor:
            self.privatter_extractor.clear_reachability_cache()

        await self._ingest_discord_accounts()

        # display_nameが空のエントリを自動補完
        await self._resolve_missing_display_names()
        self._schedule_csv_git_sync("startup")

        total_discord_servers = sum(
            1 for account in self.config['monitored_accounts']
            if account.get('platform') == 'discord'
        )
        total_accounts = len(self.config['monitored_accounts']) - total_discord_servers
        self.status_notifier.set_target_counts(
            total_accounts=total_accounts,
            total_discord_servers=total_discord_servers,
        )
        self.logger.info(
            "Status targets prepared: accounts=%s, discord_servers=%s",
            total_accounts,
            total_discord_servers,
        )
        
        # HydrusClientをコンテキストマネージャーとして使用
        async with self.hydrus_client:
            discord_task: Optional[asyncio.Task] = None
            try:
                # ステータス通知：実行中
                self.status_notifier.notify_running()
                # 0. 未アップロード分を最初に処理（HuggingFaceまたはHydrusが有効な場合）
                if self.backup_manager.backup_config.get('enabled', False) or self.hydrus_client.enabled:
                    try:
                        self.logger.info("Checking for unprocessed media in database...")
                        await self.backup_manager.upload_remaining_media(hydrus_client=self.hydrus_client)
                        self.logger.info("Unprocessed media upload completed")
                    except Exception as e:
                        self.logger.error(f"Unprocessed media upload failed: {e}", exc_info=True)
                        # エラーが発生しても新規ツイートの処理は継続

                # 0.5 Hydrus未インポート作品のリトライ（前回中断分）
                if self.hydrus_client.enabled:
                    try:
                        await self.account_processor.retry_pending_hydrus_imports()
                    except Exception as e:
                        self.logger.error(f"Hydrus import retry failed: {e}", exc_info=True)

                self.account_processor.schedule_pending_event_detection("startup pending tweets")

                # プラットフォーム別にアカウントをグルーピング
                platform_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for account in self.config['monitored_accounts']:
                    platform = account.get('platform', 'twitter')
                    platform_groups[platform].append(account)

                self.logger.info(
                    f"Platform groups: {', '.join(f'{p}({len(a)})' for p, a in platform_groups.items())}"
                )

                # クロール優先順位でソート
                for platform in platform_groups:
                    platform_groups[platform] = self._sort_accounts_by_priority(
                        platform, platform_groups[platform]
                    )

                # 並列処理の設定を取得（同一プラットフォーム内の並行数）
                parallel_config = self.config.get('system', {}).get('parallel_processing', {})
                max_concurrent = parallel_config.get('max_concurrent_accounts', 1)

                async def process_platform_group(platform: str, accounts: List[Dict[str, Any]]):
                    """プラットフォーム単位でアカウントを処理"""
                    self.logger.info(f"Starting {platform} group ({len(accounts)} accounts)")
                    semaphore = asyncio.Semaphore(max_concurrent)
                    for account in accounts:
                        if self.shutdown.requested:
                            self.logger.info(
                                f"シャットダウン要求により残りの{platform}アカウントをスキップします"
                            )
                            break
                        await self.account_processor.process_account(account, semaphore)
                    self.logger.info(f"Completed {platform} group")

                # discord は長時間かかる可能性があるため、他グループと分離して
                # 後処理をブロックしないようにする
                blocking_groups = {
                    p: a for p, a in platform_groups.items() if p != "discord"
                }
                discord_accounts = platform_groups.get("discord")

                # discord をバックグラウンドタスクとして起動
                if discord_accounts:
                    discord_task = asyncio.create_task(
                        process_platform_group("discord", discord_accounts)
                    )

                # 残りのプラットフォームを並行実行して待つ
                if len(blocking_groups) > 1:
                    tasks = [
                        process_platform_group(platform, accounts)
                        for platform, accounts in blocking_groups.items()
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
                elif blocking_groups:
                    for platform, accounts in blocking_groups.items():
                        await process_platform_group(platform, accounts)

                try:
                    await self.account_processor.resume_pending_twitter_work()
                except Exception as e:
                    self.logger.error(f"Twitter pending work resume failed: {e}", exc_info=True)

                # 全アカウント処理後: 30日超過フラグ済みアカウントをアーカイブ
                self._archive_expired_accounts()
            except Exception as e:
                self.logger.error(f"Error in run_once: {e}", exc_info=True)
                # ステータス通知：エラー
                import traceback
                self.status_notifier.notify_error(str(e), traceback.format_exc())
                raise
            finally:
                # TwitterMonitorのクリーンアップ
                await self.twitter_monitor.cleanup()
                if self.shutdown.requested:
                    self.logger.info("グレースフルシャットダウン: アカウント処理を中断しました")
                    # シャットダウン時はdiscordタスクもキャンセル
                    if discord_task and not discord_task.done():
                        self.logger.info("Discordエクスポートをキャンセルします...")
                        discord_task.cancel()
                        try:
                            await discord_task
                        except asyncio.CancelledError:
                            pass

            # シャットダウン要求時は後処理をスキップ
            if self.shutdown.requested:
                self.logger.info("シャットダウン要求のため後処理をスキップします")
                return

            # 6. 全アカウント処理後、データベースファイルをバックアップ
            if self.backup_manager.backup_config.get('enabled', False):
                try:
                    self.logger.info("Uploading database backup...")
                    await self.backup_manager.upload_database_backup()
                    self.logger.info("Database backup completed")
                except Exception as e:
                    self.logger.error(f"Database backup failed: {e}")

                # クリエイターマッピング生成・アップロード
                try:
                    self.logger.info("クリエイターマッピングを生成・アップロード中...")
                    await self.backup_manager.generate_and_upload_creator_mapping()
                    self.logger.info("クリエイターマッピング完了")
                except Exception as e:
                    self.logger.error(f"クリエイターマッピング失敗: {e}")

            # 7. Hydrus perceptual hash重複検知（後処理）
            if self.hydrus_dedup.is_active:
                try:
                    self.logger.info("Hydrus perceptual hash dedup処理を開始...")
                    async with self.hydrus_dedup as dedup:
                        stats = await dedup.process_duplicates()
                        self.logger.info(
                            f"Hydrus dedup完了: 処理={stats['processed']}, "
                            f"スキップ={stats['skipped']}, 失敗={stats['failed']}"
                        )
                except Exception as e:
                    self.logger.error(f"Hydrus dedup処理エラー: {e}", exc_info=True)

            # 古い画像のクリーンアップ
            await self._cleanup_old_images()
            await self._wait_csv_git_sync()

            # discord タスクがまだ実行中なら待つ
            if discord_task and not discord_task.done():
                self.logger.info("Discordエクスポートがまだ実行中です。完了を待機します...")
                try:
                    await discord_task
                except Exception as e:
                    self.logger.error(f"Discordエクスポートエラー: {e}", exc_info=True)
            elif discord_task and discord_task.done():
                # 既に完了しているが例外があればログ
                exc = discord_task.exception() if not discord_task.cancelled() else None
                if exc:
                    self.logger.error(f"Discordエクスポートエラー: {exc}", exc_info=True)

            # ステータス通知：停止（単発実行の場合のみ）
            if not is_daemon:
                self.status_notifier.notify_stopped()
    

    async def _cleanup_old_images(self):
        """古い画像ファイルを削除"""
        try:
            # クリーンアップが無効な場合はスキップ
            if not self.config.get('image_settings', {}).get('cleanup_enabled', True):
                self.logger.debug("Image cleanup is disabled")
                return
            
            retention_days = self.config.get('image_settings', {}).get('retention_days', 30)
            cutoff_date = datetime.now() - timedelta(days=retention_days)
            
            images_dir = Path("images")
            if not images_dir.exists():
                return
            
            deleted_count = 0
            for user_dir in images_dir.iterdir():
                if user_dir.is_dir():
                    for image_file in user_dir.glob("*.jpg"):
                        # ファイルの更新時刻を確認
                        if datetime.fromtimestamp(image_file.stat().st_mtime) < cutoff_date:
                            image_file.unlink()
                            deleted_count += 1
            
            if deleted_count > 0:
                self.logger.info(f"Cleaned up {deleted_count} old images (older than {retention_days} days)")
        except Exception as e:
            self.logger.error(f"Error during image cleanup: {e}")
    
    async def run_continuous(self):
        """継続的に実行する（デーモンモード）"""
        # dataディレクトリを作成
        data_dir = Path("data")
        data_dir.mkdir(exist_ok=True)
        
        # 初回実行時のロガー初期化
        if self.logger is None:
            self.logger = setup_logging(self.config['system']['log_level'])
        
        self.logger.info("EventMonitor started (continuous mode)")
        
        interval = self.config['system']['check_interval'] * 60  # 分を秒に変換
        
        while True:
            try:
                await self.run_once(is_daemon=True)
                if self.shutdown.requested:
                    self.logger.info("グレースフルシャットダウンによりデーモンモードを終了します")
                    break
                self.logger.info(f"Waiting {self.config['system']['check_interval']} minutes until next check...")
                # ステータス通知：待機中（次のサイクルまで待機）
                self.status_notifier.notify_idle()
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                self.logger.info("EventMonitor stopped by user")
                break
            except Exception as e:
                self.logger.error(f"Error in continuous run: {e}", exc_info=True)
                # ステータス通知：エラー（但しループは継続）
                import traceback
                self.status_notifier.notify_error(str(e), traceback.format_exc())
                # エラーが発生しても継続
                await asyncio.sleep(interval)
    
_shutdown_instance: GracefulShutdown = None


async def main():
    global _shutdown_instance
    # .envファイルを読み込む
    load_dotenv()

    shutdown = GracefulShutdown()
    _shutdown_instance = shutdown

    # コマンドライン引数をチェック
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        monitor = EventMonitor(shutdown=shutdown)
        await monitor.run_continuous()
    else:
        monitor = EventMonitor(shutdown=shutdown)
        await monitor.run_once()


def _sigint_handler(signum, frame):
    """Ctrl+Cシグナルハンドラ: 1回目はグレースフル停止、2回目は強制終了"""
    if _shutdown_instance is not None:
        _shutdown_instance.request()
    else:
        raise KeyboardInterrupt


if __name__ == "__main__":
    import sys
    import gc

    # Python 3.11以降の非同期ジェネレーター警告を抑制
    if sys.version_info >= (3, 11):
        import warnings
        warnings.filterwarnings("ignore", category=RuntimeWarning,
                              message=".*asynchronous generator.*")

    # グレースフルシャットダウン用シグナルハンドラを登録
    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        # asyncio.run()を使用（Python 3.7+）
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n強制終了しました")
    finally:
        # すべてのtempディレクトリをクリーンアップ
        import shutil
        temp_dirs_to_cleanup = [
            "temp_images_backup",
            ".rclone_temp",
            "temp_upload",
            "test_upload_temp",
            "eventmonitor_encrypted_files"
        ]
        
        for temp_dir in temp_dirs_to_cleanup:
            temp_path = Path(temp_dir)
            if temp_path.exists():
                try:
                    shutil.rmtree(temp_path)
                    print(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    print(f"Failed to clean up {temp_dir}: {e}")
        
        # temp_uploadで始まるディレクトリも削除
        try:
            for temp_path in Path(".").glob("temp_upload_*"):
                if temp_path.is_dir():
                    shutil.rmtree(temp_path)
                    print(f"Cleaned up temp directory: {temp_path}")
        except Exception as e:
            print(f"Failed to clean up temp_upload_* directories: {e}")
        
        # ガベージコレクションを強制実行
        gc.collect()
        
        # 少し待機
        import time
        time.sleep(0.5)
