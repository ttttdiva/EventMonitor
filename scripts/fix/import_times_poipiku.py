#!/usr/bin/env python3
"""
Poipiku作品のHydrusインポート時刻をID順（古い順）に振り直すスクリプト

背景:
  Poipikuは投稿日時をHTML上に表示しないため、DBの work_date は
  すべてスクレイプ時の datetime.now() フォールバックが入っている。
  そのため他プラットフォームの fix_import_times.py とは異なり、
  work_date は使わず投稿ID（数値連番）の昇順で時刻を振り直す。

戦略:
  1. ユーザーごとに poipiku_works を ID 昇順で取得
  2. 基準日時 (--base-date) から --interval 秒間隔でインポート時刻を割り当て
  3. ID が小さい（古い）ほど早い時刻になる

使用方法:
  # Dry-run（確認のみ）
  python scripts/fix/import_times_poipiku.py --dry-run

  # 実行（基準日時: 2024-01-01 00:00:00 UTC、間隔60秒）
  python scripts/fix/import_times_poipiku.py --base-date 2024-01-01

  # ユーザー指定
  python scripts/fix/import_times_poipiku.py --username 123456

  # 間隔変更（デフォルト60秒）
  python scripts/fix/import_times_poipiku.py --interval 30

  # 進捗リセット
  python scripts/fix/import_times_poipiku.py --reset

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
PROGRESS_FILE = LOGS_DIR / "fix_poipiku_import_times_progress.json"


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


def get_poipiku_works(
    db_path: str,
    limit: Optional[int] = None,
    skip_ids: Optional[Set[str]] = None,
    user_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """poipiku_worksテーブルからHydrusインポート済み作品をID昇順で取得"""
    conn = _open_db(db_path)
    cursor = conn.cursor()

    # テーブル存在確認
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='poipiku_works'"
    )
    if not cursor.fetchone():
        conn.close()
        print("エラー: poipiku_works テーブルが見つかりません")
        return []

    query = """
        SELECT id, user_id, display_name, title, work_date, work_url,
               local_media
        FROM poipiku_works
        WHERE local_media IS NOT NULL AND length(local_media) > 2
    """
    params: list = []

    if user_ids:
        placeholders = ','.join('?' for _ in user_ids)
        query += f" AND user_id IN ({placeholders})"
        params.extend(user_ids)

    # ID昇順（数値として）- Poipikuの投稿IDは数値連番
    query += " ORDER BY CAST(id AS INTEGER) ASC"

    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    records = []
    skipped = 0
    for row in rows:
        record = dict(zip(columns, row))

        if skip_ids and record['id'] in skip_ids:
            skipped += 1
            continue

        try:
            record['local_media_list'] = json.loads(record['local_media'])
        except Exception:
            record['local_media_list'] = []

        records.append(record)

    if skipped > 0:
        print(f"  処理済み{skipped}件スキップ")

    conn.close()
    return records


def get_user_ids_from_db(db_path: str) -> List[str]:
    """poipiku_worksテーブルに存在するuser_idの一覧を取得"""
    conn = _open_db(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='poipiku_works'"
    )
    if not cursor.fetchone():
        conn.close()
        return []

    cursor.execute("SELECT DISTINCT user_id FROM poipiku_works ORDER BY user_id")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return user_ids


# ========== 処理 ==========

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
    timestamp: float,
    file_service_key: str,
    config: dict,
    dry_run: bool = False,
) -> Dict[str, int]:
    """1件の作品のインポート時刻を修正"""
    result = {'files_found': 0, 'files_updated': 0, 'files_not_found': 0, 'errors': 0}

    work_url = work['work_url']

    # URLでHydrus内のファイルを検索
    url_hashes = await hydrus.search_files_by_url(work_url)

    # ローカルファイルからハッシュ順序を構築（正しいページ順）
    local_hashes = []
    if work.get('local_media_list'):
        local_hashes = _build_ordered_hashes_from_local(
            hydrus, work['local_media_list'], config
        )

    if url_hashes and local_hashes:
        url_hash_set = set(url_hashes)
        file_hashes = [h for h in local_hashes if h in url_hash_set]
        local_hash_set = set(local_hashes)
        file_hashes.extend(h for h in url_hashes if h not in local_hash_set)
    elif url_hashes:
        file_hashes = url_hashes
    elif local_hashes:
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
        ts = timestamp + i  # +i秒でページ順を維持
        dt_display = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        if dry_run:
            print(f"    [DRY-RUN] {file_hash[:16]}... -> {dt_display} UTC")
            result['files_updated'] += 1
        else:
            success = await hydrus.set_file_import_time(file_hash, ts, file_service_key)
            if success:
                result['files_updated'] += 1
            else:
                result['errors'] += 1

        await asyncio.sleep(0.05)

    return result


async def main():
    parser = argparse.ArgumentParser(
        description='Poipiku作品のHydrusインポート時刻をID順（古い順）に振り直す'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='変更せずに確認のみ')
    parser.add_argument('--base-date', type=str, default='2024-01-01',
                        help='基準日時 (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)。'
                             'デフォルト: 2024-01-01')
    parser.add_argument('--interval', type=int, default=60,
                        help='作品間の秒数（デフォルト: 60）')
    parser.add_argument('--limit', type=int,
                        help='ユーザーごとの処理件数上限')
    parser.add_argument('--username', nargs='*',
                        help='指定ユーザーIDのみ処理（複数指定可）')
    parser.add_argument('--reset', action='store_true',
                        help='進捗をリセットして最初から実行')
    args = parser.parse_args()

    # 基準日時パース
    base_date_str = args.base_date
    base_dt = None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            base_dt = datetime.strptime(base_date_str, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if base_dt is None:
        print(f"エラー: 基準日時のパースに失敗: {base_date_str}")
        print("  フォーマット: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
        return

    base_timestamp = base_dt.timestamp()

    print("=" * 60)
    print("Poipiku Hydrus インポート時刻修正（ID昇順で再割り当て）")
    print(f"基準日時: {base_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"作品間隔: {args.interval}秒")
    if args.dry_run:
        print("[DRY-RUN モード - 変更は行いません]")
    print("=" * 60)

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

    # 対象ユーザー
    if args.username:
        target_users = args.username
    else:
        target_users = get_user_ids_from_db(db_path)

    if not target_users:
        print("\n処理対象のユーザーがいません")
        return

    print(f"\n対象ユーザー: {len(target_users)}人")
    for uid in target_users:
        print(f"  - {uid}")
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

        total_stats = {'found': 0, 'updated': 0, 'not_found': 0, 'errors': 0}

        try:
            for user_id in target_users:
                works = get_poipiku_works(
                    db_path,
                    limit=args.limit,
                    skip_ids=progress.processed_ids,
                    user_ids=[user_id],
                )

                if not works:
                    print(f"[{user_id}] 処理対象なし")
                    continue

                print(f"[{user_id}] {len(works)}件処理開始")

                for idx, work in enumerate(works):
                    work_id = work['id']
                    title = (work.get('title') or '')[:40]

                    # ID昇順のインデックスに基づいてタイムスタンプを割り当て
                    timestamp = base_timestamp + (idx * args.interval)
                    dt_display = datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    ).strftime('%Y-%m-%d %H:%M:%S')

                    print(
                        f"  [{idx + 1}/{len(works)}] "
                        f"ID:{work_id} \"{title}\" -> {dt_display} UTC"
                    )

                    result = await process_work(
                        hydrus, work, timestamp, file_service_key,
                        config, args.dry_run
                    )

                    total_stats['found'] += result['files_found']
                    total_stats['updated'] += result['files_updated']
                    total_stats['not_found'] += result['files_not_found']
                    total_stats['errors'] += result['errors']

                    if result['files_found'] > 0:
                        print(
                            f"    {result['files_found']}ファイル発見, "
                            f"{result['files_updated']}件修正"
                        )
                    elif result['files_not_found'] > 0:
                        print(f"    Hydrus未登録（スキップ）")

                    if result['errors'] > 0:
                        print(f"    エラー: {result['errors']}件")

                    if not args.dry_run:
                        progress.add(work_id)

                print()

        except KeyboardInterrupt:
            print("\n\n中断されました。進捗を保存します...")
        finally:
            if not args.dry_run:
                progress.flush()

    # サマリー
    print("=" * 60)
    print("処理完了")
    print(f"  Hydrusファイル発見: {total_stats['found']}")
    print(f"  時刻修正:           {total_stats['updated']}")
    print(f"  Hydrus未登録:       {total_stats['not_found']}")
    print(f"  エラー:             {total_stats['errors']}")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
