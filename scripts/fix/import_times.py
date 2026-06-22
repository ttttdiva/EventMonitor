#!/usr/bin/env python3
"""
全プラットフォーム（Pixiv/Kemono/TINAMI）のHydrusインポート時刻を
作品投稿日（work_date）に基づいて修正するスクリプト

背景:
  gallery-dl / スクレイパーは作品を新しい順で返す。
  バッチ処理で新しいバッチが先にインポートされるため、
  Hydrusのインポート時刻が投稿日の昇順にならない。
  このスクリプトは既存ファイルのインポート時刻を work_date に設定し直す。

使用方法:
  # Dry-run（全プラットフォーム）
  python scripts/fix/import_times.py --dry-run

  # 実行（全プラットフォーム）
  python scripts/fix/import_times.py

  # プラットフォーム指定
  python scripts/fix/import_times.py --platform pixiv
  python scripts/fix/import_times.py --platform kemono
  python scripts/fix/import_times.py --platform tinami

  # ユーザー指定
  python scripts/fix/import_times.py --platform pixiv --username 12345678
  python scripts/fix/import_times.py --platform kemono --username fanbox/3316400

  # 件数制限 / 進捗リセット
  python scripts/fix/import_times.py --limit 100
  python scripts/fix/import_times.py --reset

前提:
  - Hydrus APIキーに "Edit Times" 権限が必要
"""

import sys
import os
import asyncio
import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set
import re
import yaml
from dotenv import load_dotenv

# プロジェクトルート
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient
from src.path_utils import to_absolute_path

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "fix_import_times_progress.json"

ALL_PLATFORMS = ['pixiv', 'kemono', 'tinami']

_KEMONO_NUM_RE = re.compile(r'_(\d+)\.[^.]+$')


# ========== 進捗管理 ==========

class ProgressManager:
    def __init__(self):
        self.processed_ids = self._load()
        self.buffer: Set[str] = set()
        self.save_threshold = 20

    def _load(self) -> Set[str]:
        if not PROGRESS_FILE.exists():
            return set()
        try:
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
                return set(data.get('processed_work_ids', []))
        except Exception:
            return set()

    def add(self, work_id: str):
        self.buffer.add(work_id)
        if len(self.buffer) >= self.save_threshold:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        self.processed_ids.update(self.buffer)
        self.buffer.clear()
        self._save()

    def _save(self):
        try:
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({
                    'processed_work_ids': list(self.processed_ids),
                    'last_updated': datetime.now().isoformat(),
                }, f, indent=2)
        except Exception:
            pass

    def is_processed(self, work_id: str) -> bool:
        return work_id in self.processed_ids or work_id in self.buffer


# ========== DB読み取り ==========

