#!/usr/bin/env python3
"""
FANBOX作品のHydrusインポート時刻を修正するスクリプト

問題:
    以前の実装では、FANBOXインポート時にインポート時刻を投稿日時（work_date）に
    書き換えていたため、過去の投稿をインポートすると実際のインポート日より
    大幅に古い時刻が設定され、Hydrus上で最新順に表示されなくなっていた。

修正方法:
    1. DBのcreated_at（≒実際のインポート日時）をベースタイムスタンプとして使用
    2. 同じcreated_at日にインポートされた作品群はwork_date順でソートし、
       1秒ずつオフセットして投稿順序を維持
    3. 同一作品内の複数画像もファイル名順で1秒ずつオフセット

使用方法:
    python scripts/fix/fanbox_thumbnail_order.py --dry-run
    python scripts/fix/fanbox_thumbnail_order.py
    python scripts/fix/fanbox_thumbnail_order.py --username aspart929
    python scripts/fix/fanbox_thumbnail_order.py --limit 100
    python scripts/fix/fanbox_thumbnail_order.py --reset

前提:
    - Hydrus APIキーに "Edit Times" 権限が必要
"""

import sys
import os
import io
import re
import asyncio
import argparse
import json
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set

# Windows cp932でのUnicodeEncodeError防止
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
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
PROGRESS_FILE = LOGS_DIR / "fix_fanbox_thumbnail_order_progress.json"

# 作品間のギャップ（秒）。同一時刻の複数作品が重ならないよう保証する
WORK_GAP = 3


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


# ========== DB ==========

