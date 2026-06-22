#!/usr/bin/env python3
"""
既存フォルダ管理画像をHydrus Clientに一括インポートするスクリプト

対象フォルダ:
  1. D:\f\a\07_niconico\お気に入り絵\  (作者名フォルダのみ、特殊フォルダはスキップ)
  2. F:\AI\98_model\学習素材用\05_FANBOX\ヒエログリフ\  (作者→記事ID/日付-タイトル)
  3. F:\AI\98_model\学習素材用\05_FANBOX\R-18\  (作者→記事ID/日付-タイトル)
  4. F:\AI\98_model\学習素材用\03_download\Hitomi-Downloader_pixiv\  (Hitomi-DL Pixiv)
  5. F:\AI\98_model\学習素材用\03_download\Hitomi-Downloader_twitter\  (Hitomi-DL Twitter)
  6. F:\AI\98_model\学習素材用\03_download\Hitomi-Downloader_danbooru\  (Hitomi-DL Danbooru)
  7. F:\AI\98_model\学習素材用\03_download\Hitomi-Downloader_Skeb\  (Hitomi-DL Skeb)
  8. F:\AI\98_model\学習素材用\03_download\Hitomi-Downloader_pinter\  (Hitomi-DL Pinterest)
  9. F:\AI\98_model\学習素材用\03_download\未整理\  (タグ付きdanbooruフォルダ)
  10. F:\AI\98_model\学習素材用\03_download\未整理\PxDownlaod\  (PxDownload Pixiv)

使い方:
  # Dry-run (タグ確認のみ、インポートしない)
  python scripts/hydrus/folder_import.py

  # 実行
  python scripts/hydrus/folder_import.py --execute

  # 特定フォルダのみ
  python scripts/hydrus/folder_import.py --execute --source niconico
  python scripts/hydrus/folder_import.py --execute --source hitomi_twitter

  # 特定ユーザーのみ（部分一致）
  python scripts/hydrus/folder_import.py --source hitomi_twitter --user "@suisounobeta"
  python scripts/hydrus/folder_import.py --source hitomi_pixiv --user "Ixy"
  python scripts/hydrus/folder_import.py --source miseirii_tagged --user "yuzuki_yukari"

  # 件数制限
  python scripts/hydrus/folder_import.py --execute --limit 10
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

logger = logging.getLogger("FolderImport")


def _natural_sort_key(path: Path):
    """自然順ソート用キー（'10.jpeg' が '2.jpeg' より後に来る）"""
    stem = path.stem
    parts = re.split(r'(\d+)', stem)
    return [int(p) if p.isdigit() else p.lower() for p in parts]

# ========== 定数 ==========

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.jfif'}

DOWNLOAD_BASE = Path(r"F:\AI\98_model\学習素材用\03_download")

# インポート対象フォルダ
SOURCES = {
    'niconico': {
        'path': Path(r"D:\f\a\07_niconico\お気に入り絵"),
        'description': 'ニコニコお気に入り絵',
    },
    'fanbox_hieroglyph': {
        'path': Path(r"F:\AI\98_model\学習素材用\05_FANBOX\ヒエログリフ"),
        'description': 'FANBOX ヒエログリフ',
    },
    'fanbox_r18': {
        'path': Path(r"F:\AI\98_model\学習素材用\05_FANBOX\R-18"),
        'description': 'FANBOX R-18',
    },
    'hitomi_pixiv': {
        'path': DOWNLOAD_BASE / 'Hitomi-Downloader_pixiv',
        'description': 'Hitomi-DL Pixiv',
    },
    'hitomi_twitter': {
        'path': DOWNLOAD_BASE / 'Hitomi-Downloader_twitter',
        'description': 'Hitomi-DL Twitter',
    },
    'hitomi_danbooru': {
        'path': DOWNLOAD_BASE / 'Hitomi-Downloader_danbooru',
        'description': 'Hitomi-DL Danbooru',
    },
    'hitomi_skeb': {
        'path': DOWNLOAD_BASE / 'Hitomi-Downloader_Skeb',
        'description': 'Hitomi-DL Skeb',
    },
    'hitomi_pinter': {
        'path': DOWNLOAD_BASE / 'Hitomi-Downloader_pinter',
        'description': 'Hitomi-DL Pinterest',
    },
    'miseirii_tagged': {
        'path': DOWNLOAD_BASE / '未整理',
        'description': '未整理 (タグ付き)',
    },
    'miseirii_pxdownload': {
        'path': DOWNLOAD_BASE / '未整理' / 'PxDownlaod',
        'description': '未整理 PxDownload',
    },
}

# お気に入り絵のサブフォルダで、作者名ではなく特殊用途フォルダ（スキップ対象）
SKIP_FOLDERS = {'00_ALL', '01_格納待ち', '02_other', '10_Cool', '11_全能', '20_any', '動画'}

# 未整理フォルダ内でmiseirii_taggedのスキップ対象（別ソースとして処理）
MISEIRII_SKIP_FOLDERS = {'PxDownlaod'}


# ========== ユーザーフィルタ ==========

def _match_user_filter(folder_name: str, user_filters: Optional[List[str]]) -> bool:
    """フォルダ名がユーザーフィルタに部分一致するか判定"""
    if not user_filters:
        return True
    folder_lower = folder_name.lower()
    return any(f.lower() in folder_lower for f in user_filters)


# ========== ファイル収集・タグ生成 ==========

class FileEntry:
    """インポート対象ファイルとタグ情報"""
    def __init__(self, path: Path, tags: List[str], source: str):
        self.path = path
        self.tags = tags
        self.source = source

    def __repr__(self):
        return f"FileEntry({self.path.name}, tags={self.tags})"


def is_image_file(path: Path) -> bool:
    """画像ファイルかどうか判定"""
    return path.suffix.lower() in IMAGE_EXTENSIONS


def collect_niconico_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    お気に入り絵フォルダからファイルを収集

    構造:
      お気に入り絵/
        {作者名}/          → creator:{作者名}
          {pixiv_id}.jpg
        00_ALL, 01_格納待ち 等 → スキップ
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:niconico_fav']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name

        if folder_name in SKIP_FOLDERS:
            logger.info(f"  スキップ: {folder_name}")
            continue

        if not _match_user_filter(folder_name, user_filters):
            continue

        # 作者名フォルダ
        creator_tag = f'creator:{folder_name}'
        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags)
            tags.append(creator_tag)
            entries.append(FileEntry(f, tags, 'niconico'))

    return entries


def _collect_fanbox_files(base_path: Path, extra_tags: List[str], source_name: str,
                          user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    FANBOXフォルダからファイルを収集（ヒエログリフ/R-18 共通）

    記事フォルダ名は以下の2形式を自動判別:
      - 日付形式: "2024-07-12-MAI"     → title:MAI
      - ID形式:   "3927480-5月でした"  → fanbox_id:3927480, title:5月でした

    構造:
      {base}/
        {作者名}/
          {記事ID or 日付}-{タイトル}/
            0.jpg, 1.png, ...
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:fanbox'] + extra_tags

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for creator_folder in sorted(base_path.iterdir()):
        if not creator_folder.is_dir():
            continue

        creator_name = creator_folder.name

        if not _match_user_filter(creator_name, user_filters):
            continue

        creator_tag = f'creator:{creator_name}'

        for article_folder in sorted(creator_folder.iterdir()):
            if not article_folder.is_dir():
                continue

            article_name = article_folder.name
            article_id, article_title = _parse_fanbox_article_folder(article_name)

            for f in _collect_images_recursive(article_folder):
                tags = list(base_tags)
                tags.append(creator_tag)
                if article_id:
                    tags.append(f'fanbox_id:{article_id}')
                if article_title:
                    tags.append(f'title:{article_title}')
                entries.append(FileEntry(f, tags, source_name))

    return entries


def collect_fanbox_hieroglyph_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """FANBOXヒエログリフフォルダからファイルを収集"""
    return _collect_fanbox_files(base_path, [], 'fanbox_hieroglyph', user_filters)


def collect_fanbox_r18_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """FANBOX R-18フォルダからファイルを収集"""
    return _collect_fanbox_files(base_path, ['rating:r-18'], 'fanbox_r18', user_filters)


def _collect_images_recursive(folder: Path) -> List[Path]:
    """フォルダ内の画像を再帰的に収集"""
    images = []
    for item in sorted(folder.iterdir(), key=_natural_sort_key):
        if item.is_file() and is_image_file(item):
            images.append(item)
        elif item.is_dir():
            images.extend(_collect_images_recursive(item))
    return images


def _parse_fanbox_article_folder(name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    FANBOX記事フォルダ名をパース（日付形式・ID形式の両方に対応）

    日付形式を先に判定し、マッチしなければID形式として処理する。

    日付形式:
      "2024-07-12-MAI"                    → (None, "MAI")
      "2023-07-28-兵舎 ( Full Edition )"    → (None, "兵舎 ( Full Edition )")

    ID形式:
      "3927480-5月でした"                  → ("3927480", "5月でした")
      "4300069-akn R"                     → ("4300069", "akn R")

    Returns:
        (fanbox_id or None, title or None)
    """
    # まず日付形式を試す: YYYY-MM-DD-タイトル
    date_match = re.match(r'^\d{4}-\d{2}-\d{2}-(.+)$', name)
    if date_match:
        return None, date_match.group(1)

    # 次にID形式を試す: 数字-タイトル
    id_match = re.match(r'^(\d+)-(.+)$', name)
    if id_match:
        return id_match.group(1), id_match.group(2)

    # どちらでもない場合はフォルダ名全体をタイトルとして扱う
    return None, name


