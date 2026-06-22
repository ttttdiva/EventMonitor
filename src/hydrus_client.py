import aiohttp
import asyncio
import csv
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from .path_utils import to_absolute_path

logger = logging.getLogger("EventMonitor.HydrusClient")


class HydrusClient:
    """Hydrus Client APIとの連携を管理するクラス"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初期化

        Args:
            config: 全体のconfig（hydrusセクションを含む）
        """
        # 全体のconfigを保存（file_paths等へのアクセスのため）
        self.config = config

        # hydrusセクションを取得
        hydrus_config = config.get('hydrus', {})

        self.enabled = hydrus_config.get('enabled', False)
        self.api_url = hydrus_config.get('api_url', 'http://127.0.0.1:45869')
        # 環境変数を優先、なければconfig.yamlから取得
        self.access_key = os.environ.get('HYDRUS_ACCESS_KEY') or hydrus_config.get('access_key')
        # プラットフォーム別タグサービス設定
        self._tag_services_config: Dict[str, str] = hydrus_config.get('tag_services', {})
        self._legacy_tag_service_key: str = hydrus_config.get('tag_service_key', '6c6f63616c2074616773')  # "local tags"
        # validate_tag_services()で解決される
        self._platform_to_service_key: Dict[str, str] = {}
        self._platform_to_service_name: Dict[str, str] = {}

        self.import_settings = hydrus_config.get('import_settings', {})
        self.tag_settings = hydrus_config.get('tag_settings', {})
        self._csv_creator_map: Optional[Dict[str, Tuple[str, str, str]]] = None

        self.session: Optional[aiohttp.ClientSession] = None
        self._session_key: Optional[str] = None
        
        if self.enabled and not self.access_key:
            logger.warning("Hydrus連携が有効ですが、access_keyが設定されていません")
            self.enabled = False

    @property
    def tag_service_key(self) -> str:
        """後方互換: メタデータチェック用に最初のサービスキーを返す"""
        if self._platform_to_service_key:
            return next(iter(self._platform_to_service_key.values()))
        return self._legacy_tag_service_key

    @property
    def all_tag_service_keys(self) -> List[str]:
        """設定された全タグサービスキーのリスト"""
        keys = [self._legacy_tag_service_key]
        keys.extend(self._platform_to_service_key.values())
        return list(dict.fromkeys(keys))

    async def __aenter__(self):
        """非同期コンテキストマネージャーのエンター"""
        if self.enabled:
            self.session = aiohttp.ClientSession()
            while True:
                try:
                    await self._get_session_key()
                    await self._validate_tag_services()
                    break
                except (RuntimeError, aiohttp.ClientError) as e:
                    logger.warning(f"Hydrus API接続失敗: {e}")
                    print(f"\n[Hydrus] APIに接続できません: {e}")
                    print("[R] リトライ / [S] Hydrusを無効化して続行")
                    choice = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("選択 (R/S): ").strip().upper()
                    )
                    if choice != "R":
                        logger.info("ユーザー操作によりHydrus連携を無効化して続行します")
                        await self.session.close()
                        self.session = None
                        self.enabled = False
                        break
                    logger.info("Hydrus API接続をリトライします")
        return self

    async def _validate_tag_services(self) -> None:
        """設定されたタグサービス名がHydrus側に存在するか検証し、サービスキーを解決する"""
        if not self._tag_services_config:
            logger.info("tag_services未設定: レガシーモード（単一タグサービス）で動作します")
            return

        try:
            headers = self._get_headers()
            async with self.session.get(
                f"{self.api_url}/get_services",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"タグサービス一覧の取得に失敗しました: HTTP {resp.status}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"タグサービス検証のためのHydrus API接続に失敗しました: {e}")

        # カテゴリ別リスト（local_tags, tag_repositories等）からname→keyマッピングを構築
        available_services: Dict[str, str] = {}
        for category in ('local_tags', 'tag_repositories'):
            for svc in data.get(category, []):
                if isinstance(svc, dict) and 'name' in svc and 'service_key' in svc:
                    available_services[svc['name']] = svc['service_key']

        logger.info(f"Hydrusで利用可能なタグサービス: {list(available_services.keys())}")

        # 各プラットフォームのサービス名を検証
        missing = []
        for platform, service_name in self._tag_services_config.items():
            if service_name not in available_services:
                missing.append(f"{platform} -> '{service_name}'")
            else:
                key = available_services[service_name]
                self._platform_to_service_key[platform] = key
                self._platform_to_service_name[platform] = service_name
                logger.info(f"タグサービス解決: {platform} -> '{service_name}' (key: {key})")

        if missing:
            logger.warning(
                f"Hydrusにタグサービスが見つかりません: {', '.join(missing)}。"
                f"利用可能なサービス: {list(available_services.keys())}。"
                f"該当プラットフォームはレガシーモード（service_keys_to_actions_to_tags）にフォールバックします。"
            )
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャーのイグジット"""
        if self.session:
            await self.session.close()
    
    async def _get_session_key(self) -> Optional[str]:
        """セッションキーを取得（24時間有効）"""
        if not self.enabled:
            return None
            
        try:
            headers = {'Hydrus-Client-API-Access-Key': self.access_key}
            async with self.session.get(f"{self.api_url}/session_key", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._session_key = data.get('session_key')
                    logger.info("Hydrus APIセッションキーを取得しました")
                    return self._session_key
                else:
                    logger.error(f"セッションキー取得エラー: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Hydrus API接続エラー: {e}")
            return None
    
    def _get_headers(self) -> Dict[str, str]:
        """APIリクエスト用のヘッダーを取得"""
        if self._session_key:
            return {'Hydrus-Client-API-Session-Key': self._session_key}
        else:
            return {'Hydrus-Client-API-Access-Key': self.access_key}
    
    async def import_file(self, file_path: Path) -> Optional[str]:
        """
        ファイルをHydrusにインポート
        
        Args:
            file_path: インポートするファイルのパス
            
        Returns:
            成功時はファイルのSHA256ハッシュ、失敗時はNone
        """
        if not self.enabled:
            return None
        
        # 許可する拡張子のホワイトリスト（画像+GIFのみ）
        allowed_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.avif', '.gif'}
        if file_path.suffix.lower() not in allowed_extensions:
            logger.info(f"非画像ファイルはスキップします: {file_path}")
            return None

        # 0バイトファイルをスキップ（gallery-dlのダウンロード失敗時に発生）
        try:
            if file_path.stat().st_size == 0:
                logger.warning(f"0バイトファイルをスキップ: {file_path}")
                return None
        except OSError:
            logger.warning(f"ファイルにアクセスできません: {file_path}")
            return None
            
        try:
            # ファイルハッシュを計算
            file_hash = self._calculate_file_hash(file_path)
            
            # 既存チェック（メタデータがあるかも確認）
            if self.import_settings.get('skip_existing', True):
                exists, has_metadata = await self._check_file_exists_with_metadata(file_hash)
                if exists and has_metadata:
                    logger.info(f"ファイルは既にHydrusに存在し、メタデータもあります: {file_path}")
                    return file_hash
                elif exists and not has_metadata:
                    logger.info(f"ファイルは存在しますが、メタデータが削除されています。再インポートをスキップしてタグのみ追加します: {file_path}")
                    return file_hash
            
            # ファイルをインポート
            headers = self._get_headers()
            headers['Content-Type'] = 'application/octet-stream'
            
            # ファイルをストリーミングで送信（メモリ効率化）
            async with self.session.post(
                f"{self.api_url}/add_files/add_file",
                headers=headers,
                data=open(file_path, 'rb')
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    status = result.get('status')
                    logger.info(f"Import status for {file_path}: {status}")
                    if status in [1, 2]:  # 1=success, 2=already in db
                        if status == 2:
                            logger.info(f"ファイルは既にDBに存在: {file_path}")
                        else:
                            logger.info(f"ファイルをインポートしました: {file_path}")
                        return result.get('hash')
                    elif status == 3:  # 3=previously deleted
                        logger.info(f"ファイルはHydrusで削除済み、再インポートしない: {file_path}")
                        return result.get('hash')  # ハッシュを返してインポート済みカウントを進める
                    else:
                        logger.error(f"インポート失敗: {result}")
                        return None
                else:
                    logger.error(f"インポートAPIエラー: {resp.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"ファイルインポートエラー: {e}")
            return None
    
    async def add_tags(self, file_hash: str, tags: List[str], platform: Optional[str] = None) -> bool:
        """
        ファイルにタグを追加

        Args:
            file_hash: ファイルのSHA256ハッシュ
            tags: 追加するタグのリスト
            platform: プラットフォーム名（"twitter", "pixiv"等）。tag_services設定時に振り分けに使用

        Returns:
            成功時True、失敗時False
        """
        if not self.enabled or not tags:
            return False

        try:
            logger.debug(f"タグ追加開始: {file_hash}")
            logger.debug(f"追加するタグ: {tags}")

            # title:タグの存在確認
            title_tags_to_add = [tag for tag in tags if tag.startswith('title:')]
            if title_tags_to_add:
                logger.info(f"title:タグを追加します: {title_tags_to_add[0][:100]}...")
            else:
                logger.warning("追加するタグにtitle:タグが含まれていません")

            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'

            # プラットフォーム別タグサービスが解決済みの場合
            service_name = self._platform_to_service_name.get(platform) if platform else None
            if service_name:
                data = {
                    'hashes': [file_hash],
                    'service_names_to_actions_to_tags': {
                        service_name: {
                            '0': tags
                        }
                    },
                    'override_previously_deleted_mappings': True,
                }
                logger.debug(f"タグ送信先サービス: '{service_name}' (platform: {platform})")
            else:
                # レガシーモード: 単一サービスキーを使用
                data = {
                    'hashes': [file_hash],
                    'service_keys_to_actions_to_tags': {
                        self._legacy_tag_service_key: {
                            '0': tags
                        }
                    },
                    'override_previously_deleted_mappings': True,
                }
            
            async with self.session.post(
                f"{self.api_url}/add_tags/add_tags",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"タグを追加しました: {len(tags)}個")
                    return True
                else:
                    logger.error(f"タグ追加APIエラー: {resp.status}")
                    error_text = await resp.text()
                    logger.error(f"エラー詳細: {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"タグ追加エラー: {e}")
            return False
    
    async def _get_all_local_tag_service_keys(self) -> List[str]:
        """Hydrusから全ローカルタグサービスのキーを取得する"""
        try:
            headers = self._get_headers()
            async with self.session.get(
                f"{self.api_url}/get_services",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    return [self._legacy_tag_service_key]
                data = await resp.json()

            keys = []
            for svc in data.get('local_tags', []):
                if isinstance(svc, dict) and 'service_key' in svc:
                    keys.append(svc['service_key'])
            return keys if keys else [self._legacy_tag_service_key]
        except Exception:
            return [self._legacy_tag_service_key]

    async def remove_tags_bulk(
        self, file_hashes: List[str], tags: List[str], all_services: bool = False
    ) -> bool:
        """
        複数ファイルから同じタグを一括削除

        Args:
            file_hashes: ファイルのSHA256ハッシュのリスト
            tags: 削除するタグのリスト
            all_services: Trueの場合、Hydrusの全ローカルタグサービスから削除する

        Returns:
            成功時True、失敗時False
        """
        if not self.enabled or not tags or not file_hashes:
            return False

        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'

            if all_services:
                service_keys = await self._get_all_local_tag_service_keys()
                service_keys_actions = {key: {'1': tags} for key in service_keys}
                data = {
                    'hashes': file_hashes,
                    'service_keys_to_actions_to_tags': service_keys_actions,
                }
            else:
                data = {
                    'hashes': file_hashes,
                    'service_keys_to_actions_to_tags': {
                        self._legacy_tag_service_key: {
                            '1': tags
                        }
                    },
                }

            async with self.session.post(
                f"{self.api_url}/add_tags/add_tags",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"タグを一括削除しました: {tags} ({len(file_hashes)} ファイル)")
                    return True
                else:
                    logger.error(f"タグ一括削除APIエラー: {resp.status}")
                    return False

        except Exception as e:
            logger.error(f"タグ一括削除エラー: {e}")
            return False

    async def add_tags_bulk(
        self, file_hashes: List[str], tags: List[str], platform: Optional[str] = None
    ) -> bool:
        """
        複数ファイルに同じタグを一括追加

        Args:
            file_hashes: ファイルのSHA256ハッシュのリスト
            tags: 追加するタグのリスト
            platform: プラットフォーム名

        Returns:
            成功時True、失敗時False
        """
        if not self.enabled or not tags or not file_hashes:
            return False

        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'

            service_name = self._platform_to_service_name.get(platform) if platform else None
            if service_name:
                data = {
                    'hashes': file_hashes,
                    'service_names_to_actions_to_tags': {
                        service_name: {'0': tags}
                    },
                    'override_previously_deleted_mappings': True,
                }
            else:
                data = {
                    'hashes': file_hashes,
                    'service_keys_to_actions_to_tags': {
                        self._legacy_tag_service_key: {'0': tags}
                    },
                    'override_previously_deleted_mappings': True,
                }

            async with self.session.post(
                f"{self.api_url}/add_tags/add_tags",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"タグを一括追加しました: {tags} ({len(file_hashes)} ファイル)")
                    return True
                else:
                    logger.error(f"タグ一括追加APIエラー: {resp.status}")
                    return False

        except Exception as e:
            logger.error(f"タグ一括追加エラー: {e}")
            return False

    async def remove_tags(
        self, file_hash: str, tags: List[str], all_services: bool = False
    ) -> bool:
        """
        ファイルからタグを削除

        Args:
            file_hash: ファイルのSHA256ハッシュ
            tags: 削除するタグのリスト
            all_services: Trueの場合、Hydrusの全ローカルタグサービスから削除する

        Returns:
            成功時True、失敗時False
        """
        return await self.remove_tags_bulk([file_hash], tags, all_services=all_services)

    async def import_tweet_images(self, tweet_data: Dict[str, Any],
                                 local_media: List[str]) -> List[Tuple[str, str]]:
        """
        ツイートの画像をタグ付きでインポート

        Args:
            tweet_data: ツイートデータ
            local_media: ローカル画像パスのリスト

        Returns:
            インポートされたファイルの(パス, ハッシュ)のリスト
        """
        logger.info(f"import_tweet_images called for tweet {tweet_data.get('id')} with {len(local_media) if local_media else 0} images")
        if not self.enabled or not local_media:
            return []

        imported = []

        # Hydrus側の取り込み順が崩れないよう、メディアをtweet内の通し番号で並べ替える
        ordered_media = self._sort_media_paths(local_media)

        # ツイートURLを生成
        tweet_id = tweet_data.get('id')
        username = tweet_data.get('username')
        tweet_url = f"https://twitter.com/{username}/status/{tweet_id}" if tweet_id and username else None

        for image_path in ordered_media:
            # 相対パスを絶対パスに変換
            file_path = to_absolute_path(image_path, self.config)

            if not file_path.exists():
                logger.warning(f"画像ファイルが見つかりません: {image_path} -> {file_path}")
                continue
            
            # images/ディレクトリのファイルのみ処理（videos/は動画・音声ファイルなのでスキップ）
            # Windows対応: パス区切り文字を正規化してチェック
            path_str = str(file_path).replace('\\', '/')
            if 'images/' not in path_str:
                logger.info(f"images/ディレクトリ外のファイルはスキップ: {file_path}")
                continue
                
            # ファイルをインポート（または既存ファイルのハッシュを取得）
            logger.info(f"Importing file: {file_path}")
            file_hash = await self.import_file(file_path)
            logger.info(f"Import returned hash: {file_hash}")
            if not file_hash:
                logger.error(f"Failed to get file hash for: {file_path}")
                continue
                
            # ツイートURLをknown URLとして関連付け（常に実行）
            if tweet_url:
                logger.info(f"Associating URL to file: {tweet_url}")
                await self.associate_url(file_hash, tweet_url)
                
            # タグを生成（既存ファイルでも常に実行）
            logger.info(f"Generating tags for tweet {tweet_id}")
            tags = self._generate_tags(tweet_data)
            logger.info(f"Generated tags: {tags}")
            
            # タグを追加（既存ファイルでも常に実行）
            logger.info(f"Adding tags to file {file_hash}")
            if await self.add_tags(file_hash, tags, platform="twitter"):
                imported.append((image_path, file_hash))
                logger.info(f"Successfully added tags to file: {file_hash}")
            else:
                logger.error(f"Failed to add tags to file {file_hash}")
                
            # ツイート全文をnoteとして追加
            tweet_text = tweet_data.get('content') or tweet_data.get('text', '')
            if tweet_text:
                # URLを除去してクリーンなテキストにする（改行は維持）
                cleaned_text = tweet_text.strip()
                # タブを空白に置換
                cleaned_text = cleaned_text.replace('\t', ' ')
                # t.coリンクを除去（TwitterのURL短縮）
                cleaned_text = re.sub(r'https?://t\.co/\S+', '', cleaned_text).strip()
                # 各行の前後の空白を削除
                lines = [line.strip() for line in cleaned_text.split('\n')]
                # 空行を削除して結合
                cleaned_text = '\n'.join(line for line in lines if line)
                
                if cleaned_text:
                    logger.info(f"Adding cleaned tweet text as note")
                    await self.add_note(file_hash, "twitter description", cleaned_text)
                
        return imported

    ARTWORK_TAG_CONFIG = {
        "pixiv": {"id_tag": "pixiv_id", "user_tag": "pixiv_user", "user_fields": ("username",)},
        "kemono": {
            "id_tag": "kemono_id",
            "user_tag": "kemono_user",
            "user_fields": ("username",),
            "extra_fields": (("service", "service"),),
            "raw_tags": False,
            "always_r18": True,
        },
        "tinami": {"id_tag": "tinami_id", "user_tag": "tinami_user", "user_fields": ("username",)},
        "poipiku": {"id_tag": "poipiku_id", "user_tag": "poipiku_user", "user_fields": ("username",)},
        "privatter": {"id_tag": "privatter_id", "user_tag": "privatter_user", "user_fields": ("username",)},
        "fantia": {"id_tag": "fantia_id", "user_tag": "fantia_user", "user_fields": ("fanclub_id", "username")},
        "nijie": {"id_tag": "nijie_id", "user_tag": "nijie_user", "user_fields": ("username",)},
        "skeb": {"id_tag": "skeb_id", "user_tag": "skeb_user", "user_fields": ("username",)},
        "bilibili": {"id_tag": "bilibili_id", "user_tag": "bilibili_user", "user_fields": ("username",)},
        "misskey": {"id_tag": "misskey_id", "user_tag": "misskey_user", "user_fields": ("username",)},
        "fanbox": {"id_tag": "fanbox_id", "user_tag": "fanbox_user", "user_fields": ("creator_id", "username")},
        "bluesky": {"id_tag": "bluesky_id", "user_tag": "bluesky_user", "user_fields": ("handle", "username")},
    }

    @staticmethod
    def _dedupe_tags(tags: List[str]) -> List[str]:
        return list(dict.fromkeys(tag for tag in tags if isinstance(tag, str) and tag))

    def _get_csv_creator_map(self) -> Dict[str, Tuple[str, str, str]]:
        if self._csv_creator_map is not None:
            return self._csv_creator_map

        accounts = self.config.get('monitored_accounts')
        if accounts is None:
            csv_path = Path("monitored_accounts.csv")
            accounts = []
            if csv_path.exists():
                try:
                    with csv_path.open("r", encoding="utf-8", newline="") as handle:
                        accounts = list(csv.DictReader(handle))
                except OSError as exc:
                    logger.warning(f"Failed to load monitored_accounts.csv for creator map: {exc}")

        creator_map: Dict[str, Tuple[str, str, str]] = {}
        for account in accounts or []:
            username = str(account.get("username") or "").strip()
            display_name = str(account.get("display_name") or "").strip()
            platform = str(account.get("platform") or "").strip()
            if not username or not display_name:
                continue
            creator_map[username.lower()] = (display_name, platform, username)

            twitter_id = str(account.get("twitter_id") or "").strip()
            if twitter_id:
                creator_map[twitter_id.lower()] = (display_name, "twitter", username)

        self._csv_creator_map = creator_map
        return creator_map

    def _resolve_csv_creator(self, raw_name: str) -> Optional[Tuple[str, str, str]]:
        return self._get_csv_creator_map().get(raw_name.strip().lower())

    @staticmethod
    def _platform_user_tag(platform: str, username: str) -> str:
        normalized = platform.strip().lower()
        if normalized in {"twitter", "x"}:
            return f"twitter_user:{username}"
        if normalized:
            return f"{normalized}_user:{username}"
        return ""

    def _platform_base_tags(self, platform: str) -> List[str]:
        source_tag = f"source:{platform}"
        tags: List[str] = []
        for base_tag in self.tag_settings.get('base_tags', []):
            tags.append(source_tag if base_tag == 'source:twitter' else base_tag)
        if source_tag not in tags:
            tags.append(source_tag)
        return tags

    @staticmethod
    def _first_text(work_data: Dict[str, Any], fields: Tuple[str, ...]) -> str:
        for field in fields:
            value = work_data.get(field)
            if value:
                return str(value)
        return ''

    def _artwork_note_text(self, work_data: Dict[str, Any]) -> str:
        return self._first_text(work_data, ('text', 'title', 'content'))

    def _kemono_note_text(self, work_data: Dict[str, Any]) -> str:
        title = self._first_text(work_data, ('text', 'title'))
        content = str(work_data.get('content') or '')
        if title and content:
            return f"{title}\n\n{content}"
        return title or content

    def _artwork_default_url(self, platform: str, work_data: Dict[str, Any]) -> str:
        work_id = work_data.get('id', '')
        username = work_data.get('username', '')
        if platform == 'pixiv':
            return f"https://www.pixiv.net/artworks/{work_id}"
        if platform == 'tinami':
            return f"https://www.tinami.com/view/{work_id}"
        if platform == 'poipiku':
            return f"https://poipiku.com/{username}/{work_id}.html"
        if platform == 'privatter':
            return f"https://privatter.net/i/{work_id}"
        if platform == 'fantia':
            return f"https://fantia.jp/posts/{work_id}"
        if platform == 'nijie':
            return f"https://nijie.info/view.php?id={work_id}"
        if platform == 'skeb':
            return f"https://skeb.jp/@{username}"
        if platform == 'bilibili':
            return f"https://www.bilibili.com/opus/{work_id}"
        if platform == 'misskey':
            host = work_data.get('instance_host', 'misskey.io')
            note_id = work_data.get('note_id', work_id)
            return f"https://{host}/notes/{note_id}"
        if platform == 'gelbooru':
            return f"https://gelbooru.com/index.php?page=post&s=view&id={work_id}"
        if platform == 'fanbox':
            creator_id = self._first_text(work_data, ('creator_id', 'username'))
            return f"https://www.fanbox.cc/@{creator_id}/posts/{work_id}"
        if platform == 'bluesky':
            handle = self._first_text(work_data, ('handle', 'username'))
            return f"https://bsky.app/profile/{handle}/post/{work_id}"
        return ''

    @staticmethod
    def _is_artwork_sensitive(work_data: Dict[str, Any]) -> bool:
        try:
            x_restrict = int(work_data.get('x_restrict') or 0)
        except (TypeError, ValueError):
            x_restrict = 0
        rating = str(work_data.get('rating', '')).lower()
        return bool(
            work_data.get('sensitive')
            or x_restrict >= 1
            or rating in {'sensitive', 'questionable', 'explicit'}
        )

    def _generate_artwork_tags(self, platform: str, work_data: Dict[str, Any]) -> List[str]:
        config = self.ARTWORK_TAG_CONFIG[platform]
        tags = self._platform_base_tags(platform)

        for tag_namespace, field_name in config.get('extra_fields', ()):
            value = work_data.get(field_name)
            if value:
                tags.append(f"{tag_namespace}:{value}")

        work_id = work_data.get('id')
        if work_id:
            tags.append(f"{config['id_tag']}:{work_id}")

        creator_format = self.tag_settings.get('creator_tag_format', 'creator:{name}')
        display_name = work_data.get('display_name', '')
        if display_name:
            tags.append(creator_format.format(name=display_name))

        user_value = self._first_text(work_data, tuple(config.get('user_fields', ('username',))))
        if user_value:
            tags.append(f"{config['user_tag']}:{user_value}")

        if self.tag_settings.get('include_title_tag', True):
            title = self._first_text(work_data, ('text', 'title'))
            if title:
                if len(title) > 100:
                    title = title[:97] + "..."
                tags.append(f"title:{title}")

        if config.get('raw_tags', True):
            raw_tags = work_data.get('tags', [])
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    if isinstance(tag, str) and tag:
                        tags.append(tag)

        if config.get('always_r18') or self._is_artwork_sensitive(work_data):
            tags.append('rating:r-18')

        tags.append(f"rank:{work_data.get('rank', 3)}")

        custom_tags = work_data.get('custom_tags', [])
        if custom_tags:
            tags.extend(custom_tags)

        return self._dedupe_tags(tags)

    async def _import_standard_artwork_images(
        self,
        platform: str,
        work_data: Dict[str, Any],
        local_media: List[str],
        *,
        note_name: str,
        note_builder=None,
        sort_media: bool = True,
        remove_r18_when_not_sensitive: bool = False,
    ) -> List[Tuple[str, str]]:
        if not self.enabled or not local_media:
            return []

        imported: List[Tuple[str, str]] = []
        ordered_media = self._sort_media_paths(local_media) if sort_media else list(local_media)
        work_url = work_data.get('url') or self._artwork_default_url(platform, work_data)
        build_note = note_builder or self._artwork_note_text

        for image_path in ordered_media:
            file_path = to_absolute_path(image_path, self.config)
            if not file_path.exists():
                logger.warning(f"{platform} media file not found: {image_path} -> {file_path}")
                continue

            path_str = str(file_path).replace('\\', '/')
            if 'images/' not in path_str:
                logger.info(f"Skipping non-image file: {file_path}")
                continue

            file_hash = await self.import_file(file_path)
            if not file_hash:
                continue

            if work_url:
                await self.associate_url(file_hash, work_url)

            tags = getattr(self, f"_generate_{platform}_tags")(work_data)
            if await self.add_tags(file_hash, tags, platform=platform):
                imported.append((image_path, file_hash))

            if remove_r18_when_not_sensitive and not work_data.get('sensitive'):
                await self.remove_tags(file_hash, ['rating:r-18'], all_services=True)

            note_text = build_note(work_data)
            if note_text:
                await self.add_note(file_hash, note_name, note_text)

        return imported

    async def import_pixiv_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'pixiv', work_data, local_media, note_name='pixiv description'
        )

    def _generate_pixiv_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('pixiv', work_data)

    async def import_kemono_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'kemono', work_data, local_media, note_name='kemono description',
            note_builder=self._kemono_note_text, sort_media=False
        )

    def _generate_kemono_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('kemono', work_data)

    async def import_tinami_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'tinami', work_data, local_media, note_name='tinami description'
        )

    def _generate_tinami_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('tinami', work_data)

    async def import_poipiku_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'poipiku', work_data, local_media, note_name='poipiku description'
        )

    def _generate_poipiku_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('poipiku', work_data)

    async def import_privatter_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'privatter', work_data, local_media, note_name='privatter description'
        )

    def _generate_privatter_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('privatter', work_data)

    async def import_fantia_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'fantia', work_data, local_media, note_name='fantia description'
        )

    def _generate_fantia_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('fantia', work_data)

    async def import_nijie_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'nijie', work_data, local_media, note_name='nijie description'
        )

    def _generate_nijie_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('nijie', work_data)

    async def import_skeb_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'skeb', work_data, local_media, note_name='skeb description'
        )

    def _generate_skeb_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('skeb', work_data)

    async def import_bilibili_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'bilibili', work_data, local_media, note_name='bilibili description'
        )

    def _generate_bilibili_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('bilibili', work_data)

    async def import_misskey_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'misskey', work_data, local_media, note_name='misskey note'
        )

    def _generate_misskey_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('misskey', work_data)

    async def import_gelbooru_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        if not self.enabled or not local_media:
            return []

        imported: List[Tuple[str, str]] = []
        work_url = work_data.get('url') or self._artwork_default_url('gelbooru', work_data)
        source_url = work_data.get('source_url', '')

        for image_path in self._sort_media_paths(local_media):
            file_path = to_absolute_path(image_path, self.config)
            if not file_path.exists():
                logger.warning(f"Gelbooru media file not found: {image_path} -> {file_path}")
                continue

            path_str = str(file_path).replace('\\', '/')
            if 'images/' not in path_str:
                logger.info(f"Skipping non-image file: {file_path}")
                continue

            file_hash = await self.import_file(file_path)
            if not file_hash:
                continue

            if work_url:
                await self.associate_url(file_hash, work_url)
            if source_url:
                await self.associate_url(file_hash, source_url)

            my_tags, danbooru_tags = self._generate_gelbooru_tags_split(work_data)
            my_ok = await self.add_tags(file_hash, my_tags) if my_tags else True
            danbooru_ok = await self.add_tags(file_hash, danbooru_tags, platform='gelbooru') if danbooru_tags else True
            if my_ok or danbooru_ok:
                imported.append((image_path, file_hash))

        return imported

    def _generate_gelbooru_tags_split(self, work_data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        my_tags = self._platform_base_tags('gelbooru')
        danbooru_tags: List[str] = []

        post_id = work_data.get('id')
        if post_id:
            my_tags.append(f"gelbooru_id:{post_id}")

        query = work_data.get('username', '')
        if query:
            my_tags.append(f"gelbooru_query:{query}")

        creator_format = self.tag_settings.get('creator_tag_format', 'creator:{name}')
        artists = work_data.get('tags_artist', [])
        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, str) and artist.strip():
                    clean = artist.strip()
                    my_tags.append(f"gelbooru_artist:{clean}")
                    resolved = self._resolve_csv_creator(clean)
                    if resolved:
                        display_name, platform, username = resolved
                        my_tags.append(creator_format.format(name=display_name))
                        user_tag = self._platform_user_tag(platform, username)
                        if user_tag:
                            my_tags.append(user_tag)
                    else:
                        my_tags.append(creator_format.format(name=clean))
                    danbooru_tags.append(clean)

        if self._is_artwork_sensitive(work_data):
            my_tags.append('rating:r-18')

        my_tags.append(f"rank:{work_data.get('rank', 3)}")

        custom_tags = work_data.get('custom_tags', [])
        if custom_tags:
            my_tags.extend(custom_tags)

        for field_name, prefix in (
            ('tags_character', 'character:'),
            ('tags_copyright', 'series:'),
            ('tags_general', ''),
            ('tags_metadata', ''),
        ):
            values = work_data.get(field_name, [])
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value.strip():
                        danbooru_tags.append(f"{prefix}{value.strip()}")

        return self._dedupe_tags(my_tags), self._dedupe_tags(danbooru_tags)

    async def import_fanbox_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        imported = await self._import_standard_artwork_images(
            'fanbox', work_data, local_media, note_name='fanbox description',
            remove_r18_when_not_sensitive=True
        )
        if len(imported) >= 2:
            await self._set_fanbox_import_times(imported)
        return imported

    async def _set_fanbox_import_times(
        self,
        imported: List[Tuple[str, str]],
    ) -> None:
        import time
        base_ts = time.time()

        file_service_key = await self.get_file_service_key()
        if not file_service_key:
            logger.warning("Failed to get file service key; skipping FANBOX import-time ordering")
            return

        for i, (_, file_hash) in enumerate(imported):
            timestamp = base_ts + i
            success = await self.set_file_import_time(
                file_hash, timestamp, file_service_key
            )
            if not success:
                logger.warning(
                    f"Failed to set FANBOX import time [{i}] {file_hash[:16]}..."
                )

    def _generate_fanbox_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('fanbox', work_data)

    async def import_bluesky_images(self, work_data: Dict[str, Any], local_media: List[str]) -> List[Tuple[str, str]]:
        return await self._import_standard_artwork_images(
            'bluesky', work_data, local_media, note_name='bluesky post'
        )

    def _generate_bluesky_tags(self, work_data: Dict[str, Any]) -> List[str]:
        return self._generate_artwork_tags('bluesky', work_data)

    def _sort_media_paths(self, media_paths: List[str]) -> List[str]:
        """ツイート内の添付順を維持するため、ファイル名の末尾番号でソート"""
        sorted_entries = []
        for original_index, media_path in enumerate(media_paths):
            name = Path(media_path).name
            # _p{num} パターン（Nijie等）を先に試す
            match = re.search(r'_p(\d+)(?=\.[^.]+$)', name)
            if not match:
                # _{num} パターン（Twitter/Pixiv等）にフォールバック
                match = re.search(r'_(\d+)(?=\.[^.]+$)', name)
            order = int(match.group(1)) if match else original_index
            sorted_entries.append((order, original_index, media_path))

        # 末尾番号で昇順、同一番号の場合は元の順番を維持
        sorted_entries.sort()
        return [path for _, __, path in sorted_entries]
    
    def _generate_tags(self, tweet_data: Dict[str, Any]) -> List[str]:
        """ツイートデータからタグを生成"""
        tags = []
        logger.debug(f"Generating tags for tweet {tweet_data.get('id')}: {tweet_data.get('content', '')[:50]}...")
        logger.debug(f"Tweet data keys: {list(tweet_data.keys())}")
        
        # 基本タグ
        tags.extend(self.tag_settings.get('base_tags', []))

        # ツイートIDタグ（数字のみを使用）
        if self.tag_settings.get('include_tweet_id_tag', True):
            raw_tweet_id = tweet_data.get('id')
            if raw_tweet_id is not None:
                tweet_id_str = re.sub(r'\D', '', str(raw_tweet_id))
                if tweet_id_str:
                    tag_format = self.tag_settings.get('tweet_id_tag_format', 'tweet_id:{tweet_id}')
                    try:
                        tags.append(tag_format.format(tweet_id=tweet_id_str))
                    except Exception as e:
                        logger.warning(f"Failed to format tweet_id tag with format '{tag_format}': {e}")

        # クリエイター名タグ（display_nameのみcreator:に入れる）
        creator_format = self.tag_settings.get('creator_tag_format', 'creator:{name}')

        # display_nameでタグ追加
        display_name = tweet_data.get('display_name', '')
        if display_name:
            tags.append(creator_format.format(name=display_name))

        # usernameはtwitter_user:タグとして追加（creator:とは分離）
        username = tweet_data.get('username', '')
        if username:
            tags.append(f"twitter_user:{username}")
        
        # タイトルタグ（ツイート本文）
        include_title = self.tag_settings.get('include_title_tag', True)
        logger.debug(f"include_title_tag setting: {include_title}")
        if include_title:
            # contentまたはtextフィールドを確認
            tweet_text = tweet_data.get('content') or tweet_data.get('text', '')
            logger.debug(f"Tweet text for title tag: {tweet_text[:100] if tweet_text else 'EMPTY'}")
            if tweet_text:
                # t.coリンクを除去（TwitterのURL短縮）
                cleaned_text = re.sub(r'https?://t\.co/\S+', '', tweet_text).strip()
                
                if cleaned_text:
                    # 最初の行のみを取得（改行で分割して最初の要素）
                    first_line = cleaned_text.split('\n')[0].strip()
                    # タブを空白に置換
                    first_line = first_line.replace('\t', ' ')
                    # 連続する空白を1つに圧縮
                    first_line = ' '.join(first_line.split())
                    # 最初の行が長すぎる場合は100文字で切る
                    if len(first_line) > 100:
                        first_line = first_line[:97] + "..."
                    
                    title_tag = f"title:{first_line}"
                    tags.append(title_tag)
                    logger.debug(f"Added title tag: {title_tag}")
                else:
                    logger.warning("Cleaned text is empty after processing")
            else:
                logger.warning(f"No content/text found in tweet data for tweet {tweet_data.get('id')}")
        
        # 日付タグ（config.yamlで無効化されていない場合のみ）
        if self.tag_settings.get('include_date_tag', False):
            date_format = self.tag_settings.get('date_tag_format', 'date:{date}')
            created_at = tweet_data.get('created_at')
            if created_at:
                if isinstance(created_at, str):
                    try:
                        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        date_str = dt.strftime('%Y-%m-%d')
                        tags.append(date_format.format(date=date_str))
                    except:
                        pass
        
        # ツイートURLタグは削除（known URLsとして関連付けるため）
        
        # イベント関連情報（event_infoがある場合）
        event_info = tweet_data.get('event_info', {})
        
        # イベント名タグ
        event_format = self.tag_settings.get('event_tag_format', 'event:{name}')
        detected_events = event_info.get('detected_events', [])
        for event in detected_events:
            if event:
                tags.append(event_format.format(name=event))
        
        # 検出されたキーワード
        if self.tag_settings.get('include_detected_keywords', True):
            keywords = event_info.get('detected_keywords', [])
            for keyword in keywords:
                if keyword:
                    tags.append(f"keyword:{keyword}")

        # センシティブ判定（R-18タグ）
        # sensitive=Noneの場合もありうるので、明示的にチェック
        sensitive = tweet_data.get('sensitive')
        account_sensitive = tweet_data.get('account_sensitive')
        sensitive_flags = tweet_data.get('sensitive_flags')
        if sensitive or account_sensitive or (sensitive_flags and len(sensitive_flags) > 0):
            tags.append('rating:r-18')

        # ランクタグ
        rank = tweet_data.get('rank', 3)
        tags.append(f"rank:{rank}")

        # カスタムタグ（CSV定義のユーザータグ）
        custom_tags = tweet_data.get('custom_tags', [])
        if custom_tags:
            tags.extend(custom_tags)

        # 重複を削除
        unique_tags = list(set(tags))
        # タグ数をログに記録（デバッグ用）
        if unique_tags:
            logger.info(f"Generated {len(unique_tags)} tags for tweet")
            logger.info(f"All tags: {unique_tags}")
            # title:タグが含まれているかチェック
            title_tags = [tag for tag in unique_tags if tag.startswith('title:')]
            if title_tags:
                logger.info(f"Title tag included: {title_tags[0][:100]}...")
            else:
                logger.warning("No title tag generated for this tweet")
        return unique_tags
    
    def _calculate_file_hash(self, file_path: Path) -> str:
        """ファイルのSHA256ハッシュを計算"""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    
    async def _check_file_exists(self, file_hash: str) -> bool:
        """ファイルがHydrusにローカル存在するかチェック

        is_local=True の場合のみ True を返す。
        is_local=False（削除済み or 未インポート）の場合:
          - file_services.deleted にエントリがあれば削除済み → True（再インポート不要）
          - なければ未インポート → False（インポート必要）
        """
        try:
            headers = self._get_headers()
            params = {'hash': file_hash}

            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('metadata'):
                        metadata = data['metadata'][0]
                        if metadata.get('is_local', False):
                            return True
                        # 削除済みかどうかを file_services で判定
                        file_services = metadata.get('file_services', {})
                        if file_services.get('deleted', {}):
                            return True  # ユーザーが削除した → 再インポートしない
                        return False  # 未インポート
                    return False
                else:
                    return False
        except:
            return False
    
    async def _check_file_exists_with_metadata(self, file_hash: str) -> tuple[bool, bool]:
        """ファイルの存在とタグの有無をチェック

        Returns:
            (ファイルが存在するか, EventMonitorのタグがあるか)
            - is_local=True → ローカル存在、タグをチェック
            - is_local=False + file_services.deleted あり → 削除済み、(True, True) でスキップ
            - is_local=False + file_services.deleted なし → 未インポート、(False, False)
        """
        try:
            headers = self._get_headers()
            params = {'hash': file_hash}

            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('metadata'):
                        metadata = data['metadata'][0]

                        if not metadata.get('is_local', False):
                            # 削除済みかどうかを file_services で判定
                            file_services = metadata.get('file_services', {})
                            if file_services.get('deleted', {}):
                                return (True, True)  # ユーザーが削除した → スキップ
                            return (False, False)  # 未インポート

                        # タグの存在をチェック（legacy/new 両方のHydrusメタデータ形式に対応）
                        current_tags = self._extract_display_tags_from_metadata(metadata)
                        eventmonitor_tags = [
                            tag for tag in current_tags
                            if 'eventmonitor' in tag.lower()
                            or tag.startswith('creator:')
                            or tag.startswith('title:')
                        ]
                        has_tags = bool(eventmonitor_tags)

                        return (True, has_tags)
                    return (False, False)
                else:
                    return (False, False)
        except Exception as e:
            logger.error(f"ファイル存在チェックエラー: {e}")
            return (False, False)

    def _extract_display_tags_from_metadata(self, metadata: Dict[str, Any]) -> List[str]:
        """Hydrusのメタデータから設定済みタグサービスのdisplay tagsを抽出"""
        tags: List[str] = []

        # Hydrus v649 以降の metadata["tags"][service_key]["display_tags"]["0"] 形式
        tag_containers = []
        current_tags = metadata.get('tags')
        if isinstance(current_tags, dict):
            tag_containers.append(current_tags)

        # 旧形式の metadata["service_keys_to_statuses_to_display_tags"][service_key]["0"] にも対応
        legacy_tags = metadata.get('service_keys_to_statuses_to_display_tags')
        if isinstance(legacy_tags, dict):
            tag_containers.append(legacy_tags)

        for container in tag_containers:
            for svc_key in self.all_tag_service_keys:
                service_data = container.get(svc_key)
                if not isinstance(service_data, dict):
                    continue

                display_tags = service_data.get('display_tags')
                if isinstance(display_tags, dict):
                    tags.extend(tag for tag in display_tags.get('0', []) if isinstance(tag, str))
                    continue

                # 旧形式: service_data 自体が {"0": [...]} になっている
                tags.extend(tag for tag in service_data.get('0', []) if isinstance(tag, str))

        return list(dict.fromkeys(tags))
    
    async def _undelete_file(self, file_hash: str) -> bool:
        """削除されたファイルを復元"""
        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'
            
            data = {
                'hashes': [file_hash]
            }
            
            async with self.session.post(
                f"{self.api_url}/add_files/undelete_files",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"ファイルの削除を解除しました: {file_hash}")
                    return True
                else:
                    logger.error(f"削除解除APIエラー: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"削除解除エラー: {e}")
            return False
    
    async def _get_file_tags(self, file_hash: str) -> Optional[List[str]]:
        """
        ファイルの既存タグを取得（デバッグ用）
        
        Args:
            file_hash: ファイルのSHA256ハッシュ
            
        Returns:
            タグのリスト、失敗時はNone
        """
        try:
            headers = self._get_headers()
            params = {'hash': file_hash}
            
            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('metadata'):
                        metadata = data['metadata'][0]
                        return self._extract_display_tags_from_metadata(metadata)
                    return []
                else:
                    logger.error(f"タグ取得APIエラー: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"タグ取得エラー: {e}")
            return None
    
    async def search_files_by_url(self, url: str) -> List[str]:
        """
        URLでHydrusのファイルを検索し、ファイルハッシュのリストを返す

        Hydrusの /add_urls/get_url_files API を使用して、
        URLに関連付けられたファイルのハッシュを取得する。

        Args:
            url: 検索するURL（例: "https://x.com/user/status/12345"）

        Returns:
            ファイルハッシュ（SHA256）のリスト。見つからない場合は空リスト。
        """
        if not self.enabled:
            return []

        try:
            headers = self._get_headers()

            # URLドメインの変換（twitter.com ↔ x.com）にも対応
            urls_to_try = [url]
            if 'x.com/' in url:
                urls_to_try.append(url.replace('x.com/', 'twitter.com/'))
            elif 'twitter.com/' in url:
                urls_to_try.append(url.replace('twitter.com/', 'x.com/'))

            all_hashes = []

            for search_url in urls_to_try:
                try:
                    async with self.session.get(
                        f"{self.api_url}/add_urls/get_url_files",
                        headers=headers,
                        params={'url': search_url}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            url_file_statuses = data.get('url_file_statuses', [])

                            for file_status in url_file_statuses:
                                file_hash = file_status.get('hash')
                                status = file_status.get('status')
                                # status 2 = already in db, status 0 = not in db
                                if file_hash and status == 2:
                                    all_hashes.append(file_hash)
                        elif resp.status == 404:
                            # URL not recognised
                            pass
                        else:
                            logger.debug(f"URL検索APIエラー: {resp.status} for URL: {search_url}")
                except Exception as e:
                    logger.debug(f"URL検索リクエストエラー: {e} for URL: {search_url}")

            # 重複排除（順序保持）
            return list(dict.fromkeys(all_hashes))

        except Exception as e:
            logger.error(f"URL検索エラー: {e}")
            return []

    async def _get_hashes_from_file_ids(self, file_ids: List[int]) -> List[str]:
        """
        ファイルIDリストからSHA256ハッシュリストを取得

        Args:
            file_ids: Hydrusの内部ファイルIDリスト

        Returns:
            SHA256ハッシュのリスト
        """
        if not file_ids:
            return []

        try:
            headers = self._get_headers()
            params = {
                'file_ids': json.dumps(file_ids),
                'only_return_basic_information': json.dumps(True),
            }

            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    hashes = []
                    for metadata in data.get('metadata', []):
                        file_hash = metadata.get('hash')
                        if file_hash:
                            hashes.append(file_hash)
                    return hashes
                else:
                    logger.error(f"ファイルメタデータ取得APIエラー: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"ファイルメタデータ取得エラー: {e}")
            return []

    async def add_note(self, file_hash: str, note_name: str, note_text: str) -> bool:
        """
        ファイルにnoteを追加
        
        Args:
            file_hash: ファイルのSHA256ハッシュ
            note_name: noteの名前
            note_text: noteの内容
            
        Returns:
            成功時True、失敗時False
        """
        if not self.enabled or not note_text:
            return False
            
        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'
            
            data = {
                'hash': file_hash,
                'notes': {
                    note_name: note_text
                }
            }
            
            async with self.session.post(
                f"{self.api_url}/add_notes/set_notes",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"noteを追加しました: {note_name}")
                    return True
                else:
                    logger.error(f"note追加APIエラー: {resp.status}")
                    error_text = await resp.text()
                    logger.error(f"エラー詳細: {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"note追加エラー: {e}")
            return False
    
    async def associate_url(self, file_hash: str, url: str) -> bool:
        """
        URLをファイルのknown URLとして関連付け
        
        Args:
            file_hash: ファイルのSHA256ハッシュ
            url: 関連付けるURL
            
        Returns:
            成功時True、失敗時False
        """
        if not self.enabled:
            return False
            
        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'
            
            data = {
                'hash': file_hash,
                'url_to_add': url
            }
            
            async with self.session.post(
                f"{self.api_url}/add_urls/associate_url",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.info(f"URLを関連付けました: {url}")
                    return True
                else:
                    logger.error(f"URL関連付けAPIエラー: {resp.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"URL関連付けエラー: {e}")
            return False

    async def get_file_service_key(self) -> Optional[str]:
        """'my files' (local file domain, type=2) のサービスキーを取得・キャッシュ"""
        if hasattr(self, '_file_service_key') and self._file_service_key:
            return self._file_service_key

        try:
            headers = self._get_headers()
            async with self.session.get(
                f"{self.api_url}/get_services", headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.error(f"get_services APIエラー: {resp.status}")
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error(f"get_services 接続エラー: {e}")
            return None

        # local_files カテゴリから type=2 のサービスを探す
        for svc in data.get('local_files', []):
            if isinstance(svc, dict) and svc.get('type') == 2:
                self._file_service_key = svc['service_key']
                logger.info(f"ファイルサービスキー解決: '{svc.get('name')}' (key: {self._file_service_key})")
                return self._file_service_key

        # フォールバック: 全カテゴリからtype=2を探す
        for category, services in data.items():
            if not isinstance(services, list):
                continue
            for svc in services:
                if isinstance(svc, dict) and svc.get('type') == 2:
                    self._file_service_key = svc['service_key']
                    logger.info(f"ファイルサービスキー解決（フォールバック）: '{svc.get('name')}' (key: {self._file_service_key})")
                    return self._file_service_key

        logger.error("ファイルサービスキーが見つかりません")
        return None

    async def set_file_import_time(
        self, file_hash: str, timestamp: float, file_service_key: str
    ) -> bool:
        """
        ファイルのインポート時刻を設定

        Args:
            file_hash: ファイルのSHA256ハッシュ
            timestamp: Unixタイムスタンプ（float）
            file_service_key: ファイルサービスキー（get_file_service_keyで取得）

        Returns:
            成功時True、失敗時False
        """
        if not self.enabled:
            return False

        try:
            headers = self._get_headers()
            headers['Content-Type'] = 'application/json'

            data = {
                'hash': file_hash,
                'timestamp': timestamp,
                'timestamp_type': 3,  # 3 = file import time
                'file_service_key': file_service_key,
            }

            async with self.session.post(
                f"{self.api_url}/edit_times/set_time",
                headers=headers,
                json=data
            ) as resp:
                if resp.status == 200:
                    logger.debug(f"インポート時刻を設定: {file_hash[:16]}... -> {timestamp}")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"set_time APIエラー: {resp.status} - {error_text}")
                    return False

        except Exception as e:
            logger.error(f"set_time エラー: {e}")
            return False