def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_fanbox_works(
    db_path: str,
    limit: Optional[int] = None,
    user_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """fanbox_works から local_media 付き作品を取得（2枚以上）"""
    conn = _open_db(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fanbox_works'"
    )
    if not cursor.fetchone():
        conn.close()
        return []

    query = """
        SELECT id, user_id, display_name, title, work_date, work_url,
               media_urls, local_media, created_at
        FROM fanbox_works
        WHERE local_media IS NOT NULL AND length(local_media) > 2
          AND media_urls IS NOT NULL AND length(media_urls) > 2
    """
    params: list = []

    if user_ids:
        placeholders = ','.join('?' for _ in user_ids)
        query += f" AND user_id IN ({placeholders})"
        params.extend(user_ids)

    query += " ORDER BY created_at ASC, work_date ASC"

    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    records = []
    for row in rows:
        record = dict(zip(columns, row))

        try:
            record['media_urls_list'] = json.loads(record['media_urls'])
        except Exception:
            record['media_urls_list'] = []

        try:
            record['local_media_list'] = json.loads(record['local_media'])
        except Exception:
            record['local_media_list'] = []

        # 2枚以上の作品のみ対象（1枚なら順序問題なし）
        if len(record['local_media_list']) < 2:
            continue

        records.append(record)

    conn.close()
    return records


# ========== ユーティリティ ==========

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
        '%Y-%m-%d %H:%M:%S.%f',
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


def parse_created_at(created_at_str) -> float:
    """created_atをUnixタイムスタンプに変換"""
    if not created_at_str:
        raise ValueError("created_at is empty")
    return parse_work_date(created_at_str)


def sort_local_media_natural(local_media_list: List[str]) -> List[str]:
    """local_mediaをファイル名の{num}部分で自然順ソート"""
    def extract_num(path: str) -> int:
        name = Path(path).stem
        match = re.search(r'_(\d+)$', name)
        return int(match.group(1)) if match else 0

    return sorted(local_media_list, key=extract_num)


def compute_file_hash(file_path: Path) -> Optional[str]:
    """ファイルのSHA256ハッシュを計算"""
    if not file_path.exists():
        return None
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def build_ordered_hashes(
    local_media_list: List[str],
    config: dict,
) -> List[Optional[str]]:
    """ローカルファイルを自然順ソートしてSHA256ハッシュのリストを返す"""
    sorted_media = sort_local_media_natural(local_media_list)
    ordered = []
    for media_path in sorted_media:
        file_path = to_absolute_path(media_path, config)
        ordered.append(compute_file_hash(file_path))
    return ordered


# ========== メイン処理 ==========

async def process_all_works(
    hydrus: HydrusClient,
    works: List[Dict[str, Any]],
    file_service_key: str,
    config: dict,
    progress: ProgressManager,
    dry_run: bool = False,
) -> Dict[str, int]:
    """全作品を一括処理してインポート時刻を設定

    核心のロジック:
    - created_at（実際のインポート日時）をベースタイムスタンプとして使用
    - 同一created_at日の作品はwork_date順でソートし、順序を維持
    - 一度タイムスタンプを割り当てたハッシュは、別の作品では再割り当てしない
    """
    stats = {
        'works_total': len(works),
        'works_processed': 0,
        'works_skipped_progress': 0,
        'works_skipped_no_match': 0,
        'works_skipped_all_deduped': 0,
        'files_reordered': 0,
        'files_local_missing': 0,
        'errors': 0,
    }

    # created_at + work_date + id でソート
    sorted_works = sorted(
        works,
        key=lambda w: (
            parse_created_at(w.get('created_at', w['work_date'])),
            parse_work_date(w['work_date']),
            str(w['id']),
        )
    )

    # グローバルハッシュ追跡: 複数作品に同一画像がある場合、最初の作品にのみ割り当て
    processed_hashes: Set[str] = set()
    # 前作品の終了タイムスタンプ（重複防止用）
    next_available_ts: float = 0.0

    try:
        for idx, work in enumerate(sorted_works, 1):
            work_id = str(work['id'])
            title = (work.get('title') or '')[:40]
            user_id = work.get('user_id', '')
            file_count = len(work['local_media_list'])
            created_at_str = (
                str(work.get('created_at', ''))[:19]
                if work.get('created_at') else '?'
            )
            work_date_str = (
                str(work['work_date'])[:19] if work.get('work_date') else '?'
            )

            # base_ts = created_at（実際のインポート日時）
            try:
                created_ts = parse_created_at(
                    work.get('created_at', work['work_date'])
                )
            except ValueError as e:
                print(f"[{idx}/{len(sorted_works)}] {work_id} ERROR: {e}")
                stats['errors'] += 1
                continue

            base_ts = max(created_ts, next_available_ts) if next_available_ts > 0 else created_ts

            # 処理済みなら、タイムスタンプ範囲だけ進めてスキップ
            if progress.is_processed(work_id):
                next_available_ts = base_ts + file_count + WORK_GAP
                stats['works_skipped_progress'] += 1
                continue

            print(
                f"[{idx}/{len(sorted_works)}] {work_id} "
                f"@{user_id} \"{title}\" "
                f"({file_count}files, imported:{created_at_str}, posted:{work_date_str})"
            )

            # ローカルファイルのハッシュを順序付きで取得
            local_hashes = build_ordered_hashes(work['local_media_list'], config)
            valid_local = [(i, h) for i, h in enumerate(local_hashes) if h is not None]

            if not valid_local:
                print(f"  ローカルファイル見つからず (skip)")
                stats['files_local_missing'] += file_count
                next_available_ts = base_ts + file_count + WORK_GAP
                continue

            missing = len(local_hashes) - len(valid_local)
            if missing > 0:
                stats['files_local_missing'] += missing

            # Hydrus URL検索で該当ファイルのハッシュを取得
            url_hashes = await hydrus.search_files_by_url(work['work_url'])
            url_hash_set = set(h.lower() for h in url_hashes)

            if not url_hash_set:
                print(f"  Hydrusに未登録 (skip)")
                stats['works_skipped_no_match'] += 1
                next_available_ts = base_ts + file_count + WORK_GAP
                continue

            # 正しい順序の構築:
            # 1. ローカルにあり且つHydrusにもある → ローカル順序で
            # 2. Hydrusにあるがローカルにない → 末尾に追加
            local_hash_set = set(h.lower() for _, h in valid_local)
            ordered_hashes: List[str] = []
            for _, h in valid_local:
                if h.lower() in url_hash_set:
                    ordered_hashes.append(h)

            for h in url_hashes:
                if h.lower() not in local_hash_set:
                    if h.lower() not in {oh.lower() for oh in ordered_hashes}:
                        ordered_hashes.append(h)

            # 既に別の作品で処理済みのハッシュを除外
            new_hashes = [
                h for h in ordered_hashes
                if h.lower() not in processed_hashes
            ]

            if not new_hashes:
                print(f"  全画像が他の作品で処理済み (skip)")
                stats['works_skipped_all_deduped'] += 1
                next_available_ts = base_ts + file_count + WORK_GAP
                continue

            # タイムスタンプ設定
            ts_shift = base_ts - created_ts
            if ts_shift > 0:
                print(f"  base_ts調整: +{ts_shift:.0f}秒 (前作品との重複回避)")

            reordered = 0
            for i, file_hash in enumerate(new_hashes):
                timestamp = base_ts + i

                if dry_run:
                    dt_display = datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    ).strftime('%Y-%m-%d %H:%M:%S')
                    in_local = file_hash.lower() in local_hash_set
                    label = f"[{i}]" if in_local else f"[{i}] (url-only)"
                    print(f"  [DRY-RUN] {label} {file_hash[:16]}... -> {dt_display} UTC")
                else:
                    success = await hydrus.set_file_import_time(
                        file_hash, timestamp, file_service_key
                    )
                    if not success:
                        stats['errors'] += 1
                        continue

                processed_hashes.add(file_hash.lower())
                reordered += 1
                await asyncio.sleep(0.02)

            stats['files_reordered'] += reordered
            stats['works_processed'] += 1

            if reordered > 0:
                print(f"  -> {reordered}ファイル設定完了")
                if not dry_run:
                    progress.add(work_id)

            # 次の作品の開始点を更新
            allocated = max(len(new_hashes), file_count)
            next_available_ts = base_ts + allocated + WORK_GAP

            if idx % 100 == 0:
                print(f"\n=== 経過: {idx}/{len(sorted_works)} ===")
                print(
                    f"  処理: {stats['works_processed']}, "
                    f"スキップ(済): {stats['works_skipped_progress']}, "
                    f"スキップ(未登録): {stats['works_skipped_no_match']}, "
                    f"スキップ(重複): {stats['works_skipped_all_deduped']}, "
                    f"設定: {stats['files_reordered']}, "
                    f"エラー: {stats['errors']}"
                )
                print()

    except KeyboardInterrupt:
        print("\n\n中断、進捗保存中...")
    finally:
        if not dry_run:
            progress.flush()

    return stats


