#!/usr/bin/env python3
"""
フォルダインポート時に rating:r-18 タグが付与されなかったバグの修正スクリプト

原因:
  hydrus_folder_import.py の collect_fanbox_r18_files() が extra_tags=['r-18'] としており、
  他プラットフォーム標準の 'rating:r-18' が欠落していた。

修正方法:
  1. FANBOX R-18フォルダを直接スキャン
  2. 各ファイルのSHA256でHydrus上のファイルを特定
  3. 'rating:r-18' タグを付与

使い方:
  # Dry-run（対象ファイル数の確認のみ）
  python scripts/fix/r18_tags.py

  # 実行
  python scripts/fix/r18_tags.py --execute

  # 件数制限
  python scripts/fix/r18_tags.py --execute --limit 10
"""

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

logger = logging.getLogger("FixR18Tags")

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.jfif'}

R18_FOLDER = Path(r"F:\AI\98_model\学習素材用\05_FANBOX\R-18")

TAG_TO_ADD = 'rating:r-18'


def _natural_sort_key(path: Path):
    stem = path.stem
    parts = re.split(r'(\d+)', stem)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def _collect_images_recursive(folder: Path) -> List[Path]:
    """フォルダ内の画像を再帰的に自然順で収集"""
    images = []
    for item in sorted(folder.iterdir(), key=_natural_sort_key):
        if item.is_file() and is_image_file(item):
            images.append(item)
        elif item.is_dir():
            images.extend(_collect_images_recursive(item))
    return images


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def collect_r18_files(base_path: Path) -> List[Path]:
    """R-18フォルダから全画像ファイルを収集"""
    files = []
    if not base_path.exists():
        logger.error(f"フォルダが見つかりません: {base_path}")
        return files

    for creator_folder in sorted(base_path.iterdir()):
        if not creator_folder.is_dir():
            continue
        for article_folder in sorted(creator_folder.iterdir()):
            if not article_folder.is_dir():
                continue
            for f in _collect_images_recursive(article_folder):
                files.append(f)

    return files


class HydrusTagFixer:
    """Hydrusファイルにタグを一括付与"""

    def __init__(self, api_url: str, access_key: str):
        self.api_url = api_url.rstrip('/')
        self.access_key = access_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.stats = {
            'tagged': 0,
            'not_found': 0,
            'errors': 0,
        }

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        headers = {'Hydrus-Client-API-Access-Key': self.access_key}
        async with self.session.get(
            f"{self.api_url}/verify_access_key", headers=headers
        ) as resp:
            if resp.status != 200:
                raise ConnectionError(f"Hydrus API認証失敗: status={resp.status}")
            logger.info("Hydrus API接続成功")
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _headers(self) -> Dict[str, str]:
        return {
            'Hydrus-Client-API-Access-Key': self.access_key,
            'Content-Type': 'application/json',
        }

    async def file_exists(self, file_hash: str) -> bool:
        """ファイルがHydrusに存在するか確認"""
        import json as json_mod
        headers = {'Hydrus-Client-API-Access-Key': self.access_key}
        params = {'hashes': json_mod.dumps([file_hash])}
        try:
            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    metadata_list = data.get('metadata', [])
                    if metadata_list:
                        # is_trashed や is_deleted でないことを確認
                        meta = metadata_list[0]
                        return not meta.get('is_trashed', False)
                return False
        except Exception as e:
            logger.error(f"メタデータ取得エラー: {e}")
            return False

    async def add_tag(self, file_hash: str, tag: str) -> bool:
        """ファイルにタグを付与"""
        data = {
            'hashes': [file_hash],
            'service_keys_to_actions_to_tags': {
                '6c6f63616c2074616773': {  # "local tags"
                    '0': [tag],
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

    async def fix_file(self, file_path: Path) -> bool:
        """1ファイルのタグを修正"""
        file_hash = compute_sha256(file_path)

        exists = await self.file_exists(file_hash)
        if not exists:
            self.stats['not_found'] += 1
            logger.debug(f"Hydrusに未登録: {file_path.name}")
            return False

        success = await self.add_tag(file_hash, TAG_TO_ADD)
        if success:
            self.stats['tagged'] += 1
            return True
        else:
            self.stats['errors'] += 1
            return False


async def execute_fix(files: List[Path], limit: Optional[int] = None):
    """タグ修正を実行"""
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
        files = files[:limit]

    total = len(files)
    print(f"\n🔧 タグ修正開始: {total} ファイルに '{TAG_TO_ADD}' を付与")

    async with HydrusTagFixer(api_url, access_key) as fixer:
        start_time = time.time()

        for i, file_path in enumerate(files, 1):
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0

            print(
                f"\r  [{i}/{total}] "
                f"({i * 100 // total}%) "
                f"ETA: {eta:.0f}s "
                f"| ✅{fixer.stats['tagged']} "
                f"❓{fixer.stats['not_found']} "
                f"❌{fixer.stats['errors']} "
                f"| {file_path.name[:40]}",
                end='', flush=True
            )

            await fixer.fix_file(file_path)

            if i % 10 == 0:
                await asyncio.sleep(0.1)

        print()
        elapsed = time.time() - start_time

        print("\n" + "=" * 70)
        print("📊 タグ修正結果")
        print("=" * 70)
        print(f"  処理時間:     {elapsed:.1f}秒")
        print(f"  タグ付与成功: {fixer.stats['tagged']}")
        print(f"  Hydrus未検出: {fixer.stats['not_found']}")
        print(f"  エラー:       {fixer.stats['errors']}")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description='FANBOX R-18フォルダのインポート済みファイルに rating:r-18 タグを付与'
    )
    parser.add_argument(
        '--execute', action='store_true',
        help='実際にタグ修正を実行する（デフォルトはdry-run）'
    )
    parser.add_argument(
        '--limit', type=int, default=None,
        help='処理するファイル数の上限'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='詳細ログを表示'
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # .env読み込み
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

    # R-18フォルダスキャン
    print(f"\n📁 R-18フォルダをスキャン中... ({R18_FOLDER})")
    files = collect_r18_files(R18_FOLDER)

    if not files:
        print("⚠️ 対象ファイルが見つかりませんでした")
        return

    print(f"   → {len(files)} ファイル検出")

    if args.execute:
        if args.limit:
            print(f"⚠️ 制限モード: 先頭 {args.limit} ファイルのみ処理")
        asyncio.run(execute_fix(files, args.limit))
    else:
        print(f"\n📋 Dry-Run: {len(files)} ファイルに '{TAG_TO_ADD}' タグを付与予定")
        print(f"\n💡 実際に修正するには --execute フラグを追加してください")
        print(f"   python scripts/fix/r18_tags.py --execute")


if __name__ == '__main__':
    main()
