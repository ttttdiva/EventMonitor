#!/usr/bin/env python3
"""
Pixiv作品のHydrusインポート時刻を修正するスクリプト

gallery-dlが新着順で返していた時期にインポートされた画像は
Hydrusでのインポート順が逆になっている。
このスクリプトは既存のインポート済みファイルのインポート時刻を
Pixivのオリジナル投稿日（work_date）に設定する。

使用方法:
    python scripts/fix/import_times_pixiv.py --dry-run
    python scripts/fix/import_times_pixiv.py
    python scripts/fix/import_times_pixiv.py --username 12345678
    python scripts/fix/import_times_pixiv.py --limit 100

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

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .envファイルを読み込み
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient
from src.path_utils import to_absolute_path

# 進捗管理
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "fix_pixiv_import_times_progress.json"


class ProgressManager:
    """進捗管理クラス"""

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
        except Exception as e:
            print(f"警告: 進捗ファイルの読み込みに失敗しました: {e}")
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
                    'last_updated': datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            print(f"警告: 進捗ファイルの保存に失敗しました: {e}")

    def is_processed(self, work_id: str) -> bool:
        return work_id in self.processed_ids or work_id in self.buffer


def get_pixiv_works(
    db_path: str,
    limit: Optional[int] = None,
    skip_ids: Optional[Set[str]] = None,
    user_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """pixiv_worksテーブルからwork_date昇順でレコードを取得"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()

    query = """
        SELECT id, user_id, display_name, title, work_date, work_url,
               local_media, page_count
        FROM pixiv_works
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

        if skip_ids and record['id'] in skip_ids:
            skipped += 1
            continue

        try:
            record['local_media_list'] = json.loads(record['local_media'])
        except Exception:
            record['local_media_list'] = []

        records.append(record)

    if skipped > 0:
        print(f"処理済みの作品を{skipped}件スキップしました")

    conn.close()
    return records


def parse_work_date(work_date_str: str) -> float:
    """work_date文字列をUnixタイムスタンプに変換"""
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.strptime(work_date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue

    # fromisoformat でフォールバック
    try:
        dt = datetime.fromisoformat(work_date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass

    raise ValueError(f"work_dateのパースに失敗: {work_date_str}")


async def process_work(
    hydrus: HydrusClient,
    work: Dict[str, Any],
    file_service_key: str,
    config: dict,
    dry_run: bool = False,
) -> Dict[str, int]:
    """1件のPixiv作品のインポート時刻を修正"""
    result = {'files_found': 0, 'files_updated': 0, 'files_not_found': 0, 'errors': 0}

    work_id = work['id']
    work_url = work['work_url']

    # work_dateをタイムスタンプに変換
    try:
        base_timestamp = parse_work_date(work['work_date'])
    except ValueError as e:
        print(f"  エラー: {e}")
        result['errors'] += 1
        return result

    # URLでHydrus内のファイルを検索
    file_hashes = await hydrus.search_files_by_url(work_url)

    # フォールバック: ローカルファイルのハッシュで検索
    if not file_hashes and work.get('local_media_list'):
        for media_path in work['local_media_list']:
            file_path = to_absolute_path(media_path, config)
            if file_path.exists():
                file_hash = hydrus._calculate_file_hash(file_path)
                exists = await hydrus._check_file_exists(file_hash)
                if exists:
                    file_hashes.append(file_hash)

    if not file_hashes:
        result['files_not_found'] += 1
        return result

    result['files_found'] = len(file_hashes)

    # 各ファイルのインポート時刻を設定
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

        await asyncio.sleep(0.05)  # API負荷軽減

    return result


async def main():
    parser = argparse.ArgumentParser(
        description='Pixiv作品のHydrusインポート時刻を修正'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='変更せずに確認のみ')
    parser.add_argument('--limit', type=int,
                        help='処理件数を制限')
    parser.add_argument('--username', nargs='*',
                        help='指定ユーザーIDのみ処理（Pixiv user_id、複数指定可）')
    parser.add_argument('--reset', action='store_true',
                        help='進捗をリセットして最初から実行')
    args = parser.parse_args()

    print("=" * 55)
    print("Pixiv Hydrus インポート時刻修正スクリプト")
    if args.dry_run:
        print("[DRY-RUN モード - 変更は行いません]")
    print("=" * 55)

    # 進捗リセット
    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("進捗をリセットしました")

    # config読み込み
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # DB パス
    db_config = config.get('database', {})
    db_path = db_config.get('path', 'data/eventmonitor.db')
    if not Path(db_path).is_absolute():
        db_path = str(PROJECT_ROOT / db_path)

    if not Path(db_path).exists():
        print(f"エラー: データベースが見つかりません: {db_path}")
        return

    # 進捗管理
    progress = ProgressManager()

    # Pixiv作品を取得
    works = get_pixiv_works(
        db_path,
        limit=args.limit,
        skip_ids=progress.processed_ids,
        user_ids=args.username,
    )

    if not works:
        print("処理対象の作品がありません")
        return

    print(f"対象Pixiv作品: {len(works)}件")
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

        # ファイルサービスキー取得
        file_service_key = await hydrus.get_file_service_key()
        if not file_service_key:
            print("エラー: ファイルサービスキーが取得できません")
            print("  Hydrus APIキーに 'Edit Times' 権限があるか確認してください")
            print("  Hydrus > services > review services > client api で設定")
            return

        print(f"ファイルサービスキー: {file_service_key[:16]}...")
        print()

        # 統計
        total_found = 0
        total_updated = 0
        total_not_found = 0
        total_errors = 0

        try:
            for idx, work in enumerate(works, 1):
                work_id = work['id']
                title = (work.get('title') or '')[:40]
                display_name = work.get('display_name', '')
                page_count = work.get('page_count', 1)
                work_date = work['work_date'][:10] if work.get('work_date') else '?'

                print(f"[{idx}/{len(works)}] ID:{work_id} @{display_name} \"{title}\" ({page_count}p, {work_date})")

                result = await process_work(hydrus, work, file_service_key, config, args.dry_run)

                total_found += result['files_found']
                total_updated += result['files_updated']
                total_not_found += result['files_not_found']
                total_errors += result['errors']

                if result['files_found'] > 0:
                    print(f"  {result['files_found']}ファイル発見, {result['files_updated']}件時刻修正")
                elif result['files_not_found'] > 0:
                    print(f"  Hydrus未登録（スキップ）")

                if result['errors'] > 0:
                    print(f"  エラー: {result['errors']}件")

                if not args.dry_run:
                    progress.add(work_id)

                # 定期的に進捗表示
                if idx % 50 == 0:
                    print(f"\n=== 進捗: {idx}/{len(works)} 作品処理完了 ===")
                    print(f"  時刻修正: {total_updated}ファイル, 未登録: {total_not_found}, エラー: {total_errors}")
                    print()

        except KeyboardInterrupt:
            print("\n\n中断されました。進捗を保存します...")
        finally:
            if not args.dry_run:
                progress.flush()

    # サマリー
    print()
    print("=" * 55)
    print("処理完了！")
    print(f"  Hydrusファイル発見: {total_found}")
    print(f"  時刻修正: {total_updated}")
    print(f"  Hydrus未登録: {total_not_found}")
    print(f"  エラー: {total_errors}")
    print("=" * 55)


if __name__ == '__main__':
    asyncio.run(main())
