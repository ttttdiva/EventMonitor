#!/usr/bin/env python3
"""
ニジエ作品のHydrusインポート時刻を投稿日時順に振り直すスクリプト

背景:
  nijie_extractor.py のソートバグ（_p{num}の辞書順ソート）により、
  10ページ以上ある作品で画像のインポート順がサイト掲載順と異なっていた。
  また _sort_media_paths のregexが _p{num} パターンに非対応だったため、
  Hydrusへのインポート時にも順序が崩れていた。

  このスクリプトは:
  1. ローカルファイルから正しいページ順（_p0, _p1, ... _pN）を復元
  2. 作品の work_date を基準に、ページ順に+1秒ずつインポート時刻を設定
  3. ユーザーごとに全作品を古い順に処理

使用方法:
  # Dry-run（確認のみ）
  python scripts/fix/import_times_nijie.py --dry-run

  # 実行
  python scripts/fix/import_times_nijie.py

  # ユーザー指定
  python scripts/fix/import_times_nijie.py --username 12345

  # 特定の作品IDのみ
  python scripts/fix/import_times_nijie.py --work-id 552002

  # 進捗リセット
  python scripts/fix/import_times_nijie.py --reset

前提:
  - Hydrus APIキーに "Edit Times" 権限が必要
"""

import sys
import os
import re
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
PROGRESS_FILE = LOGS_DIR / "fix_nijie_import_times_progress.json"


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


