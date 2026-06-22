#!/usr/bin/env python3
"""
Kemono ZIP展開済み画像の Hydrus import time を修正するスクリプト

reprocess_kemono_zips.py で展開・インポートされた画像の import time が
正しい順序になっていないケースを修正する。

各投稿の画像に対して、投稿日時ベース + index秒 のオフセットで
import time を再設定し、Hydrus上の表示順序を正しくする。

使い方:
  # Dry-run（対象確認のみ）
  python scripts/fix/import_times_kemono.py

  # 実行
  python scripts/fix/import_times_kemono.py --execute

  # 特定ユーザーのみ
  python scripts/fix/import_times_kemono.py --execute --user "fanbox/4894"
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

logger = logging.getLogger("FixKemonoImportTimes")

HYDRUS_IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.bmp',
    '.tiff', '.tif', '.avif', '.gif',
}


def _natural_sort_key(s: str):
    """自然順ソート用キー"""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]


def calculate_sha256(file_path: Path) -> Optional[str]:
    """ファイルのSHA256ハッシュを計算"""
    try:
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return None


def parse_work_date(date_str: str) -> Optional[int]:
    """work_dateをUnixタイムスタンプに変換"""
    if not date_str:
        return None
    for fmt in ['%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
        try:
            dt = datetime.strptime(str(date_str), fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def find_target_records(db_path: Path, user_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """import time修正が必要なレコードを検索（ZIPから展開された画像を含むもの）"""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ZIP展開済み = local_mediaに _XX_NNN. パターン（gallery-dl番号_連番）を含む
    if user_filter:
        cur.execute(
            "SELECT * FROM kemono_works WHERE user_id = ?",
            (user_filter,)
        )
    else:
        cur.execute("SELECT * FROM kemono_works")

    rows = []
    for r in cur.fetchall():
        d = dict(r)
        lm = json.loads(d['local_media']) if d['local_media'] else []
        has_zip_extracted = any(re.search(r'_\d{2}_\d{3}\.', p) for p in lm)
        if has_zip_extracted:
            rows.append(d)

    conn.close()
    return rows


async def resolve_file_service_key(api_url: str, access_key: str) -> Optional[str]:
    """ファイルサービスキーを取得"""
    async with aiohttp.ClientSession() as session:
        headers = {'Hydrus-Client-API-Access-Key': access_key}
        try:
            async with session.get(f"{api_url}/get_services", headers=headers) as resp:
                if resp.status != 200:
                    return None
                services = await resp.json()
        except Exception:
            return None

        for stype in ['local_files']:
            for svc in services.get(stype, []):
                if 'my files' in svc.get('name', '').lower():
                    return svc.get('service_key')

    return None


async def fix_import_times(
    records: List[Dict[str, Any]],
    media_base: Path,
    api_url: str,
    access_key: str,
):
    """import timeを修正"""
    file_service_key = await resolve_file_service_key(api_url, access_key)
    if not file_service_key:
        print("ERROR: file_service_key が取得できません")
        return {'fixed': 0, 'skipped': 0, 'errors': 0}

    stats = {'fixed': 0, 'skipped': 0, 'errors': 0}

    async with aiohttp.ClientSession() as session:
        json_headers = {
            'Hydrus-Client-API-Access-Key': access_key,
            'Content-Type': 'application/json',
        }

        for i, rec in enumerate(records, 1):
            work_id = rec['id']
            local_media = json.loads(rec['local_media']) if rec['local_media'] else []
            work_date = rec.get('work_date', '')

            base_ts = parse_work_date(work_date)
            if not base_ts:
                print(f"[{i}/{len(records)}] {work_id} - SKIP (日時パース失敗)")
                stats['skipped'] += 1
                continue

            # 画像ファイルのみ抽出（自然順ソート）
            image_media = [
                p for p in local_media
                if Path(p).suffix.lower() in HYDRUS_IMAGE_EXTENSIONS
                and 'images/' in p
            ]
            image_media.sort(key=_natural_sort_key)

            if not image_media:
                stats['skipped'] += 1
                continue

            print(f"[{i}/{len(records)}] {work_id} ({len(image_media)} files)")

            work_fixed = 0
            for idx, media_path in enumerate(image_media):
                abs_path = media_base / media_path
                if not abs_path.exists():
                    continue

                file_hash = calculate_sha256(abs_path)
                if not file_hash:
                    continue

                timestamp = base_ts + idx

                try:
                    async with session.post(
                        f"{api_url}/edit_times/set_time",
                        headers=json_headers,
                        json={
                            'hash': file_hash,
                            'timestamp': timestamp,
                            'timestamp_type': 3,
                            'file_service_key': file_service_key,
                        },
                    ) as resp:
                        if resp.status == 200:
                            work_fixed += 1
                        else:
                            stats['errors'] += 1
                except Exception:
                    stats['errors'] += 1

            if work_fixed > 0:
                date_str = datetime.fromtimestamp(base_ts, tz=timezone.utc).strftime('%Y-%m-%d')
                print(f"  → {work_fixed} files fixed (base: {date_str})")
                stats['fixed'] += work_fixed

    return stats


def main():
    parser = argparse.ArgumentParser(description='Kemono ZIP展開済み画像のimport time修正')
    parser.add_argument('--execute', action='store_true', help='実行（デフォルトはdry-run）')
    parser.add_argument('--user', type=str, default=None, help='特定ユーザーのみ (例: fanbox/4894)')
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # .env / config
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

    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    hydrus_config = config.get('hydrus', {})
    api_url = hydrus_config.get('api_url', 'http://127.0.0.1:45869').rstrip('/')
    access_key = os.environ.get('HYDRUS_ACCESS_KEY') or hydrus_config.get('access_key')

    # DB
    db_path = PROJECT_ROOT / 'data' / 'eventmonitor.db'
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}")
        sys.exit(1)

    # media_base解決
    media_base = Path(config.get('media', {}).get('save_dir', 'data/media'))
    for candidate in [media_base, media_base.parent, Path('F:/48_EventMonitor_log')]:
        if (candidate / 'images').exists():
            media_base = candidate
            break

    # 対象レコード検索
    records = find_target_records(db_path, args.user)
    if not records:
        print("修正対象のレコードはありません")
        return

    total_files = sum(
        len([p for p in json.loads(r['local_media']) if Path(p).suffix.lower() in HYDRUS_IMAGE_EXTENSIONS and 'images/' in p])
        for r in records
    )
    users = set(r['user_id'] for r in records)

    print(f"\n{'='*60}")
    print(f"Kemono import time 修正")
    print(f"{'='*60}")
    print(f"  対象レコード: {len(records)}")
    print(f"  対象ファイル: {total_files}")
    print(f"  対象ユーザー: {', '.join(sorted(users))}")
    print(f"  メディアベース: {media_base}")
    print(f"{'='*60}")

    if not args.execute:
        for rec in records:
            lm = json.loads(rec['local_media']) if rec['local_media'] else []
            imgs = [p for p in lm if Path(p).suffix.lower() in HYDRUS_IMAGE_EXTENSIONS and 'images/' in p]
            print(f"  {rec['id']}: {len(imgs)} files, date={rec.get('work_date', '?')[:10]}")
        print(f"\n実行するには --execute を追加してください")
        return

    if not access_key:
        print("ERROR: HYDRUS_ACCESS_KEY が設定されていません")
        sys.exit(1)

    stats = asyncio.run(fix_import_times(
        records=records,
        media_base=media_base,
        api_url=api_url,
        access_key=access_key,
    ))

    print(f"\n{'='*60}")
    print(f"完了!")
    print(f"  修正:   {stats['fixed']} files")
    print(f"  スキップ: {stats['skipped']} records")
    print(f"  エラー: {stats['errors']}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