# ========== 03_download コレクター ==========

def collect_hitomi_pixiv_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    Hitomi-Downloader Pixivフォルダからファイルを収集

    構造:
      Hitomi-Downloader_pixiv/
        {作者名} (pixiv_{user_id})/
          {illust_id}_p{page}.jpg
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:pixiv']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    pixiv_folder_re = re.compile(r'^(.+?)\s*\(pixiv_(\d+)\)$')
    pixiv_file_re = re.compile(r'^(\d+)_p\d+')

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if not _match_user_filter(folder_name, user_filters):
            continue

        m = pixiv_folder_re.match(folder_name)
        if m:
            creator_name = m.group(1).strip()
            pixiv_user_id = m.group(2)
            folder_tags = [f'creator:{creator_name}', f'pixiv_user_id:{pixiv_user_id}']
        else:
            folder_tags = [f'creator:{folder_name}']

        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags) + list(folder_tags)
            fm = pixiv_file_re.match(f.stem)
            if fm:
                tags.append(f'pixiv_id:{fm.group(1)}')
            entries.append(FileEntry(f, tags, 'hitomi_pixiv'))

    return entries


def collect_hitomi_twitter_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    Hitomi-Downloader Twitterフォルダからファイルを収集

    構造:
      Hitomi-Downloader_twitter/
        {表示名} (@{screen_name})/
          [YY-MM-DD] {tweet_id}_p{page}.ext
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:twitter']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    twitter_folder_re = re.compile(r'^(.+?)\s*\(@([^)]+)\)$')
    twitter_file_re = re.compile(r'^\[\d{2}-\d{2}-\d{2}\]\s*(\d+)_p\d+')

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if not _match_user_filter(folder_name, user_filters):
            continue

        m = twitter_folder_re.match(folder_name)
        if m:
            screen_name = m.group(2)
            folder_tags = [f'creator:{screen_name}']
        else:
            folder_tags = [f'creator:{folder_name}']

        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags) + list(folder_tags)
            fm = twitter_file_re.match(f.stem)
            if fm:
                tags.append(f'tweet_id:{fm.group(1)}')
            entries.append(FileEntry(f, tags, 'hitomi_twitter'))

    return entries