def get_nijie_works(
    db_path: str,
    limit: Optional[int] = None,
    skip_ids: Optional[Set[str]] = None,
    user_ids: Optional[List[str]] = None,
    work_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """nijie_worksテーブルからHydrusインポート済み作品を日時昇順で取得"""
    conn = _open_db(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='nijie_works'"
    )
    if not cursor.fetchone():
        conn.close()
        print("エラー: nijie_works テーブルが見つかりません")
        return []

    query = """
        SELECT id, user_id, display_name, title, work_date, work_url,
               local_media
        FROM nijie_works
        WHERE local_media IS NOT NULL AND length(local_media) > 2
    """
    params: list = []

    if user_ids:
        placeholders = ','.join('?' for _ in user_ids)
        query += f" AND user_id IN ({placeholders})"
        params.extend(user_ids)

    if work_ids:
        placeholders = ','.join('?' for _ in work_ids)
        query += f" AND id IN ({placeholders})"
        params.extend(work_ids)

    # 日時昇順 → 同一日時はID昇順
    query += " ORDER BY work_date ASC, CAST(id AS INTEGER) ASC"

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
    """nijie_worksテーブルに存在するuser_idの一覧を取得"""
    conn = _open_db(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='nijie_works'"
    )
    if not cursor.fetchone():
        conn.close()
        return []

    cursor.execute("SELECT DISTINCT user_id FROM nijie_works ORDER BY user_id")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return user_ids


# ========== ページ順ソート ==========

def _sort_local_media_by_page(local_media_list: List[str]) -> List[str]:
    """
    ローカルメディアパスを _p{num} のページ番号順にソート
    例: 552002_p0.jpg, 552002_p1.jpg, ..., 552002_p10.jpg
    """
    def page_key(path: str):
        name = Path(path).name
        m = re.search(r'_p(\d+)', name)
        return int(m.group(1)) if m else 0

    return sorted(local_media_list, key=page_key)


# ========== 処理 ==========

def _build_ordered_hashes_from_local(
    hydrus: HydrusClient,
    local_media_list: List[str],
    config: dict,
) -> List[str]:
    """ローカルファイルからハッシュを計算し、ページ順のリストを返す"""
    # まずページ番号順にソート
    sorted_media = _sort_local_media_by_page(local_media_list)

    ordered = []
    for media_path in sorted_media:
        file_path = to_absolute_path(media_path, config)
        if file_path.exists():
            ordered.append(hydrus._calculate_file_hash(file_path))
    return ordered


def _parse_work_date(work_date_str: str) -> float:
    """work_dateをUnixタイムスタンプに変換"""
    if not work_date_str:
        return datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()

    for fmt in (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%d',
    ):
        try:
            dt = datetime.strptime(work_date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    return datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()


async def process_work(
    hydrus: HydrusClient,
    work: Dict[str, Any],
    file_service_key: str,
    config: dict,
    dry_run: bool = False,
) -> Dict[str, int]:
    """1件の作品のインポート時刻を修正（ページ順に+1秒ずつ）"""
    result = {'files_found': 0, 'files_updated': 0, 'files_not_found': 0, 'errors': 0}

    work_url = work['work_url']
    work_date = str(work.get('work_date', ''))
    base_timestamp = _parse_work_date(work_date)

    # URLでHydrus内のファイルを検索
    url_hashes = await hydrus.search_files_by_url(work_url)

    # ローカルファイルからハッシュ順序を構築（正しいページ順）
    local_hashes = []
    if work.get('local_media_list'):
        local_hashes = _build_ordered_hashes_from_local(
            hydrus, work['local_media_list'], config
        )

    # ローカルハッシュ順をベースに、URL検索結果と照合
    if url_hashes and local_hashes:
        url_hash_set = set(url_hashes)
        file_hashes = [h for h in local_hashes if h in url_hash_set]
        # ローカルにないがURL検索で見つかったファイルも追加
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
        ts = base_timestamp + i  # +i秒でページ順を維持
        dt_display = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            '%Y-%m-%d %H:%M:%S'
        )

        if dry_run:
            print(f"    [DRY-RUN] p{i} {file_hash[:16]}... -> {dt_display} UTC")
            result['files_updated'] += 1
        else:
            success = await hydrus.set_file_import_time(
                file_hash, ts, file_service_key
            )
            if success:
                result['files_updated'] += 1
            else:
                result['errors'] += 1

        await asyncio.sleep(0.05)

    return result


async def main():
    parser = argparse.ArgumentParser(
        description='ニジエ作品のHydrusインポート時刻を投稿日時＋ページ順に振り直す'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='変更せずに確認のみ')
    parser.add_argument('--limit', type=int,
                        help='ユーザーごとの処理件数上限')
    parser.add_argument('--username', nargs='*',
                        help='指定ユーザーIDのみ処理（複数指定可）')
    parser.add_argument('--work-id', nargs='*',
                        help='指定作品IDのみ処理（複数指定可）')
    parser.add_argument('--reset', action='store_true',
                        help='進捗をリセットして最初から実行')
    args = parser.parse_args()

    print("=" * 60)
    print("Nijie Hydrus インポート時刻修正（投稿日時 + ページ順）")
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
    if args.work_id:
        # 作品ID指定時はユーザーフィルタなし
        target_users = [None]
    elif args.username:
        target_users = args.username
    else:
        target_users = get_user_ids_from_db(db_path)

    if not target_users:
        print("\n処理対象のユーザーがいません")
        return

    if args.work_id:
        print(f"\n対象作品ID: {', '.join(args.work_id)}")
    else:
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
                works = get_nijie_works(
                    db_path,
                    limit=args.limit,
                    skip_ids=progress.processed_ids if not args.work_id else None,
                    user_ids=[user_id] if user_id else None,
                    work_ids=args.work_id,
                )

                if not works:
                    label = user_id or "指定作品"
                    print(f"[{label}] 処理対象なし")
                    continue

                label = user_id or "指定作品"
                print(f"[{label}] {len(works)}件処理開始")

                for idx, work in enumerate(works):
                    work_id = work['id']
                    title = (work.get('title') or '')[:40]
                    media_count = len(work.get('local_media_list', []))
                    work_date = str(work.get('work_date', ''))[:19]

                    print(
                        f"  [{idx + 1}/{len(works)}] "
                        f"ID:{work_id} \"{title}\" "
                        f"({media_count}ファイル, {work_date})"
                    )

                    result = await process_work(
                        hydrus, work, file_service_key,
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

                    if not args.dry_run and not args.work_id:
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
