#!/usr/bin/env python3
"""
データベースに記録されているlocal_mediaファイルを全てHydrus Clientに再インポートするスクリプト
処理済み記録機能付きで、中断しても再開可能

使用方法:
    python scripts/hydrus/reimport.py [--limit N]

オプション:
    --limit N: 処理件数を制限（デフォルト: 全件）
"""

import sys
import os
import asyncio
import argparse
import json
import csv
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Set
from concurrent.futures import ThreadPoolExecutor
import yaml
from dotenv import load_dotenv

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .envファイルを読み込み（プロジェクトルートから明示的に読み込み、既存の環境変数を上書き）
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

# 標準のsqlite3を使用（Windows環境ではpysqlite3-binaryが非推奨のため）
import sqlite3

# プロジェクトのモジュールをインポート
from src.hydrus_client import HydrusClient


# 処理済みレコードを記録するファイル（logsディレクトリ内）
logs_dir = Path("logs")
logs_dir.mkdir(exist_ok=True)  # logsディレクトリがなければ作成
PROGRESS_FILE = logs_dir / "reimport_progress.json"

# 並列処理設定
MAX_CONCURRENT_RECORDS = 3  # 同時処理レコード数
MAX_CONCURRENT_FILES = 5    # レコード内のファイル同時処理数


def load_progress() -> Set[str]:
    """
    処理済みのツイートIDを読み込む
    
    Returns:
        処理済みツイートIDのセット
    """
    if not PROGRESS_FILE.exists():
        return set()
    
    try:
        with open(PROGRESS_FILE, 'r') as f:
            data = json.load(f)
            return set(data.get('processed_tweet_ids', []))
    except Exception as e:
        print(f"警告: 進捗ファイルの読み込みに失敗しました: {e}")
        return set()