def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_works(
    db_path: str,
    platform: str,
    limit: Optional[int] = None,
    skip_ids: Optional[Set[str]] = None,
    user_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """指定プラットフォームのHydrusインポート済み作品を取得"""
    table_map = {
        'pixiv': 'pixiv_works',
        'kemono': 'kemono_works',
        'tinami': 'tinami_works',
    }
    table = table_map.get(platform)
    if not table:
        return []

    conn = _open_db(db_path)
    cursor = conn.cursor()

    # テーブル存在確認
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    if not cursor.fetchone():
        conn.close()
        return []

    query = f"""
        SELECT id, user_id, display_name, title, work_date, work_url,
               local_media
        FROM {table}
        WHERE local_media IS NOT NULL AND length(local_media) > 2
    """
    params: list = []

    if user_ids:
        placeholders = ','.join('?' for _ in user_ids)
        query += f" AND user_id IN ({placeholders})"
        params.extend(user_ids)

    query += " ORDER BY work_date ASC"

    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    records = []
    skipped = 0
    for row in rows:
        record = dict(zip(columns, row))
        record['platform'] = platform

        if skip_ids and record['id'] in skip_ids:
            skipped += 1
            continue

        try:
            record['local_media_list'] = json.loads(record['local_media'])
        except Exception:
            record['local_media_list'] = []

        records.append(record)

    if skipped > 0:
        print(f"  {platform}: 処理済み{skipped}件スキップ")

    conn.close()
    return records


# ========== 日付パース ==========

def parse_work_date(work_date_str) -> float:
    """work_dateをUnixタイムスタンプに変換"""
    if isinstance(work_date_str, datetime):
        dt = work_date_str
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    if not work_date_str:
        raise ValueError("work_date is empty")

    for fmt in (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S',
    ):
        try:
            dt = datetime.strptime(str(work_date_str), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(str(work_date_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass

    raise ValueError(f"work_dateのパースに失敗: {work_date_str}")


# ========== 処理 ==========

def _sort_kemono_local_media(local_media_list: List[str]) -> List[str]:
    """Kemono local_mediaをgallery-dl {num}順（=サイト表示順）にソート

    既存DBデータはfile先頭の誤った順序で保存されている場合がある。
    ファイル名の{num}部分（例: 11454275_03.jpg → 3）でソートすることで
    gallery-dl順 = attachments→file = Kemonoサイト表示順に修正する。
    """
    def extract_num(path: str) -> int:
        match = _KEMONO_NUM_RE.search(path)
        return int(match.group(1)) if match else 999
    return sorted(local_media_list, key=extract_num)


def _build_ordered_hashes_from_local(
    hydrus: HydrusClient,
    local_media_list: List[str],
    config: dict,
) -> List[str]:
    """ローカルファイルからハッシュを計算し、local_media順のリストを返す"""
    ordered = []
    for media_path in local_media_list:
        file_path = to_absolute_path(media_path, config)
        if file_path.exists():
            ordered.append(hydrus._calculate_file_hash(file_path))
    return ordered


async def process_work(
    hydrus: HydrusClient,
    work: Dict[str, Any],
    file_service_key: str,
    config: dict,
    dry_run: bool = False,
) -> Dict[str, int]:
    """1件の作品のインポート時刻を修正"""
    result = {'files_found': 0, 'files_updated': 0, 'files_not_found': 0, 'errors': 0}

    work_url = work['work_url']

    try:
        base_timestamp = parse_work_date(work['work_date'])
    except ValueError as e:
        print(f"  エラー: {e}")
        result['errors'] += 1
        return result

    # URLでHydrus内のファイルを検索
    url_hashes = await hydrus.search_files_by_url(work_url)

    # ローカルファイルからハッシュ順序を構築（正しいページ順）
    local_media_list = work.get('local_media_list', [])
    # Kemono: 既存DBデータはfile先頭の誤った順序の可能性があるため
    # gallery-dl {num}順（=サイト表示順）にソートし直す
    if work.get('platform') == 'kemono' and local_media_list:
        local_media_list = _sort_kemono_local_media(local_media_list)
    local_hashes = []
    if local_media_list:
        local_hashes = _build_ordered_hashes_from_local(
            hydrus, local_media_list, config
        )

    if url_hashes and local_hashes:
        # URL検索結果をローカルファイル順に並べ替え
        url_hash_set = set(url_hashes)
        file_hashes = [h for h in local_hashes if h in url_hash_set]
        # ローカルに無いがURLで見つかったハッシュを末尾に追加
        local_hash_set = set(local_hashes)
        file_hashes.extend(h for h in url_hashes if h not in local_hash_set)
    elif url_hashes:
        # ローカルファイルが無い場合はURL検索結果をそのまま使用
        file_hashes = url_hashes
    elif local_hashes:
        # URL検索が空の場合はローカルハッシュでHydrus存在チェック
        file_hashes = []
        for file_hash in local_hashes:
            exists = await hydrus._check_file_exists(file_hash)
            if exists:
                file_hashes.append(file_hash)
    else:
        file_hashes = []

    if not file_hashes:
        result['files_not_found'] += 1
        return result

    result['files_found'] = len(file_hashes)

    for i, file_hash in enumerate(file_hashes):
        timestamp = base_timestamp + i  # +i秒でページ順を維持
        dt_display = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        if dry_run:
            print(f"  [DRY-RUN] {file_hash[:16]}... -> {dt_display} UTC")
            result['files_updated'] += 1
        else:
            success = await hydrus.set_file_import_time(file_hash, timestamp, file_service_key)
            if success:
                result['files_updated'] += 1
            else:
                result['errors'] += 1

        await asyncio.sleep(0.05)

    return result


async def main():
    parser = argparse.ArgumentParser(
        description='Pixiv/Kemono/TINAMI作品のHydrusインポート時刻を投稿日に修正'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='変更せずに確認のみ')
    parser.add_argument('--platform', choices=ALL_PLATFORMS,
                        help='対象プラットフォーム（省略時は全て）')
    parser.add_argument('--limit', type=int,
                        help='プラットフォームごとの処理件数上限')
    parser.add_argument('--username', nargs='*',
                        help='指定ユーザーIDのみ処理（複数指定可）')
    parser.add_argument('--reset', action='store_true',
                        help='進捗をリセットして最初から実行')
    args = parser.parse_args()

    platforms = [args.platform] if args.platform else ALL_PLATFORMS

    print("=" * 55)
    print("Hydrus インポート時刻修正（全プラットフォーム対応）")
    print(f"対象: {', '.join(platforms)}")
    if args.dry_run:
        print("[DRY-RUN モード - 変更は行いません]")
    print("=" * 55)

    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("進捗をリセットしました")

    # config
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # DB
    db_config = config.get('database', {})
    db_path = db_config.get('path', 'data/eventmonitor.db')
    if not Path(db_path).is_absolute():
        db_path = str(PROJECT_ROOT / db_path)

    if not Path(db_path).exists():
        print(f"エラー: データベースが見つかりません: {db_path}")
        return

    # 進捗
    progress = ProgressManager()

    # 全プラットフォームの作品を収集
    all_works: List[Dict[str, Any]] = []
    for platform in platforms:
        works = get_works(
            db_path, platform,
            limit=args.limit,
            skip_ids=progress.processed_ids,
            user_ids=args.username,
        )
        print(f"  {platform}: {len(works)}件")
        all_works.extend(works)

    if not all_works:
        print("\n処理対象の作品がありません")
        return

    print(f"\n合計: {len(all_works)}件")
    print()

    # Hydrus接続
    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("エラー: Hydrus連携が無効です")
            return

        if not hydrus._session_key:
            print("エラー: Hydrus APIに接続できません")
            return

        print("Hydrus API接続OK")

        file_service_key = await hydrus.get_file_service_key()
        if not file_service_key:
            print("エラー: ファイルサービスキーが取得できません")
            print("  Hydrus APIキーに 'Edit Times' 権限があるか確認してください")
            return

        print(f"ファイルサービスキー: {file_service_key[:16]}...")
        print()

        # 統計（プラットフォーム別）
        stats: Dict[str, Dict[str, int]] = {
            p: {'found': 0, 'updated': 0, 'not_found': 0, 'errors': 0}
            for p in ALL_PLATFORMS
        }

        try:
            for idx, work in enumerate(all_works, 1):
                work_id = work['id']
                platform = work['platform']
                title = (work.get('title') or '')[:40]
                display_name = work.get('display_name', '')
                work_date = str(work['work_date'])[:10] if work.get('work_date') else '?'

                print(
                    f"[{idx}/{len(all_works)}] [{platform}] "
                    f"ID:{work_id} @{display_name} \"{title}\" ({work_date})"
                )

                result = await process_work(
                    hydrus, work, file_service_key, config, args.dry_run
                )

                s = stats[platform]
                s['found'] += result['files_found']
                s['updated'] += result['files_updated']
                s['not_found'] += result['files_not_found']
                s['errors'] += result['errors']

                if result['files_found'] > 0:
                    print(f"  {result['files_found']}ファイル発見, {result['files_updated']}件修正")
                elif result['files_not_found'] > 0:
                    print(f"  Hydrus未登録（スキップ）")

                if result['errors'] > 0:
                    print(f"  エラー: {result['errors']}件")

                if not args.dry_run:
                    progress.add(f"{platform}:{work_id}")

                if idx % 50 == 0:
                    print(f"\n=== 進捗: {idx}/{len(all_works)} ===\n")

        except KeyboardInterrupt:
            print("\n\n中断されました。進捗を保存します...")
        finally:
            if not args.dry_run:
                progress.flush()

    # サマリー
    print()
    print("=" * 55)
    print("処理完了")
    for platform in platforms:
        s = stats[platform]
        total = s['found'] + s['not_found'] + s['errors']
        if total == 0:
            continue
        print(f"\n  [{platform}]")
        print(f"    Hydrusファイル発見: {s['found']}")
        print(f"    時刻修正:           {s['updated']}")
        print(f"    Hydrus未登録:       {s['not_found']}")
        print(f"    エラー:             {s['errors']}")
    print("=" * 55)


if __name__ == '__main__':
    asyncio.run(main())
