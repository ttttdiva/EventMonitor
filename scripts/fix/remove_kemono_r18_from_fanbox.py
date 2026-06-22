#!/usr/bin/env python3
"""
FANBOX non-sensitive作品からKemono由来の rating:r-18 タグを除去するスクリプト

問題:
  Kemonoクローラーはセンシティブ情報を持たないため、全作品に rating:r-18 を付与する。
  同一画像がFANBOXでもインポートされた場合、Hydrusの重複統合により
  FANBOXでnon-sensitiveな画像にも rating:r-18 が残ってしまう。

修正方法:
  1. DBからFANBOX non-sensitive作品のlocal_mediaパスを取得
  2. 各ファイルのSHA256でHydrusのファイルを特定
  3. rating:r-18 タグを削除

使い方:
  # Dry-run（対象ファイル数の確認のみ）
  python scripts/fix/remove_kemono_r18_from_fanbox.py

  # 実行
  python scripts/fix/remove_kemono_r18_from_fanbox.py --execute

  # 件数制限
  python scripts/fix/remove_kemono_r18_from_fanbox.py --execute --limit 50
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

from src.path_utils import to_absolute_path

logger = logging.getLogger("RemoveKemonoR18")

TAG_TO_REMOVE = 'rating:r-18'

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.jfif', '.avif', '.tiff', '.tif'}


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def collect_target_files(config: dict) -> List[Path]:
    """DBからFANBOX non-sensitive作品のlocal_mediaパスを取得し、実在ファイルを返す"""
    import sqlite3

    db_path = PROJECT_ROOT / 'data' / 'eventmonitor.db'
    if not db_path.exists():
        logger.error(f"DBが見つかりません: {db_path}")
        return []

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, local_media FROM fanbox_works "
        "WHERE sensitive = 0 AND local_media IS NOT NULL AND local_media != '[]'"
    )
    rows = cur.fetchall()
    conn.close()

    files = []
    missing = 0
    for work_id, local_media_json in rows:
        try:
            paths = json.loads(local_media_json)
        except (json.JSONDecodeError, TypeError):
            continue

        for rel_path in paths:
            abs_path = to_absolute_path(rel_path, config)
            if abs_path.exists() and abs_path.suffix.lower() in IMAGE_EXTENSIONS:
                files.append(abs_path)
            else:
                missing += 1

    if missing:
        logger.info(f"ファイル未検出（移動済み等）: {missing}件")

    return files


class HydrusTagRemover:
    """Hydrusファイルからタグを一括削除"""

    def __init__(self, api_url: str, access_key: str):
        self.api_url = api_url.rstrip('/')
        self.access_key = access_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.stats = {
            'removed': 0,
            'not_found': 0,
            'no_tag': 0,
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

    async def file_has_tag(self, file_hash: str, tag: str) -> Optional[bool]:
        """ファイルが指定タグを持つか確認。存在しない場合None"""
        headers = {'Hydrus-Client-API-Access-Key': self.access_key}
        params = {'hashes': json.dumps([file_hash])}
        try:
            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                metadata_list = data.get('metadata', [])
                if not metadata_list:
                    return None

                meta = metadata_list[0]
                if meta.get('is_trashed', False):
                    return None

                # 全タグサービスのタグを走査
                tags_services = meta.get('tags', {})
                for svc_key, svc_data in tags_services.items():
                    storage = svc_data.get('storage_tags', {})
                    # status 0 = current tags
                    current_tags = storage.get('0', [])
                    if tag in current_tags:
                        return True
                return False
        except Exception as e:
            logger.error(f"メタデータ取得エラー: {e}")
            return None

    async def remove_tag(self, file_hash: str, tag: str) -> bool:
        """ファイルからタグを削除（全タグサービスから）"""
        # local tags (action 1 = delete)
        data = {
            'hashes': [file_hash],
            'service_keys_to_actions_to_tags': {
                '6c6f63616c2074616773': {
                    '1': [tag],
                }
            },
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
                    logger.error(f"タグ削除APIエラー ({resp.status}): {error[:200]}")
                    return False
        except Exception as e:
            logger.error(f"タグ削除エラー: {e}")
            return False

    async def fix_file(self, file_path: Path) -> bool:
        """1ファイルのrating:r-18タグを削除"""
        file_hash = compute_sha256(file_path)

        has_tag = await self.file_has_tag(file_hash, TAG_TO_REMOVE)
        if has_tag is None:
            self.stats['not_found'] += 1
            return False
        if not has_tag:
            self.stats['no_tag'] += 1
            return False

        success = await self.remove_tag(file_hash, TAG_TO_REMOVE)
        if success:
            self.stats['removed'] += 1
            return True
        else:
            self.stats['errors'] += 1
            return False


async def execute_fix(files: List[Path], limit: Optional[int] = None):
    """タグ削除を実行"""
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
    print(f"\n修正開始: {total} ファイルから '{TAG_TO_REMOVE}' を削除")

    async with HydrusTagRemover(api_url, access_key) as fixer:
        start_time = time.time()

        for i, file_path in enumerate(files, 1):
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0

            print(
                f"\r  [{i}/{total}] "
                f"({i * 100 // total}%) "
                f"ETA: {eta:.0f}s "
                f"| 削除:{fixer.stats['removed']} "
                f"タグなし:{fixer.stats['no_tag']} "
                f"未検出:{fixer.stats['not_found']} "
                f"エラー:{fixer.stats['errors']} "
                f"| {file_path.name[:40]}",
                end='', flush=True
            )

            await fixer.fix_file(file_path)

            if i % 10 == 0:
                await asyncio.sleep(0.1)

        print()
        elapsed = time.time() - start_time

        print("\n" + "=" * 70)
        print("タグ削除結果")
        print("=" * 70)
        print(f"  処理時間:     {elapsed:.1f}秒")
        print(f"  タグ削除成功: {fixer.stats['removed']}")
        print(f"  タグなし:     {fixer.stats['no_tag']}")
        print(f"  Hydrus未検出: {fixer.stats['not_found']}")
        print(f"  エラー:       {fixer.stats['errors']}")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description='FANBOX non-sensitive作品からKemono由来の rating:r-18 タグを除去'
    )
    parser.add_argument(
        '--execute', action='store_true',
        help='実際にタグ削除を実行する（デフォルトはdry-run）'
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

    # config読み込み
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 対象ファイル収集
    print(f"\nDBからFANBOX non-sensitive作品のファイルを収集中...")
    files = collect_target_files(config)

    if not files:
        print("対象ファイルが見つかりませんでした")
        return

    print(f"   -> {len(files)} ファイル検出")

    if args.execute:
        if args.limit:
            print(f"制限モード: 先頭 {args.limit} ファイルのみ処理")
        asyncio.run(execute_fix(files, args.limit))
    else:
        print(f"\nDry-Run: {len(files)} ファイルから '{TAG_TO_REMOVE}' タグを削除予定")
        print(f"\n実際に修正するには --execute フラグを追加してください")
        print(f"   python scripts/fix/remove_kemono_r18_from_fanbox.py --execute")


if __name__ == '__main__':
    main()