class ProgressManager:
    """進捗管理クラス - バッファリングでI/Oを最適化"""

    def __init__(self):
        self.processed_ids = load_progress()
        self.buffer = set()
        self.save_threshold = 50  # 50件ごとに保存

    def add(self, tweet_id: str):
        """処理済みIDを追加"""
        self.buffer.add(tweet_id)
        if len(self.buffer) >= self.save_threshold:
            self.flush()

    def flush(self):
        """バッファをファイルに保存"""
        if not self.buffer:
            return

        self.processed_ids.update(self.buffer)
        self.buffer.clear()
        self._save_to_file()

    def _save_to_file(self):
        """ファイルに保存"""
        try:
            with open(PROGRESS_FILE, 'w') as f:
                json.dump({
                    'processed_tweet_ids': list(self.processed_ids),
                    'last_updated': datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            print(f"警告: 進捗ファイルの保存に失敗しました: {e}")

    def is_processed(self, tweet_id: str) -> bool:
        """処理済みかチェック"""
        return tweet_id in self.processed_ids or tweet_id in self.buffer


def save_progress(processed_ids: Set[str]) -> None:
    """
    処理済みのツイートIDを保存（互換性維持）

    Args:
        processed_ids: 処理済みツイートIDのセット
    """
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({
                'processed_tweet_ids': list(processed_ids),
                'last_updated': datetime.now().isoformat()
            }, f, indent=2)
    except Exception as e:
        print(f"警告: 進捗ファイルの保存に失敗しました: {e}")


def clear_progress() -> None:
    """
    進捗ファイルを削除
    """
    if PROGRESS_FILE.exists():
        try:
            PROGRESS_FILE.unlink()
            print("進捗ファイルを削除しました")
        except Exception as e:
            print(f"警告: 進捗ファイルの削除に失敗しました: {e}")


def batch_file_check(file_paths: List[Path]) -> Dict[str, bool]:
    """
    複数ファイルの存在を一括チェック

    Args:
        file_paths: チェック対象ファイルパスのリスト

    Returns:
        ファイルパス: 存在フラグのdict
    """
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {str(path): executor.submit(path.exists) for path in file_paths}
        return {path_str: future.result() for path_str, future in futures.items()}


def update_hydrus_status(conn: sqlite3.Connection, tweet_id: str, imported_count: int, expected_count: int) -> None:
    """Hydrusインポート状態をDBに反映（all_tweets/event_tweets）"""
    try:
        for table_name in ("all_tweets", "event_tweets"):
            conn.execute(
                f"""
                UPDATE {table_name}
                SET
                    hydrus_expected_count = ?,
                    hydrus_imported_count = ?
                WHERE id = ?
                """,
                (expected_count, imported_count, tweet_id),
            )
        conn.commit()
    except Exception as e:
        print(f"警告: Hydrus状態のDB更新に失敗しました ({tweet_id}): {e}")


def ensure_hydrus_columns(conn: sqlite3.Connection) -> None:
    """Hydrus管理用カラムが無ければ追加"""
    required_columns = [
        ("hydrus_expected_count", "INTEGER DEFAULT 0"),
        ("hydrus_imported_count", "INTEGER DEFAULT 0"),
    ]
    for table_name in ("all_tweets", "event_tweets"):
        try:
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            existing = {row[1] for row in cursor.fetchall()}
            for column_name, column_type in required_columns:
                if column_name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
        except Exception as e:
            print(f"警告: {table_name} のカラム追加に失敗しました: {e}")
    conn.commit()


async def get_media_records(db_path: str, limit: Optional[int] = None, skip_ids: Set[str] = None,
                            exclude_log_accounts: bool = True, usernames: Optional[List[str]] = None,
                            skip_completed: bool = True) -> List[Dict[str, Any]]:
    """
    データベースからlocal_mediaがあるレコードを取得（最適化版）

    Args:
        db_path: データベースファイルパス
        limit: 取得件数制限
        skip_ids: スキップするツイートIDのセット
        exclude_log_accounts: logアカウントを除外するか

    Returns:
        レコードのリスト
    """
    conn = sqlite3.connect(db_path)
    # SQLiteパフォーマンス最適化
    conn.execute("PRAGMA cache_size = -2000")    # 2MBキャッシュ
    conn.execute("PRAGMA temp_store = MEMORY")   # テンポラリをメモリに
    conn.execute("PRAGMA journal_mode = WAL")    # WALモード
    cursor = conn.cursor()
    
    # logアカウントのリストを取得
    log_accounts = set()
    if exclude_log_accounts:
        import csv
        csv_path = Path('monitored_accounts.csv')
        if csv_path.exists():
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('account_type', '').lower() == 'log':
                        log_accounts.add(row['username'])

        if log_accounts:
            print(f"logアカウント {len(log_accounts)}件を除外: {', '.join(sorted(log_accounts)[:5])}{'...' if len(log_accounts) > 5 else ''}")

    base_query = """
        SELECT id, username, display_name, tweet_text, tweet_date,
               tweet_url, local_media, created_at,
               COALESCE(hydrus_expected_count, 0) AS hydrus_expected_count,
               COALESCE(hydrus_imported_count, 0) AS hydrus_imported_count
        FROM all_tweets
        WHERE local_media IS NOT NULL AND length(local_media) > 2
    """

    params = []
    if usernames:
        placeholders = ','.join('?' for _ in usernames)
        base_query += f" AND username IN ({placeholders})"
        params.extend(usernames)

    base_query += " ORDER BY tweet_date ASC"

    if limit:
        base_query += f" LIMIT {limit}"
    
    cursor.execute(base_query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    
    records = []
    skipped_count = 0
    log_skipped_count = 0
    completed_skipped_count = 0
    for row in rows:
        record = dict(zip(columns, row))

        # logアカウントの場合はスキップ
        if exclude_log_accounts and record['username'] in log_accounts:
            log_skipped_count += 1
            continue

        # 完了済みレコードはスキップ（expected/importedが揃っている場合）
        if skip_completed:
            expected = int(record.get('hydrus_expected_count') or 0)
            imported = int(record.get('hydrus_imported_count') or 0)
            if expected > 0 and imported >= expected:
                completed_skipped_count += 1
                continue

        # 処理済みの場合はスキップ
        if skip_ids and record['id'] in skip_ids:
            skipped_count += 1
            continue

        # local_mediaをJSONパース
        try:
            record['local_media_list'] = json.loads(record['local_media'])
        except:
            record['local_media_list'] = []
        records.append(record)
    
    if skipped_count > 0:
        print(f"処理済みレコードを{skipped_count}件スキップしました")
    if log_skipped_count > 0:
        print(f"logアカウントのレコードを{log_skipped_count}件スキップしました")
    if completed_skipped_count > 0:
        print(f"既に完了済みのレコードを{completed_skipped_count}件スキップしました")
    
    conn.close()
    return records


async def get_media_records_per_user(db_path: str, per_user_limit: int, skip_ids: Set[str] = None,
                                     exclude_log_accounts: bool = True, usernames: Optional[List[str]] = None,
                                     skip_completed: bool = True) -> List[Dict[str, Any]]:
    """
    ユーザーごとに最新N件のlocal_mediaレコードを取得
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()

    # logアカウントのリストを取得
    log_accounts = set()
    if exclude_log_accounts:
        csv_path = Path('monitored_accounts.csv')
        if csv_path.exists():
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('account_type', '').lower() == 'log':
                        log_accounts.add(row['username'])

    # 対象ユーザーの取得
    if usernames:
        target_users = list(dict.fromkeys(usernames))
    else:
        cursor.execute("""
            SELECT DISTINCT username
            FROM all_tweets
            WHERE local_media IS NOT NULL AND length(local_media) > 2
        """)
        target_users = [row[0] for row in cursor.fetchall()]

    if exclude_log_accounts and log_accounts:
        target_users = [u for u in target_users if u not in log_accounts]
        if log_accounts:
            print(f"logアカウント {len(log_accounts)}件を除外: {', '.join(sorted(log_accounts)[:5])}{'...' if len(log_accounts) > 5 else ''}")

    records = []
    skipped_count = 0
    completed_skipped_count = 0

    for username in target_users:
        cursor.execute(
            """
            SELECT id, username, display_name, tweet_text, tweet_date,
                   tweet_url, local_media, created_at,
                   COALESCE(hydrus_expected_count, 0) AS hydrus_expected_count,
                   COALESCE(hydrus_imported_count, 0) AS hydrus_imported_count
            FROM all_tweets
            WHERE local_media IS NOT NULL AND length(local_media) > 2
              AND username = ?
            ORDER BY COALESCE(created_at, tweet_date) DESC
            LIMIT ?
            """,
            (username, per_user_limit),
        )
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        for row in rows:
            record = dict(zip(columns, row))

            if skip_ids and record['id'] in skip_ids:
                skipped_count += 1
                continue

            if skip_completed:
                expected = int(record.get('hydrus_expected_count') or 0)
                imported = int(record.get('hydrus_imported_count') or 0)
                if expected > 0 and imported >= expected:
                    completed_skipped_count += 1
                    continue

            try:
                record['local_media_list'] = json.loads(record['local_media'])
            except Exception:
                record['local_media_list'] = []
            records.append(record)

    if skipped_count > 0:
        print(f"処理済みレコードを{skipped_count}件スキップしました")
    if completed_skipped_count > 0:
        print(f"既に完了済みのレコードを{completed_skipped_count}件スキップしました")

    conn.close()
    return records


async def process_single_file(hydrus: HydrusClient, media_path: str, config: dict, tweet_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    1つのファイルを処理

    Args:
        hydrus: HydrusClientインスタンス
        media_path: メディアファイルパス
        config: 設定
        tweet_data: ツイートデータ
    Returns:
        処理結果
    """
    result = {'processed': 0, 'skipped': 0, 'failed': 0, 'errors': []}

    # 相対パスを絶対パスに変換
    if media_path.startswith('images/'):
        images_base = Path(config.get('media_storage', {}).get('images_path', 'images'))
        relative_part = media_path[7:]
        file_path = images_base / relative_part
    elif media_path.startswith('videos/'):
        videos_base = Path(config.get('media_storage', {}).get('videos_path', 'videos'))
        relative_part = media_path[7:]
        file_path = videos_base / relative_part
    else:
        file_path = Path(media_path)

    # ファイル存在チェック
    if not file_path.exists():
        result['skipped'] += 1
        result['errors'].append(f"ファイルが存在しません: {media_path}")
        return result

    # 動画ファイルはスキップ
    video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m3u8']
    if file_path.suffix.lower() in video_extensions:
        result['skipped'] += 1
        return result

    # images/ディレクトリのファイルのみ処理
    # Windows対応: パス区切り文字を正規化してチェック
    path_str = str(file_path).replace('\\', '/')
    if '/images/' not in path_str and 'images/' not in path_str:
        result['skipped'] += 1
        return result

    try:
        # ファイルをインポート
        file_hash = await hydrus.import_file(file_path)

        if file_hash:
            # タグを生成して追加
            tags = hydrus._generate_tags(tweet_data)
            await hydrus.add_tags(file_hash, tags, platform="twitter")

            # ツイートURLを関連付け
            tweet_url = f"https://twitter.com/{tweet_data['username']}/status/{tweet_data['id']}"
            await hydrus.associate_url(file_hash, tweet_url)

            # ツイート本文をnoteとして追加
            if tweet_data.get('content'):
                import re
                cleaned_text = tweet_data['content'].strip()
                cleaned_text = cleaned_text.replace('\t', ' ')
                cleaned_text = re.sub(r'https?://t\.co/\S+', '', cleaned_text).strip()
                lines = [line.strip() for line in cleaned_text.split('\n')]
                cleaned_text = '\n'.join(line for line in lines if line)

                if cleaned_text:
                    await hydrus.add_note(file_hash, "twitter description", cleaned_text)

            result['processed'] += 1
        else:
            result['failed'] += 1
            result['errors'].append(f"インポート失敗: {media_path}")

    except Exception as e:
        result['failed'] += 1
        result['errors'].append(f"エラー ({media_path}): {str(e)}")

    return result


async def process_record(hydrus: HydrusClient, record: Dict[str, Any], config: dict, max_concurrent_files: int = 5) -> Dict[str, Any]:
    """
    1件のレコードを処理してHydrusにインポート（並列処理版）

    Args:
        hydrus: HydrusClientインスタンス
        record: データベースレコード
        config: 設定
    Returns:
        処理結果
    """
    result = {
        'tweet_id': record['id'],
        'username': record['username'],
        'total_files': len(record['local_media_list']),
        'processed': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    # ツイートデータを準備
    tweet_data = {
        'id': record['id'],
        'username': record['username'],
        'display_name': record['display_name'],
        'content': record['tweet_text'],
        'text': record['tweet_text'],
        'date': record['tweet_date']
    }

    # ファイルを並列処理
    semaphore = asyncio.Semaphore(max_concurrent_files)

    async def process_with_semaphore(media_path):
        async with semaphore:
            return await process_single_file(hydrus, media_path, config, tweet_data)

    # 全ファイルを並列処理
    tasks = [process_with_semaphore(media_path) for media_path in record['local_media_list']]
    file_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 結果を集計
    for file_result in file_results:
        if isinstance(file_result, Exception):
            result['failed'] += 1
            result['errors'].append(f"例外: {str(file_result)}")
        else:
            result['processed'] += file_result['processed']
            result['skipped'] += file_result['skipped']
            result['failed'] += file_result['failed']
            result['errors'].extend(file_result['errors'])

    return result


async def process_record_legacy(hydrus: HydrusClient, record: Dict[str, Any], config: dict) -> Dict[str, Any]:
    """
    1件のレコードを処理してHydrusにインポート（従来版・互換性維持）

    Args:
        hydrus: HydrusClientインスタンス
        record: データベースレコード
    Returns:
        処理結果
    """
    result = {
        'tweet_id': record['id'],
        'username': record['username'],
        'total_files': len(record['local_media_list']),
        'processed': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    # ツイートデータを準備（HydrusClientが期待する形式）
    tweet_data = {
        'id': record['id'],
        'username': record['username'],
        'display_name': record['display_name'],
        'content': record['tweet_text'],
        'text': record['tweet_text'],  # 互換性のため両方設定
        'date': record['tweet_date']
    }

    for media_path in record['local_media_list']:
        # 相対パスを絶対パスに変換
        if media_path.startswith('images/'):
            # config.yamlから実際のパスを取得
            images_base = Path(config.get('media_storage', {}).get('images_path', 'images'))
            # images/以降の部分を取得して結合
            relative_part = media_path[7:]  # 'images/'の7文字を除去
            file_path = images_base / relative_part
        elif media_path.startswith('videos/'):
            videos_base = Path(config.get('media_storage', {}).get('videos_path', 'videos'))
            relative_part = media_path[7:]  # 'videos/'の7文字を除去
            file_path = videos_base / relative_part
        else:
            file_path = Path(media_path)
        
        # ファイル存在チェック
        if not file_path.exists():
            result['skipped'] += 1
            result['errors'].append(f"ファイルが存在しません: {media_path}")
            continue
        
        # 動画ファイルはスキップ
        video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m3u8']
        if file_path.suffix.lower() in video_extensions:
            result['skipped'] += 1
            continue
        
        # images/ディレクトリのファイルのみ処理（/mnt/f/48_EventMonitor_log/images/も含む）
        # Windows対応: パス区切り文字を正規化してチェック
        path_str = str(file_path).replace('\\', '/')
        if '/images/' not in path_str and 'images/' not in path_str:
            result['skipped'] += 1
            continue
        
        try:
            # ファイルをインポート（既存ファイルは自動的にスキップされ、ハッシュのみ返される）
            file_hash = await hydrus.import_file(file_path)
            
            if file_hash:
                # 既存ファイルでも、タグとメタデータは更新する（重複チェックはimport_file内で実施済み）
                
                # タグを生成して追加
                tags = hydrus._generate_tags(tweet_data)
                tags_added = await hydrus.add_tags(file_hash, tags, platform="twitter")
                
                # ツイートURLを関連付け
                tweet_url = f"https://twitter.com/{record['username']}/status/{record['id']}"
                await hydrus.associate_url(file_hash, tweet_url)
                
                # ツイート本文をnoteとして追加
                if record['tweet_text']:
                    import re
                    cleaned_text = record['tweet_text'].strip()
                    cleaned_text = cleaned_text.replace('\t', ' ')
                    cleaned_text = re.sub(r'https?://t\.co/\S+', '', cleaned_text).strip()
                    lines = [line.strip() for line in cleaned_text.split('\n')]
                    cleaned_text = '\n'.join(line for line in lines if line)
                    
                    if cleaned_text:
                        await hydrus.add_note(file_hash, "twitter description", cleaned_text)
                
                result['processed'] += 1
                # より簡潔な表示（既存ファイルかどうかは内部で判断済み）
                print(f"  ✓ {media_path}")
            else:
                result['failed'] += 1
                result['errors'].append(f"インポート失敗: {media_path}")
                print(f"  ✗ Failed: {media_path}")
                
        except Exception as e:
            result['failed'] += 1
            result['errors'].append(f"エラー ({media_path}): {str(e)}")
            print(f"  ✗ Error: {media_path} - {e}")
    
    return result


async def main():
    parser = argparse.ArgumentParser(description='Hydrusへのメディア再インポート（再開機能付き）')
    parser.add_argument('--limit', type=int, help='処理件数を制限')
    parser.add_argument('--username', nargs='*', help='指定ユーザーのみ処理（複数指定可）')
    parser.add_argument('--reset', action='store_true', help='進捗をリセットして最初から実行')
    parser.add_argument('--include-log', action='store_true', help='logアカウントも含めて処理')
    parser.add_argument('--legacy', action='store_true', help='従来の逐次処理を使用（デバッグ用）')
    parser.add_argument('--force-all', action='store_true', help='完了済みレコードも含めて全件処理')
    parser.add_argument('--per-user-limit', type=int, help='ユーザーごとに最新N件だけ処理')
    parser.add_argument('--concurrent-records', type=int, default=3, help='同時処理レコード数')
    parser.add_argument('--concurrent-files', type=int, default=5, help='レコード内ファイル同時処理数')
    args = parser.parse_args()

    # 並列処理設定を更新
    max_concurrent_records = args.concurrent_records
    max_concurrent_files = args.concurrent_files

    # リセットオプションが指定された場合
    if args.reset:
        clear_progress()
        print("進捗をリセットしました")
    
    # 処理済みIDを読み込み
    processed_ids = load_progress()
    if processed_ids:
        print(f"前回の処理を再開します（処理済み: {len(processed_ids)}件）")
    
    # 設定ファイルを読み込み
    with open('config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # データベースパス
    db_path = 'data/eventmonitor.db'
    
    # Hydrus管理カラムを先に保証
    precheck_conn = sqlite3.connect(db_path)
    ensure_hydrus_columns(precheck_conn)
    precheck_conn.close()

    # レコードを取得
    print(f"データベースから対象レコードを取得中...")
    exclude_log = not args.include_log
    if args.per_user_limit:
        records = await get_media_records_per_user(
            db_path,
            per_user_limit=args.per_user_limit,
            skip_ids=processed_ids,
            exclude_log_accounts=exclude_log,
            usernames=args.username,
            skip_completed=not args.force_all
        )
    else:
        records = await get_media_records(
            db_path,
            limit=args.limit,
            skip_ids=processed_ids,
            exclude_log_accounts=exclude_log,
            usernames=args.username,
            skip_completed=not args.force_all
        )
    print(f"対象レコード数: {len(records)}")
    
    if not records:
        print("処理対象のレコードがありません")
        if processed_ids and not args.limit:
            print("全レコードの処理が完了しています")
            clear_progress()
        return
    
    # 総ファイル数を計算
    total_files = sum(len(r['local_media_list']) for r in records)
    print(f"総ファイル数: {total_files}")
    
    # Hydrus接続確認
    print("\nHydrus Clientへの接続を確認中...")
    
    # Hydrus状態更新用の接続を確保
    status_conn = sqlite3.connect(db_path)
    status_conn.execute("PRAGMA journal_mode = WAL")
    status_conn.execute("PRAGMA synchronous = NORMAL")
    ensure_hydrus_columns(status_conn)

    # Hydrusクライアントを初期化
    try:
        async with HydrusClient(config) as hydrus:
            if not hydrus.enabled:
                print("エラー: Hydrus連携が無効になっています")
                print("config.yamlでhydrus.enabledをtrueに設定してください")
                return
            
            # 接続テスト
            if not hydrus._session_key:
                print("エラー: Hydrus APIに接続できませんでした")
                print("Hydrus Clientが起動していることを確認してください")
                return
            print("Hydrus APIに正常に接続しました")
            
            # 統計情報
            stats = {
                'total_records': len(records),
                'total_files': total_files,
                'processed_records': 0,
                'processed_files': 0,
                'skipped_files': 0,
                'failed_files': 0
            }
            
            if args.legacy:
                # 従来の逐次処理
                print(f"\n従来の逐次処理を開始します...")

                try:
                    for i, record in enumerate(records, 1):
                        print(f"\n[{i}/{len(records)}] @{record['username']} - ID: {record['id']} ({len(record['local_media_list'])}ファイル)")

                        result = await process_record_legacy(hydrus, record, config)

                        # 統計を更新
                        stats['processed_records'] += 1
                        stats['processed_files'] += result['processed']
                        stats['skipped_files'] += result['skipped']
                        stats['failed_files'] += result['failed']
                        expected_count = result['processed'] + result['failed']
                        update_hydrus_status(status_conn, record['id'], result['processed'], expected_count)

                        # 処理済みIDを記録
                        processed_ids.add(record['id'])

                        # 10件ごとに進捗を保存
                        if i % 10 == 0:
                            save_progress(processed_ids)
                            print(f"  → 進捗を保存しました")

                        # エラーがあれば表示
                        if result['errors']:
                            print(f"  エラー: {', '.join(result['errors'][:3])}")

                        # 進捗表示
                        if i % 50 == 0:
                            print(f"\n=== 進捗: {i}/{len(records)} レコード処理済み ===")
                            print(f"  処理: {stats['processed_files']}ファイル")
                            print(f"  スキップ: {stats['skipped_files']}ファイル")
                            print(f"  失敗: {stats['failed_files']}ファイル")

                except KeyboardInterrupt:
                    print("\n\n処理が中断されました")
                    save_progress(processed_ids)
                    print(f"進捗を保存しました（処理済み: {len(processed_ids)}件）")
                    print("次回実行時に自動的に再開されます")
                    return

                except Exception as e:
                    print(f"\n\nエラーが発生しました: {e}")
                    save_progress(processed_ids)
                    print(f"進捗を保存しました（処理済み: {len(processed_ids)}件）")
                    raise

            else:
                # 並列処理
                print(f"\n並列処理を開始します（同時レコード数: {max_concurrent_records}, 同時ファイル数: {max_concurrent_files}）...")

                # 進捗管理
                progress_manager = ProgressManager()
                # 既存の処理済みIDを使用
                progress_manager.processed_ids = processed_ids

                try:
                    # バッチ処理用のセマフォ
                    record_semaphore = asyncio.Semaphore(max_concurrent_records)

                    async def process_record_with_progress(i, record):
                        async with record_semaphore:
                            print(f"\n[{i+1}/{len(records)}] @{record['username']} - ID: {record['id']} ({len(record['local_media_list'])}ファイル)")

                            result = await process_record(hydrus, record, config, max_concurrent_files)

                            # 処理済みIDを記録
                            progress_manager.add(record['id'])

                            # エラーがあれば表示
                            if result['errors']:
                                print(f"  エラー: {', '.join(result['errors'][:3])}")

                            return result

                    # 全レコードを並列処理
                    tasks = [process_record_with_progress(i, record) for i, record in enumerate(records)]

                    # 結果を順次取得して統計を更新
                    for i, task in enumerate(asyncio.as_completed(tasks)):
                        result = await task

                        # 統計を更新
                        stats['processed_records'] += 1
                        stats['processed_files'] += result['processed']
                        stats['skipped_files'] += result['skipped']
                        stats['failed_files'] += result['failed']
                        expected_count = result['processed'] + result['failed']
                        update_hydrus_status(status_conn, result['tweet_id'], result['processed'], expected_count)

                        # 進捗表示
                        if (i + 1) % 10 == 0 or (i + 1) == len(records):
                            print(f"\n=== 進捗: {i+1}/{len(records)} レコード処理完了 ===")
                            print(f"  処理: {stats['processed_files']}ファイル")
                            print(f"  スキップ: {stats['skipped_files']}ファイル")
                            print(f"  失敗: {stats['failed_files']}ファイル")

                except KeyboardInterrupt:
                    print("\n\n処理が中断されました")
                    progress_manager.flush()
                    print(f"進捗を保存しました（処理済み: {len(progress_manager.processed_ids)}件）")
                    print("次回実行時に自動的に再開されます")
                    return

                except Exception as e:
                    print(f"\n\nエラーが発生しました: {e}")
                    progress_manager.flush()
                    print(f"進捗を保存しました（処理済み: {len(progress_manager.processed_ids)}件）")
                    raise

            # 最終的な進捗を保存
            if args.legacy:
                save_progress(processed_ids)
            else:
                progress_manager.flush()
            
            # 処理完了
            print(f"\n{'='*50}")
            print(f"処理完了！")
            print(f"  レコード: {stats['processed_records']}/{stats['total_records']}")
            print(f"  ファイル処理: {stats['processed_files']}")
            print(f"  ファイルスキップ: {stats['skipped_files']}")
            print(f"  ファイル失敗: {stats['failed_files']}")
            print(f"{'='*50}")
            
            # 全件処理完了の場合は進捗ファイルを削除
            if stats['processed_records'] == stats['total_records']:
                clear_progress()
                print("全レコードの処理が完了したため、進捗ファイルを削除しました")
    finally:
        status_conn.close()


if __name__ == '__main__':
    asyncio.run(main())