def collect_hitomi_danbooru_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    Hitomi-Downloader Danbooruフォルダからファイルを収集

    構造:
      Hitomi-Downloader_danbooru/
        {artist_tag}/
          {post_id}.ext
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:danbooru']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if not _match_user_filter(folder_name, user_filters):
            continue

        artist_tag = f'danbooru_artist:{folder_name}'

        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags)
            tags.append(f'creator:{folder_name}')
            tags.append(artist_tag)
            if f.stem.isdigit():
                tags.append(f'danbooru_id:{f.stem}')
            entries.append(FileEntry(f, tags, 'hitomi_danbooru'))

    return entries


def collect_hitomi_skeb_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    Hitomi-Downloader Skebフォルダからファイルを収集

    構造:
      Hitomi-Downloader_Skeb/
        {artist}/
          {uuid}.webp
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:skeb']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if not _match_user_filter(folder_name, user_filters):
            continue

        creator_tag = f'creator:{folder_name}'

        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags)
            tags.append(creator_tag)
            entries.append(FileEntry(f, tags, 'hitomi_skeb'))

    return entries


def collect_hitomi_pinter_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    Hitomi-Downloader Pinterestフォルダからファイルを収集

    構造:
      Hitomi-Downloader_pinter/
        {pin_id}.ext  (フラット、サブフォルダなし)
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:pinterest']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for f in sorted(base_path.iterdir(), key=_natural_sort_key):
        if f.is_file() and is_image_file(f):
            entries.append(FileEntry(f, list(base_tags), 'hitomi_pinter'))

    return entries


