#!/usr/bin/env python3
"""
Kemono ZIP展開 一括再処理スクリプト

クローラーが既に処理済みの kemono_works レコードのうち、local_media に
ZIPファイルが含まれるものを検出し、以下を実行する:

  1. ディスク上の ZIP を展開（画像ファイルのみ）
  2. DB の local_media を更新（ZIP パス → 展開画像パスに置換）
  3. hydrus_expected_count を再計算
  4. Hydrus API で展開画像をインポート + タグ付与

使い方:
  # Dry-run（対象レコード確認のみ）
  python scripts/fix/reprocess_kemono_zips.py

  # 実行
  python scripts/fix/reprocess_kemono_zips.py --execute

  # 特定ユーザーのみ
  python scripts/fix/reprocess_kemono_zips.py --execute --user "fanbox/4894"
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

logger = logging.getLogger("ReprocessKemonoZips")

IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.bmp', '.tiff', '.tif', '.avif', '.jfif',
}
HYDRUS_IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.bmp',
    '.tiff', '.tif', '.avif', '.gif',
}
MY_TAGS_KEY = '6c6f63616c2074616773'  # "my tags" (local tags)


# ========== ユーティリティ ==========

def _natural_sort_key(s: str):
    """自然順ソート用キー（数値部分を数値として比較）"""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', s)
    ]


# ========== ZIP展開 ==========

def extract_zip_to_images(zip_path: Path) -> List[Path]:
    """
    ZIPから画像ファイルを展開する。

    展開先はZIPと同じディレクトリ。
    命名: {zip_stem}_{inner_num:03d}.{ext}
    """
    if not zip_path.exists():
        logger.warning(f"ZIP not found: {zip_path}")
        return []

    extract_dir = zip_path.parent
    zip_prefix = zip_path.stem

    extracted: List[Path] = []
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            image_members = sorted(
                (
                    m for m in zf.namelist()
                    if not m.endswith('/')
                    and Path(m).suffix.lower() in IMAGE_EXTENSIONS
                ),
                key=_natural_sort_key,
            )
            if not image_members:
                logger.warning(f"No images in ZIP: {zip_path.name}")
                return []

            logger.info(f"Extracting {len(image_members)} images from {zip_path.name}")

            for idx, member in enumerate(image_members, 1):
                ext = Path(member).suffix.lower()
                new_name = f"{zip_prefix}_{idx:03d}{ext}"
                target = extract_dir / new_name

                if target.exists():
                    counter = 1
                    while target.exists():
                        new_name = f"{zip_prefix}_{idx:03d}_{counter}{ext}"
                        target = extract_dir / new_name
                        counter += 1

                target.write_bytes(zf.read(member))
                extracted.append(target)

    except (zipfile.BadZipFile, RuntimeError) as e:
        logger.error(f"ZIP extraction failed: {zip_path.name}: {e}")
        return []

    return extracted


def estimate_hydrus_expected(local_media: List[str]) -> int:
    """Hydrusインポート対象の画像ファイル数を推定"""
    count = 0
    for p in local_media:
        path_str = str(p).replace('\\', '/')
        if 'images/' not in path_str:
            continue
        if Path(path_str).suffix.lower() not in HYDRUS_IMAGE_EXTENSIONS:
            continue
        count += 1
    return count


# ========== CSV参照 ==========

def load_csv_accounts() -> Dict[str, Dict[str, Any]]:
    """monitored_accounts.csv を読み込み、kemono アカウント情報を返す"""
    csv_path = PROJECT_ROOT / 'monitored_accounts.csv'
    accounts: Dict[str, Dict[str, Any]] = {}
    if not csv_path.exists():
        return accounts

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            if len(row) < 5:
                continue
            platform = row[4].strip() if len(row) > 4 else ''
            if platform != 'kemono':
                continue
            username = row[0].strip()
            rank_str = row[6].strip() if len(row) > 6 else ''
            rank = int(rank_str) if rank_str.isdigit() else 3
            custom_tags_str = row[5].strip() if len(row) > 5 else ''
            custom_tags = [t.strip() for t in custom_tags_str.split(',') if t.strip()] if custom_tags_str else []
            accounts[username] = {
                'display_name': row[1].strip() if len(row) > 1 else '',
                'rank': rank,
                'custom_tags': custom_tags,
            }
    return accounts


# ========== タグ生成 ==========

def generate_kemono_tags(
    work_row: Dict[str, Any],
    csv_info: Optional[Dict[str, Any]],
) -> List[str]:
    """_generate_kemono_tags と同じルールでタグ生成"""
    tags = [
        'source:kemono',
        'imported_by:eventmonitor',
    ]

    service = work_row.get('service', '')
    if service:
        tags.append(f"service:{service}")

    work_id = work_row.get('id', '')
    if work_id:
        tags.append(f"kemono_id:{work_id}")

    display_name = work_row.get('display_name', '')
    if display_name:
        tags.append(f"creator:{display_name}")

    title = work_row.get('title', '')
    if title:
        if len(title) > 100:
            title = title[:97] + "..."
        tags.append(f"title:{title}")

    # Kemono は常に R-18
    tags.append('rating:r-18')

    rank = 3
    if csv_info:
        rank = csv_info.get('rank', 3)
    tags.append(f"rank:{rank}")

    if csv_info and csv_info.get('custom_tags'):
        tags.extend(csv_info['custom_tags'])

    return list(set(tags))


# ========== DB操作 ==========

def find_zip_records(db_path: Path, user_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """kemono_works から local_media に .zip を含むレコードを検索"""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if user_filter:
        cur.execute(
            "SELECT * FROM kemono_works WHERE user_id = ? AND local_media LIKE '%.zip%'",
            (user_filter,)
        )
    else:
        cur.execute(
            "SELECT * FROM kemono_works WHERE local_media LIKE '%.zip%'"
        )

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def update_record(db_path: Path, work_id: str, new_local_media: List[str], new_expected: int):
    """kemono_works の local_media と hydrus_expected_count を更新"""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "UPDATE kemono_works SET local_media = ?, hydrus_expected_count = ?, hydrus_imported_count = 0 WHERE id = ?",
        (json.dumps(new_local_media), new_expected, work_id)
    )
    conn.commit()
    conn.close()


# ========== Hydrusインポート ==========

async def import_to_hydrus(
    image_paths: List[Path],
    tags: List[str],
    work_url: str,
    note_name: str,
    note_text: str,
    api_url: str,
    access_key: str,
    tag_service_key: str,
    file_service_key: Optional[str] = None,
    work_date: Optional[str] = None,
) -> int:
    """画像をHydrusにインポートしてタグ付与。インポート成功数を返す。"""
    # 投稿日時をパース（import time設定用）
    base_timestamp = None
    if work_date:
        from datetime import datetime, timezone
        for fmt in ['%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
            try:
                dt = datetime.strptime(str(work_date), fmt).replace(tzinfo=timezone.utc)
                base_timestamp = int(dt.timestamp())
                break
            except ValueError:
                continue

    imported = 0
    async with aiohttp.ClientSession() as session:
        json_headers = {
            'Hydrus-Client-API-Access-Key': access_key,
            'Content-Type': 'application/json',
        }

        for idx, img_path in enumerate(image_paths):
            if not img_path.exists():
                continue
            if img_path.suffix.lower() not in HYDRUS_IMAGE_EXTENSIONS:
                continue

            # インポート
            import_headers = {
                'Hydrus-Client-API-Access-Key': access_key,
                'Content-Type': 'application/octet-stream',
            }
            try:
                with open(img_path, 'rb') as f:
                    async with session.post(
                        f"{api_url}/add_files/add_file",
                        headers=import_headers, data=f,
                    ) as resp:
                        if resp.status != 200:
                            continue
                        result = await resp.json()
            except Exception:
                continue

            status = result.get('status')
            file_hash = result.get('hash')
            if not file_hash:
                continue

            # 削除済みファイル復元
            if status == 3:
                try:
                    await session.post(
                        f"{api_url}/add_files/undelete_files",
                        headers=json_headers,
                        json={'hashes': [file_hash]},
                    )
                except Exception:
                    pass

            # タグ付与
            try:
                await session.post(
                    f"{api_url}/add_tags/add_tags",
                    headers=json_headers,
                    json={
                        'hashes': [file_hash],
                        'service_keys_to_actions_to_tags': {
                            tag_service_key: {'0': tags},
                        },
                        'override_previously_deleted_mappings': True,
                    },
                )
            except Exception:
                pass

            # URL関連付け
            if work_url:
                try:
                    await session.post(
                        f"{api_url}/add_urls/associate_url",
                        headers=json_headers,
                        json={'hash': file_hash, 'url_to_add': work_url},
                    )
                except Exception:
                    pass

            # ノート
            if note_text:
                try:
                    await session.post(
                        f"{api_url}/add_notes/set_notes",
                        headers=json_headers,
                        json={'hash': file_hash, 'notes': {note_name: note_text}},
                    )
                except Exception:
                    pass

            # インポート時刻設定（投稿日時 + idx秒で順序を保証）
            if base_timestamp and file_service_key:
                try:
                    await session.post(
                        f"{api_url}/edit_times/set_time",
                        headers=json_headers,
                        json={
                            'hash': file_hash,
                            'timestamp': base_timestamp + idx,
                            'timestamp_type': 3,
                            'file_service_key': file_service_key,
                        },
                    )
                except Exception:
                    pass

            imported += 1

    return imported


async def resolve_service_keys(api_url: str, access_key: str) -> Tuple[str, Optional[str]]:
    """タグサービスキーとファイルサービスキーを解決。"""
    tag_key = MY_TAGS_KEY
    file_key = None

    async with aiohttp.ClientSession() as session:
        headers = {'Hydrus-Client-API-Access-Key': access_key}
        try:
            async with session.get(f"{api_url}/get_services", headers=headers) as resp:
                if resp.status != 200:
                    return tag_key, file_key
                services = await resp.json()
        except Exception:
            return tag_key, file_key

        for stype in ['local_tags', 'tag_repositories']:
            for svc in services.get(stype, []):
                if svc.get('name') == 'kemono tags':
                    tag_key = svc.get('service_key', MY_TAGS_KEY)
                    break
            if tag_key != MY_TAGS_KEY:
                break

        for stype in ['local_files']:
            for svc in services.get(stype, []):
                if 'my files' in svc.get('name', '').lower():
                    file_key = svc.get('service_key')
                    break
            if file_key:
                break

    return tag_key, file_key


# ========== メイン ==========

async def execute(
    records: List[Dict[str, Any]],
    media_base: str,
    db_path: Path,
    csv_accounts: Dict[str, Dict[str, Any]],
    api_url: str,
    access_key: str,
):
    """ZIP展開 + DB更新 + Hydrusインポートを実行"""
    tag_service_key, file_service_key = await resolve_service_keys(api_url, access_key)
    logger.info(f"Tag service key: {tag_service_key[:20]}...")
    logger.info(f"File service key: {file_service_key[:20] if file_service_key else 'None'}...")

    stats = {'extracted': 0, 'images_total': 0, 'hydrus_imported': 0, 'errors': 0}

    for i, rec in enumerate(records, 1):
        work_id = rec['id']
        user_id = rec['user_id']
        local_media = json.loads(rec['local_media']) if rec['local_media'] else []

        print(f"\n[{i}/{len(records)}] {work_id} ({rec.get('title', '')[:40]})")

        new_local_media: List[str] = []
        extracted_abs_paths: List[Path] = []

        for media_path in local_media:
            if not media_path.endswith('.zip'):
                new_local_media.append(media_path)
                continue

            # ZIP展開
            abs_zip = Path(media_base) / media_path
            extracted = extract_zip_to_images(abs_zip)

            if extracted:
                stats['extracted'] += 1
                stats['images_total'] += len(extracted)
                # 相対パスに変換
                for ep in extracted:
                    abs_str = str(ep).replace('\\', '/')
                    if '/images/' in abs_str:
                        rel = 'images/' + abs_str.split('/images/')[1]
                    else:
                        rel = abs_str
                    new_local_media.append(rel)
                extracted_abs_paths.extend(extracted)

                # ZIP削除
                try:
                    abs_zip.unlink()
                    print(f"  ZIP展開: {abs_zip.name} → {len(extracted)} images")
                except Exception:
                    print(f"  ZIP展開OK (削除失敗): {abs_zip.name}")
            else:
                print(f"  ZIP展開失敗: {abs_zip.name}")
                new_local_media.append(media_path)
                stats['errors'] += 1

        # DB更新
        new_expected = estimate_hydrus_expected(new_local_media)
        update_record(db_path, work_id, new_local_media, new_expected)
        print(f"  DB更新: local_media={len(new_local_media)}件, expected={new_expected}")

        # Hydrusインポート（展開画像のみ）
        if extracted_abs_paths:
            csv_info = csv_accounts.get(user_id)
            tags = generate_kemono_tags(rec, csv_info)

            title = rec.get('title', '')
            content = rec.get('content', '')
            note_text = f"{title}\n\n{content}" if title and content else (title or content or '')

            imported = await import_to_hydrus(
                image_paths=extracted_abs_paths,
                tags=tags,
                work_url=rec.get('work_url', ''),
                note_name='kemono description',
                note_text=note_text,
                api_url=api_url,
                access_key=access_key,
                tag_service_key=tag_service_key,
                file_service_key=file_service_key,
                work_date=rec.get('work_date'),
            )
            stats['hydrus_imported'] += imported
            print(f"  Hydrus: {imported}/{len(extracted_abs_paths)} imported")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Kemono ZIP展開 一括再処理'
    )
    parser.add_argument('--execute', action='store_true', help='実行（デフォルトはdry-run）')
    parser.add_argument('--user', type=str, default=None, help='特定ユーザーのみ (例: fanbox/4894)')
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # .env / config 読み込み
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

    media_config = config.get('media', {})
    media_base = media_config.get('save_dir', 'data/media')
    # images/ の親ディレクトリ
    # local_media は "images/fanbox_4894/xxx.zip" 形式
    # media_base + "/" + local_media でフルパスになる
    media_base_path = Path(media_base).parent if 'images' in str(Path(media_base)) else Path(media_base)

    # DB
    db_path = PROJECT_ROOT / 'data' / 'eventmonitor.db'
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}")
        sys.exit(1)

    # ZIPレコード検索
    records = find_zip_records(db_path, args.user)
    if not records:
        print("ZIPを含むレコードはありません")
        return

    # media_base 解決: local_media の先頭パスから実際のベースを推定
    sample_media = json.loads(records[0]['local_media'])[0]
    # sample: "images/fanbox_4894/1112766_01.zip"
    # 実ファイルパス: F:/48_EventMonitor_log/images/fanbox_4894/1112766_01.zip
    # → media_base = F:/48_EventMonitor_log
    for candidate in [
        Path(media_base),
        Path(media_base).parent,
        Path('F:/48_EventMonitor_log'),
    ]:
        test_path = candidate / sample_media
        if test_path.exists():
            media_base_path = candidate
            break

    # CSV
    csv_accounts = load_csv_accounts()

    # レポート
    total_zips = sum(
        sum(1 for p in json.loads(r['local_media']) if p.endswith('.zip'))
        for r in records
    )
    users = set(r['user_id'] for r in records)

    print(f"\n{'='*60}")
    print(f"Kemono ZIP再処理")
    print(f"{'='*60}")
    print(f"  対象レコード:   {len(records)}")
    print(f"  ZIPファイル数:  {total_zips}")
    print(f"  対象ユーザー:   {', '.join(sorted(users))}")
    print(f"  メディアベース: {media_base_path}")
    print(f"{'='*60}")

    if not args.execute:
        # Dry-run: 詳細表示
        for rec in records:
            lm = json.loads(rec['local_media']) if rec['local_media'] else []
            zips = [p for p in lm if p.endswith('.zip')]
            imgs = [p for p in lm if not p.endswith('.zip')]
            zip_exists = all((media_base_path / z).exists() for z in zips)
            print(f"  {rec['id']}: ZIP={len(zips)} IMG={len(imgs)} disk={'OK' if zip_exists else 'MISSING'}")
        print(f"\n実行するには --execute を追加してください")
        return

    if not access_key:
        print("ERROR: HYDRUS_ACCESS_KEY が設定されていません")
        sys.exit(1)

    stats = asyncio.run(execute(
        records=records,
        media_base=str(media_base_path),
        db_path=db_path,
        csv_accounts=csv_accounts,
        api_url=api_url,
        access_key=access_key,
    ))

    print(f"\n{'='*60}")
    print(f"完了!")
    print(f"  ZIP展開:        {stats['extracted']}")
    print(f"  展開画像数:     {stats['images_total']}")
    print(f"  Hydrusインポート: {stats['hydrus_imported']}")
    print(f"  エラー:         {stats['errors']}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