async def main():
    parser = argparse.ArgumentParser(
        description='FANBOX作品のHydrusインポート時刻をcreated_at（実際のインポート日時）ベースに修正'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='変更せずに確認のみ')
    parser.add_argument('--limit', type=int,
                        help='処理する最大作品数')
    parser.add_argument('--username', nargs='*',
                        help='クリエイターIDフィルタ（例: aspart929）')
    parser.add_argument('--reset', action='store_true',
                        help='進捗をリセットして全作品を再処理')
    args = parser.parse_args()

    print("=" * 60)
    print("FANBOX Hydrus import time fix (created_at based)")
    print("  インポート時刻をcreated_at（実際のインポート日時）ベースに修正")
    print("  同一日インポート分はwork_date順で並べて順序を保持")
    if args.dry_run:
        print("  [DRY-RUN]")
    print("=" * 60)

    # 進捗リセット
    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("進捗リセット完了")

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
        print(f"ERROR: DB not found: {db_path}")
        return

    # 進捗
    progress = ProgressManager()
    if progress.processed_ids:
        print(f"処理済み: {len(progress.processed_ids)}件")

    # FANBOX作品取得
    works = get_fanbox_works(
        db_path,
        limit=args.limit,
        user_ids=args.username,
    )

    if not works:
        print("対象作品なし")
        return

    # 処理済みでない作品数をカウント
    unprocessed = sum(
        1 for w in works if not progress.is_processed(str(w['id']))
    )
    print(f"対象: {len(works)}作品 (未処理: {unprocessed})")
    print()

    if unprocessed == 0:
        print("全作品処理済み。--reset で再処理できます")
        return

    # Hydrus
    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("ERROR: Hydrus disabled")
            return

        if not hydrus._session_key:
            print("ERROR: Hydrus API接続失敗")
            return

        print("Hydrus API OK")

        file_service_key = await hydrus.get_file_service_key()
        if not file_service_key:
            print("ERROR: ファイルサービスキー取得失敗")
            print("  -> Hydrus APIキーに 'Edit Times' 権限が必要")
            return

        print(f"file service key: {file_service_key[:16]}...")
        print()

        stats = await process_all_works(
            hydrus, works, file_service_key, config, progress, args.dry_run,
        )

    # サマリ
    print()
    print("=" * 60)
    print("完了!")
    print(f"  対象作品:           {stats['works_total']}")
    print(f"  処理:               {stats['works_processed']}")
    print(f"  スキップ(処理済み): {stats['works_skipped_progress']}")
    print(f"  スキップ(未登録):   {stats['works_skipped_no_match']}")
    print(f"  スキップ(重複):     {stats['works_skipped_all_deduped']}")
    print(f"  ファイル設定:       {stats['files_reordered']}")
    print(f"  ローカル欠損:       {stats['files_local_missing']}")
    print(f"  エラー:             {stats['errors']}")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