def collect_miseirii_tagged_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    未整理フォルダからタグ付きファイルを収集（PxDownlaodを除く）

    構造:
      未整理/
        {キャラ名 or 作者名}/
          {post_id}.ext       → 画像
          {post_id}.ext.txt   → カンマ区切りdanbooruタグ
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:danbooru']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if folder_name in MISEIRII_SKIP_FOLDERS:
            continue

        if not _match_user_filter(folder_name, user_filters):
            continue

        query_tag = f'search_query:{folder_name}'

        for f in sorted(subfolder.iterdir(), key=_natural_sort_key):
            if not f.is_file() or not is_image_file(f):
                continue

            tags = list(base_tags)
            tags.append(query_tag)

            # .txt タグファイルを読み込み
            txt_path = f.parent / (f.name + '.txt')
            if txt_path.exists():
                try:
                    txt_content = txt_path.read_text(encoding='utf-8').strip()
                    if txt_content:
                        danbooru_tags = [t.strip() for t in txt_content.split(',') if t.strip()]
                        tags.extend(danbooru_tags)
                except Exception as e:
                    logger.warning(f"タグファイル読み込みエラー: {txt_path} - {e}")

            if f.stem.isdigit():
                tags.append(f'danbooru_id:{f.stem}')

            entries.append(FileEntry(f, tags, 'miseirii_tagged'))

    return entries


def collect_miseirii_pxdownload_files(base_path: Path, user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """
    未整理/PxDownlaodフォルダからファイルを収集

    構造:
      PxDownlaod/
        {作者名} - pixiv/
          {illust_id}_p{page}.ext
        #タグ名... - pixiv/          → タグ検索結果フォルダ
          {illust_id}_p{page}.ext
        *.zip                         → スキップ
    """
    entries = []
    base_tags = ['imported_by:folder_import', 'source:pixiv']

    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return entries

    pxdl_folder_re = re.compile(r'^(.+?)\s*-\s*pixiv$')
    pixiv_file_re = re.compile(r'^(\d+)_p\d+')

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir():
            continue

        folder_name = subfolder.name
        if not _match_user_filter(folder_name, user_filters):
            continue

        m = pxdl_folder_re.match(folder_name)
        if m:
            raw_name = m.group(1).strip()
            if raw_name.startswith('#'):
                # タグ検索結果フォルダ（#東北きりたん... 等）
                folder_tags = [f'search_query:{raw_name}']
            else:
                folder_tags = [f'creator:{raw_name}']
        else:
            folder_tags = [f'creator:{folder_name}']

        for f in _collect_images_recursive(subfolder):
            tags = list(base_tags) + list(folder_tags)
            fm = pixiv_file_re.match(f.stem)
            if fm:
                tags.append(f'pixiv_id:{fm.group(1)}')
            entries.append(FileEntry(f, tags, 'miseirii_pxdownload'))

    return entries


# ========== Hydrus API 連携 ==========

class HydrusImporter:
    """Hydrus Client APIを使ったファイルインポーター"""

    def __init__(self, api_url: str, access_key: str):
        self.api_url = api_url.rstrip('/')
        self.access_key = access_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.stats = {
            'imported': 0,
            'already_exists': 0,
            'undeleted': 0,
            'tagged': 0,
            'errors': 0,
            'skipped': 0,
        }

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        # API接続テスト
        try:
            headers = {'Hydrus-Client-API-Access-Key': self.access_key}
            async with self.session.get(
                f"{self.api_url}/verify_access_key", headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.info(f"Hydrus API接続成功: {result.get('human_description', 'OK')}")
                else:
                    raise ConnectionError(f"Hydrus API認証失敗: status={resp.status}")
        except aiohttp.ClientError as e:
            raise ConnectionError(f"Hydrus APIに接続できません: {e}")
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _headers(self, content_type: str = 'application/json') -> Dict[str, str]:
        return {
            'Hydrus-Client-API-Access-Key': self.access_key,
            'Content-Type': content_type,
        }

    async def import_and_tag(self, entry: FileEntry) -> bool:
        """ファイルをインポートしてタグを付与"""
        try:
            file_hash = await self._import_file(entry.path)
            if file_hash is None:
                self.stats['errors'] += 1
                return False

            if entry.tags:
                success = await self._add_tags(file_hash, entry.tags)
                if success:
                    self.stats['tagged'] += 1
                else:
                    logger.warning(f"タグ付与失敗: {entry.path.name}")
            return True
        except Exception as e:
            logger.error(f"インポートエラー: {entry.path} - {e}")
            self.stats['errors'] += 1
            return False

    async def _import_file(self, file_path: Path) -> Optional[str]:
        """ファイルをHydrusにインポート"""
        headers = {
            'Hydrus-Client-API-Access-Key': self.access_key,
            'Content-Type': 'application/octet-stream',
        }
        try:
            with open(file_path, 'rb') as f:
                async with self.session.post(
                    f"{self.api_url}/add_files/add_file",
                    headers=headers,
                    data=f,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        status = result.get('status')
                        file_hash = result.get('hash')

                        if status == 1:  # 新規インポート成功
                            self.stats['imported'] += 1
                            return file_hash
                        elif status == 2:  # 既にDB内
                            self.stats['already_exists'] += 1
                            return file_hash
                        elif status == 3:  # 以前削除済み
                            logger.info(f"削除済みファイルを復元: {file_path.name}")
                            if await self._undelete_file(file_hash):
                                self.stats['undeleted'] += 1
                                return file_hash
                            else:
                                return None
                        else:
                            logger.error(f"不明なインポートステータス ({status}): {file_path.name}")
                            return None
                    else:
                        logger.error(f"インポートAPIエラー ({resp.status}): {file_path.name}")
                        return None
        except Exception as e:
            logger.error(f"ファイル読み込みエラー: {file_path} - {e}")
            return None

    async def _undelete_file(self, file_hash: str) -> bool:
        """削除されたファイルを復元"""
        try:
            async with self.session.post(
                f"{self.api_url}/add_files/undelete_files",
                headers=self._headers(),
                json={'hashes': [file_hash]},
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"削除解除エラー: {e}")
            return False

    async def _add_tags(self, file_hash: str, tags: List[str]) -> bool:
        """ファイルにタグを付与（local tagsサービスに送信）"""
        data = {
            'hashes': [file_hash],
            'service_keys_to_actions_to_tags': {
                '6c6f63616c2074616773': {  # "local tags"
                    '0': tags,
                }
            },
            'override_previously_deleted_mappings': True,
        }
        try:
            async with self.session.post(
                f"{self.api_url}/add_tags/add_tags",
                headers=self._headers(),
                json=data,
            ) as resp:
                if resp.status == 200:
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"タグ追加APIエラー ({resp.status}): {error[:200]}")
                    return False
        except Exception as e:
            logger.error(f"タグ追加エラー: {e}")
            return False


# ========== メインロジック ==========

def collect_all_entries(sources: List[str], user_filters: Optional[List[str]] = None) -> List[FileEntry]:
    """指定ソースからファイルエントリを収集"""
    entries = []

    collectors = {
        'niconico': lambda: collect_niconico_files(SOURCES['niconico']['path'], user_filters),
        'fanbox_hieroglyph': lambda: collect_fanbox_hieroglyph_files(SOURCES['fanbox_hieroglyph']['path'], user_filters),
        'fanbox_r18': lambda: collect_fanbox_r18_files(SOURCES['fanbox_r18']['path'], user_filters),
        'hitomi_pixiv': lambda: collect_hitomi_pixiv_files(SOURCES['hitomi_pixiv']['path'], user_filters),
        'hitomi_twitter': lambda: collect_hitomi_twitter_files(SOURCES['hitomi_twitter']['path'], user_filters),
        'hitomi_danbooru': lambda: collect_hitomi_danbooru_files(SOURCES['hitomi_danbooru']['path'], user_filters),
        'hitomi_skeb': lambda: collect_hitomi_skeb_files(SOURCES['hitomi_skeb']['path'], user_filters),
        'hitomi_pinter': lambda: collect_hitomi_pinter_files(SOURCES['hitomi_pinter']['path'], user_filters),
        'miseirii_tagged': lambda: collect_miseirii_tagged_files(SOURCES['miseirii_tagged']['path'], user_filters),
        'miseirii_pxdownload': lambda: collect_miseirii_pxdownload_files(SOURCES['miseirii_pxdownload']['path'], user_filters),
    }

    for source in sources:
        if source not in collectors:
            logger.warning(f"不明なソース: {source}")
            continue

        desc = SOURCES[source]['description']
        path = SOURCES[source]['path']
        logger.info(f"📁 {desc} をスキャン中... ({path})")

        source_entries = collectors[source]()
        logger.info(f"   → {len(source_entries)} ファイル検出")
        entries.extend(source_entries)

    return entries


def print_dry_run_report(entries: List[FileEntry]):
    """Dry-runレポートを表示"""
    print("\n" + "=" * 70)
    print("📋 Dry-Run レポート")
    print("=" * 70)

    # ソース別集計
    source_counts: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}

    for entry in entries:
        source_counts[entry.source] = source_counts.get(entry.source, 0) + 1
        for tag in entry.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    print(f"\n📊 合計: {len(entries)} ファイル")
    print("\nソース別:")
    for source, count in sorted(source_counts.items()):
        desc = SOURCES.get(source, {}).get('description', source)
        print(f"  {desc}: {count} ファイル")

    # タグ統計（上位20件）
    print("\nタグ統計 (上位20件):")
    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    for tag, count in sorted_tags[:20]:
        print(f"  {tag}: {count}")

    # サンプル表示（各ソースから最大3件）
    print("\n📝 サンプルエントリ:")
    shown_sources = set()
    sample_count = 0

    for entry in entries:
        if entry.source not in shown_sources or sample_count < 15:
            print(f"\n  ファイル: {entry.path}")
            print(f"  タグ: {entry.tags}")
            shown_sources.add(entry.source)
            sample_count += 1
            if sample_count >= 15:
                break

    print("\n" + "=" * 70)
    print("💡 実際にインポートするには --execute フラグを追加してください")
    print("=" * 70)


async def execute_import(entries: List[FileEntry], limit: Optional[int] = None):
    """実際のインポートを実行"""
    # config.yamlからHydrus設定を読み込み
    config_path = PROJECT_ROOT / 'config.yaml'
    if not config_path.exists():
        logger.error(f"config.yaml が見つかりません: {config_path}")
        return

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    hydrus_config = config.get('hydrus', {})
    api_url = hydrus_config.get('api_url', 'http://127.0.0.1:45869')
    access_key = os.environ.get('HYDRUS_ACCESS_KEY') or hydrus_config.get('access_key')

    if not access_key:
        logger.error("HYDRUS_ACCESS_KEY が設定されていません (.env または config.yaml)")
        return

    if limit:
        entries = entries[:limit]

    total = len(entries)
    print(f"\n🚀 インポート開始: {total} ファイル")

    async with HydrusImporter(api_url, access_key) as importer:
        start_time = time.time()

        for i, entry in enumerate(entries, 1):
            # 進捗表示
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0

            print(
                f"\r  [{i}/{total}] "
                f"({i * 100 // total}%) "
                f"ETA: {eta:.0f}s "
                f"| ✅{importer.stats['imported']} "
                f"⏭️{importer.stats['already_exists']} "
                f"🔄{importer.stats['undeleted']} "
                f"❌{importer.stats['errors']} "
                f"| {entry.path.name[:40]}",
                end='', flush=True
            )

            await importer.import_and_tag(entry)

            # API負荷軽減のための小休止
            if i % 10 == 0:
                await asyncio.sleep(0.1)

        print()  # 改行

        # 結果サマリー
        elapsed = time.time() - start_time
        print("\n" + "=" * 70)
        print("📊 インポート結果")
        print("=" * 70)
        print(f"  処理時間: {elapsed:.1f}秒")
        print(f"  新規インポート: {importer.stats['imported']}")
        print(f"  既存スキップ:   {importer.stats['already_exists']}")
        print(f"  削除復元:       {importer.stats['undeleted']}")
        print(f"  タグ付与:       {importer.stats['tagged']}")
        print(f"  エラー:         {importer.stats['errors']}")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description='既存フォルダ管理画像をHydrus Clientにインポート'
    )
    parser.add_argument(
        '--execute', action='store_true',
        help='実際にインポートを実行する（デフォルトはdry-run）'
    )
    parser.add_argument(
        '--source', choices=list(SOURCES.keys()), nargs='+',
        default=list(SOURCES.keys()),
        help='インポート対象のソース（デフォルト: 全て）'
    )
    parser.add_argument(
        '--user', nargs='+', default=None,
        help='特定のユーザーフォルダのみ処理（部分一致、複数指定可）'
    )
    parser.add_argument(
        '--limit', type=int, default=None,
        help='インポートするファイル数の上限'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='詳細ログを表示'
    )

    args = parser.parse_args()

    # --user は --source と併用必須
    if args.user and args.source == list(SOURCES.keys()):
        parser.error("--user を使う場合は --source も指定してください")

    # ログ設定
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # .envファイルを読み込み
    env_path = PROJECT_ROOT / '.env'
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value

    # ファイル収集
    if args.user:
        print(f"\n📂 フォルダをスキャン中... (ユーザーフィルタ: {args.user})")
    else:
        print("\n📂 フォルダをスキャン中...")
    entries = collect_all_entries(args.source, args.user)

    if not entries:
        print("⚠️ インポート対象のファイルが見つかりませんでした")
        return

    if args.execute:
        # 実行モード
        if args.limit:
            print(f"⚠️ 制限モード: 先頭 {args.limit} ファイルのみ処理")
        asyncio.run(execute_import(entries, args.limit))
    else:
        # Dry-runモード
        print_dry_run_report(entries)


if __name__ == '__main__':
    main()
