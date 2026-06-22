#!/usr/bin/env python3
"""
センシティブフラグ再処理スクリプト

既にダウンロード済みの画像に対して:
1. Pixiv: x_restrict値からsensitiveフラグをDB更新
2. Twitter: gallery-dlで再フェッチしてsensitiveフラグをDB更新
3. Hydrus: sensitive=TrueのレコードにHydrusでrating:r-18タグを付与

使用方法:
    python scripts/fix/reprocess_sensitive.py --all
    python scripts/fix/reprocess_sensitive.py --pixiv-db
    python scripts/fix/reprocess_sensitive.py --twitter-refetch
    python scripts/fix/reprocess_sensitive.py --hydrus-sync
    python scripts/fix/reprocess_sensitive.py --hydrus-sync --dry-run
"""

import sys
import os
import asyncio
import argparse
import json
import hashlib
import subprocess
import csv
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Tuple
import yaml
from dotenv import load_dotenv

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .envファイルを読み込み
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

import sqlite3

from src.hydrus_client import HydrusClient

# ログ・進捗ファイル
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "reprocess_sensitive_progress.json"


# =============================================================================
# 進捗管理
# =============================================================================

def load_progress() -> Dict[str, Any]:
    """進捗ファイルを読み込み"""
    if not PROGRESS_FILE.exists():
        return {'twitter_processed_users': [], 'hydrus_processed_ids': []}
    try:
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'twitter_processed_users': [], 'hydrus_processed_ids': []}


def save_progress(progress: Dict[str, Any]) -> None:
    """進捗ファイルを保存"""
    progress['last_updated'] = datetime.now().isoformat()
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"警告: 進捗ファイルの保存に失敗: {e}")


def clear_progress() -> None:
    """進捗ファイルを削除"""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("進捗ファイルを削除しました")


# =============================================================================
# Phase 1: Pixiv DB更新（x_restrict → sensitive）
# =============================================================================

def update_pixiv_sensitive(db_path: str, dry_run: bool = False) -> Dict[str, int]:
    """Pixivテーブルのsensitiveフラグをx_restrictから一括更新"""
    conn = sqlite3.connect(db_path)
    stats = {'pixiv_works_updated': 0, 'pixiv_log_only_updated': 0}

    # pixiv_works: x_restrictカラムがある
    cursor = conn.execute(
        "SELECT COUNT(*) FROM pixiv_works WHERE x_restrict >= 1 AND (sensitive = 0 OR sensitive IS NULL)"
    )
    count = cursor.fetchone()[0]
    stats['pixiv_works_updated'] = count

    if count > 0:
        if dry_run:
            print(f"  [DRY-RUN] pixiv_works: {count}件をsensitive=Trueに更新予定")
        else:
            conn.execute(
                "UPDATE pixiv_works SET sensitive = 1 WHERE x_restrict >= 1 AND (sensitive = 0 OR sensitive IS NULL)"
            )
            conn.commit()
            print(f"  pixiv_works: {count}件をsensitive=Trueに更新しました")
    else:
        print(f"  pixiv_works: 更新対象なし")

    # pixiv_log_only_works: x_restrictカラムがない場合がある
    try:
        cursor = conn.execute("PRAGMA table_info(pixiv_log_only_works)")
        columns = {row[1] for row in cursor.fetchall()}
        if 'x_restrict' in columns:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM pixiv_log_only_works WHERE x_restrict >= 1 AND (sensitive = 0 OR sensitive IS NULL)"
            )
            count = cursor.fetchone()[0]
            stats['pixiv_log_only_updated'] = count
            if count > 0:
                if dry_run:
                    print(f"  [DRY-RUN] pixiv_log_only_works: {count}件をsensitive=Trueに更新予定")
                else:
                    conn.execute(
                        "UPDATE pixiv_log_only_works SET sensitive = 1 WHERE x_restrict >= 1 AND (sensitive = 0 OR sensitive IS NULL)"
                    )
                    conn.commit()
                    print(f"  pixiv_log_only_works: {count}件をsensitive=Trueに更新しました")
            else:
                print(f"  pixiv_log_only_works: 更新対象なし")
        else:
            print(f"  pixiv_log_only_works: x_restrictカラムなし（スキップ）")
    except Exception as e:
        print(f"  pixiv_log_only_works: テーブル確認エラー（{e}）")

    conn.close()
    return stats


# =============================================================================
# Phase 2: Twitter再フェッチ（gallery-dl → sensitive）
# =============================================================================

def get_twitter_usernames_from_db(db_path: str) -> List[str]:
    """DBに存在するTwitterユーザー名一覧を取得"""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT DISTINCT username FROM all_tweets")
    usernames = [row[0] for row in cursor.fetchall()]
    conn.close()
    return sorted(usernames)


# fetch_sensitive_flags_for_user の特殊戻り値
_ACCOUNT_NOT_FOUND = object()  # アカウント不在確定（処理済みに記録してスキップ）
_TIMEOUT = object()            # タイムアウト（処理済みに記録しない＝次回リトライ）

# アカウント不在確定の gallery-dl エラータイプ
# 実機確認済み: NotFoundError → [[-1, {"error": "NotFoundError", "message": "Requested user could not be found"}]]
_NOT_FOUND_ERROR_TYPES = {'NotFoundError'}


def fetch_sensitive_flags_for_user(username: str, config: dict):
    """
    gallery-dlで指定ユーザーのメタデータを再取得し、tweet_id → sensitive のマッピングを返す

    Returns:
        Dict: 成功時はsensitiveマッピング（空dictの場合もある）
        _ACCOUNT_NOT_FOUND: アカウント不在確定 → 処理済みに記録してスキップ
        _TIMEOUT: タイムアウト → 処理済みに記録しない（次回リトライ）
        {}: エラーや空データ
    """
    wrapper_path = PROJECT_ROOT / 'src' / 'gallery_dl_wrapper.py'
    url = f"https://x.com/{username}/media"

    # Cookie設定
    from src.gallery_dl_cookie_rotator import GalleryDLCookieRotator
    rotator = GalleryDLCookieRotator()
    cookie_file = rotator.get_next_cookie()
    if not cookie_file:
        cookie_file = Path(config.get('twitter', {}).get('cookie_file', 'cookies/x.com_cookies.txt'))

    cmd = [
        sys.executable,
        str(wrapper_path),
        '--cookies', str(cookie_file),
        '-q',   # Quiet
        '-j',   # JSON出力のみ（ダウンロードしない）
        url
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120  # 2分タイムアウト
        )

        stderr_text = result.stderr or ''

        if result.returncode != 0:
            print(f"    gallery-dl エラー (returncode={result.returncode})")
            if stderr_text.strip():
                first_line = stderr_text.strip().split('\n')[0][:200]
                print(f"    stderr: {first_line}")
            return {}

        output = result.stdout.strip()
        if not output or not output.startswith('['):
            # 正常終了だが出力なし → データなし（存在はする）
            return {}

        # gallery-dl はアカウント不在でも returncode=0 で JSON を返す
        # 例: [[-1, {"error": "NotFoundError", "message": "Requested user could not be found"}]]
        try:
            all_items = json.loads(output)
        except json.JSONDecodeError as e:
            print(f"    JSONパースエラー: {e}")
            return {}

        # エラーレスポンスのチェック（タイプ -1）
        for item in all_items:
            if isinstance(item, list) and len(item) >= 2 and item[0] == -1:
                error_info = item[1] if isinstance(item[1], dict) else {}
                error_type = error_info.get('error', '')
                error_msg = error_info.get('message', '')

                if error_type in _NOT_FOUND_ERROR_TYPES:
                    # アカウント不在確定
                    print(f"    アカウント不在（スキップ）: @{username} [{error_type}: {error_msg}]")
                    return _ACCOUNT_NOT_FOUND

                # AuthRequired等はアカウント不在ではない（cookie切れ等）
                print(f"    gallery-dl エラー: @{username} [{error_type}: {error_msg}]")
                return {}

        sensitive_map = {}

        for item in all_items:
            if isinstance(item, list) and len(item) >= 2:
                item_type = item[0]
                item_data = item[1]

                # タイプ2: ツイート情報
                if item_type == 2 and isinstance(item_data, dict):
                    tweet_id = str(item_data.get('tweet_id', ''))
                    sensitive = item_data.get('sensitive', False)
                    sensitive_flags = item_data.get('sensitive_flags', [])
                    if tweet_id:
                        sensitive_map[tweet_id] = bool(sensitive) or bool(sensitive_flags)

                # タイプ3: メディア情報（メディア単位のsensitive_flags）
                elif item_type == 3 and len(item) >= 3:
                    media_data = item[2] if len(item) > 2 else {}
                    if isinstance(media_data, dict):
                        tweet_id = str(media_data.get('tweet_id', ''))
                        media_flags = media_data.get('sensitive_flags', [])
                        if tweet_id and media_flags:
                            sensitive_map[tweet_id] = True

        return sensitive_map

    except subprocess.TimeoutExpired:
        print(f"    gallery-dl タイムアウト（2分） → 次回リトライ: @{username}")
        return _TIMEOUT
    except Exception as e:
        print(f"    エラー: {e}")
        return {}


def update_twitter_sensitive(db_path: str, config: dict, dry_run: bool = False,
                              progress: Optional[Dict] = None,
                              target_usernames: Optional[List[str]] = None) -> Dict[str, int]:
    """Twitterツイートのsensitiveフラグをgallery-dlで再取得して更新"""
    if target_usernames:
        usernames = target_usernames
    else:
        usernames = get_twitter_usernames_from_db(db_path)
    processed_users = set(progress.get('twitter_processed_users', [])) if progress else set()

    # 未処理ユーザーのみ
    remaining = [u for u in usernames if u not in processed_users]
    print(f"  Twitter全ユーザー: {len(usernames)}件")
    print(f"  処理済み: {len(processed_users)}件, 残り: {len(remaining)}件")

    stats = {'users_processed': 0, 'users_not_found': 0, 'tweets_updated': 0, 'tweets_checked': 0}
    conn = sqlite3.connect(db_path)

    try:
        for i, username in enumerate(remaining, 1):
            print(f"  [{i}/{len(remaining)}] @{username} のメタデータ再取得中...")

            sensitive_map = fetch_sensitive_flags_for_user(username, config)

            if sensitive_map is _ACCOUNT_NOT_FOUND:
                # アカウント不在確定 → 処理済みに記録してスキップ
                processed_users.add(username)
                stats['users_processed'] += 1
                stats['users_not_found'] += 1
                if progress is not None and i % 5 == 0:
                    progress['twitter_processed_users'] = list(processed_users)
                    save_progress(progress)
                continue

            if sensitive_map is _TIMEOUT:
                # タイムアウト → 処理済みに記録しない（次回リトライ）
                continue

            if not sensitive_map:
                print(f"    メタデータ取得失敗またはデータなし")
                processed_users.add(username)
                stats['users_processed'] += 1
                if progress is not None and i % 5 == 0:
                    progress['twitter_processed_users'] = list(processed_users)
                    save_progress(progress)
                continue

            stats['tweets_checked'] += len(sensitive_map)

            # sensitiveなツイートのみDB更新
            sensitive_ids = [tid for tid, is_sens in sensitive_map.items() if is_sens]
            if sensitive_ids:
                if dry_run:
                    print(f"    [DRY-RUN] {len(sensitive_ids)}/{len(sensitive_map)}件がセンシティブ")
                else:
                    # バッチ更新（SQLiteのパラメータ上限に注意）
                    batch_size = 900
                    updated_total = 0
                    for batch_start in range(0, len(sensitive_ids), batch_size):
                        batch = sensitive_ids[batch_start:batch_start + batch_size]
                        placeholders = ','.join('?' for _ in batch)

                        for table in ('all_tweets', 'event_tweets', 'log_only_tweets'):
                            try:
                                cursor = conn.execute(
                                    f"UPDATE {table} SET sensitive = 1 WHERE id IN ({placeholders}) AND (sensitive = 0 OR sensitive IS NULL)",
                                    batch
                                )
                                updated_total += cursor.rowcount
                            except Exception:
                                pass  # テーブルが存在しない場合もある

                    conn.commit()
                    stats['tweets_updated'] += updated_total
                    print(f"    {len(sensitive_ids)}/{len(sensitive_map)}件がセンシティブ → {updated_total}行更新")
            else:
                print(f"    {len(sensitive_map)}件チェック → センシティブなし")

            processed_users.add(username)
            stats['users_processed'] += 1

            # 進捗保存（5ユーザーごと）
            if progress is not None and i % 5 == 0:
                progress['twitter_processed_users'] = list(processed_users)
                save_progress(progress)

    except KeyboardInterrupt:
        print("\n  中断されました。進捗を保存します...")
        conn.commit()
        if progress is not None:
            progress['twitter_processed_users'] = list(processed_users)
            save_progress(progress)
        raise

    finally:
        # 最終進捗保存
        if progress is not None:
            progress['twitter_processed_users'] = list(processed_users)
            save_progress(progress)
        conn.close()

    return stats


# =============================================================================
# Phase 3: Hydrus同期（sensitive=True → rating:r-18タグ付与）
# =============================================================================

def calculate_file_hash(file_path: Path) -> str:
    """ファイルのSHA256ハッシュを計算"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(8192), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def resolve_media_path(media_path: str, config: dict) -> Optional[Path]:
    """相対メディアパスを絶対パスに解決"""
    if media_path.startswith('images/'):
        images_base = Path(config.get('media_storage', {}).get('images_path', 'images'))
        return images_base / media_path[7:]
    elif media_path.startswith('videos/'):
        videos_base = Path(config.get('media_storage', {}).get('videos_path', 'videos'))
        return videos_base / media_path[7:]
    else:
        return Path(media_path)


def get_sensitive_records(db_path: str, platform: str = 'all') -> List[Dict[str, Any]]:
    """
    sensitive=Trueのレコードをlocal_media付きで取得

    Args:
        db_path: DBパス
        platform: 'twitter', 'pixiv', or 'all'
    """
    conn = sqlite3.connect(db_path)
    records = []

    if platform in ('twitter', 'all'):
        for table in ('all_tweets', 'event_tweets'):
            try:
                cursor = conn.execute(f"""
                    SELECT id, username, display_name, tweet_text, local_media, sensitive,
                           '{table}' as source_table
                    FROM {table}
                    WHERE sensitive = 1
                      AND local_media IS NOT NULL AND length(local_media) > 2
                """)
                columns = [desc[0] for desc in cursor.description]
                for row in cursor.fetchall():
                    record = dict(zip(columns, row))
                    record['platform'] = 'twitter'
                    try:
                        record['local_media_list'] = json.loads(record['local_media'])
                    except Exception:
                        record['local_media_list'] = []
                    records.append(record)
            except Exception:
                pass

    if platform in ('pixiv', 'all'):
        for table in ('pixiv_works',):
            try:
                cursor = conn.execute(f"""
                    SELECT id, user_id as username, display_name, title as tweet_text,
                           local_media, sensitive, x_restrict,
                           '{table}' as source_table
                    FROM {table}
                    WHERE sensitive = 1
                      AND local_media IS NOT NULL AND length(local_media) > 2
                """)
                columns = [desc[0] for desc in cursor.description]
                for row in cursor.fetchall():
                    record = dict(zip(columns, row))
                    record['platform'] = 'pixiv'
                    try:
                        record['local_media_list'] = json.loads(record['local_media'])
                    except Exception:
                        record['local_media_list'] = []
                    records.append(record)
            except Exception:
                pass

    conn.close()

    # ID重複排除（all_tweetsとevent_tweetsで同じIDが存在する場合）
    seen_ids = {}
    unique_records = []
    for r in records:
        key = (r['platform'], r['id'])
        if key not in seen_ids:
            seen_ids[key] = True
            unique_records.append(r)

    return unique_records


async def sync_hydrus_sensitive_tags(
    config: dict, db_path: str, dry_run: bool = False,
    progress: Optional[Dict] = None, platform: str = 'all'
) -> Dict[str, int]:
    """sensitive=Trueのレコードに対してHydrusのrating:r-18タグを同期"""

    records = get_sensitive_records(db_path, platform=platform)
    processed_ids = set(progress.get('hydrus_processed_ids', [])) if progress else set()

    # 未処理のみ
    remaining = [r for r in records if f"{r['platform']}:{r['id']}" not in processed_ids]
    print(f"  センシティブレコード: {len(records)}件")
    print(f"  処理済み: {len(processed_ids)}件, 残り: {len(remaining)}件")

    if not remaining:
        print("  処理対象なし")
        return {
            'records_checked': 0, 'files_tagged': 0, 'files_skipped': 0,
            'files_already_tagged': 0, 'files_not_found': 0, 'files_not_in_hydrus': 0,
        }

    stats = {
        'records_checked': 0,
        'files_tagged': 0,
        'files_skipped': 0,
        'files_already_tagged': 0,
        'files_not_found': 0,
        'files_not_in_hydrus': 0,
    }

    VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m3u8'}

    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("  エラー: Hydrus連携が無効です")
            return stats
        if not hydrus._session_key:
            print("  エラー: Hydrus APIに接続できません")
            return stats
        print("  Hydrus API接続OK")

        try:
            for i, record in enumerate(remaining, 1):
                record_key = f"{record['platform']}:{record['id']}"
                platform_label = "Twitter" if record['platform'] == 'twitter' else "Pixiv"

                if i % 50 == 1 or i == len(remaining):
                    print(f"\n  [{i}/{len(remaining)}] {platform_label} @{record['username']} ID:{record['id']}")

                for media_path in record['local_media_list']:
                    file_path = resolve_media_path(media_path, config)
                    if file_path is None:
                        continue

                    # 動画スキップ
                    if file_path.suffix.lower() in VIDEO_EXTS:
                        stats['files_skipped'] += 1
                        continue

                    # images/ディレクトリのみ
                    path_str = str(file_path).replace('\\', '/')
                    if 'images/' not in path_str:
                        stats['files_skipped'] += 1
                        continue

                    if not file_path.exists():
                        stats['files_not_found'] += 1
                        continue

                    # SHA256ハッシュ計算
                    file_hash = calculate_file_hash(file_path)

                    # Hydrusでタグを確認
                    existing_tags = await hydrus._get_file_tags(file_hash)

                    if existing_tags is None:
                        # ファイルがHydrusに存在しない
                        stats['files_not_in_hydrus'] += 1
                        continue

                    if 'rating:r-18' in existing_tags:
                        stats['files_already_tagged'] += 1
                        continue

                    # タグ付与
                    if dry_run:
                        stats['files_tagged'] += 1
                    else:
                        success = await hydrus.add_tags(file_hash, ['rating:r-18'], platform=record['platform'])
                        if success:
                            stats['files_tagged'] += 1
                        else:
                            stats['files_skipped'] += 1

                processed_ids.add(record_key)
                stats['records_checked'] += 1

                # 進捗保存（20件ごと）
                if progress is not None and i % 20 == 0:
                    progress['hydrus_processed_ids'] = list(processed_ids)
                    save_progress(progress)

        except KeyboardInterrupt:
            print("\n  中断されました。進捗を保存します...")
            if progress is not None:
                progress['hydrus_processed_ids'] = list(processed_ids)
                save_progress(progress)
            raise

        finally:
            if progress is not None:
                progress['hydrus_processed_ids'] = list(processed_ids)
                save_progress(progress)

    return stats


# =============================================================================
# Phase 3b: URLベースHydrus検索 + on-demand センシティブ判定
# =============================================================================

def fetch_sensitive_for_single_tweet(tweet_id: str, username: str, config: dict) -> Optional[bool]:
    """単一ツイートのセンシティブ判定をgallery-dlで取得
    
    Returns:
        True/False: センシティブ判定結果
        None: 取得失敗
    """
    wrapper_path = PROJECT_ROOT / 'src' / 'gallery_dl_wrapper.py'
    url = f"https://x.com/{username}/status/{tweet_id}"

    from src.gallery_dl_cookie_rotator import GalleryDLCookieRotator
    rotator = GalleryDLCookieRotator()
    cookie_file = rotator.get_next_cookie()
    if not cookie_file:
        cookie_file = Path(config.get('twitter', {}).get('cookie_file', 'cookies/x.com_cookies.txt'))

    cmd = [
        sys.executable,
        str(wrapper_path),
        '--cookies', str(cookie_file),
        '-q', '-j',
        url
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            return None

        output = result.stdout.strip()
        if not output or not output.startswith('['):
            return None

        items = json.loads(output)

        for item in items:
            if isinstance(item, list) and len(item) >= 2:
                item_type = item[0]
                item_data = item[1]

                if item_type == 2 and isinstance(item_data, dict):
                    sensitive = item_data.get('sensitive', False)
                    sensitive_flags = item_data.get('sensitive_flags', [])
                    return bool(sensitive) or bool(sensitive_flags)

                # メディア情報のsensitive_flags
                if item_type == 3 and len(item) >= 3:
                    media_data = item[2] if isinstance(item[2], dict) else {}
                    if media_data.get('sensitive_flags'):
                        return True

        return False

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"    gallery-dl単一ツイート取得エラー: {e}")
        return None


def get_all_twitter_records_with_url(db_path: str, target_username: Optional[str] = None) -> List[Dict[str, Any]]:
    """DBから全Twitterレコード（sensitive問わず）をURL付きで取得
    
    Args:
        db_path: DBパス
        target_username: 特定ユーザーのみ処理する場合に指定
    """
    conn = sqlite3.connect(db_path)
    records = []

    where_clause = ""
    params = ()
    if target_username:
        where_clause = "AND username = ?"
        params = (target_username,)

    for table in ('all_tweets', 'event_tweets'):
        try:
            cursor = conn.execute(f"""
                SELECT id, username, display_name, tweet_text, sensitive,
                       '{table}' as source_table
                FROM {table}
                WHERE 1=1 {where_clause}
            """, params)
            columns = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                record = dict(zip(columns, row))
                record['platform'] = 'twitter'
                # ツイートURLを構築
                record['tweet_url'] = f"https://x.com/{record['username']}/status/{record['id']}"
                records.append(record)
        except Exception:
            pass

    conn.close()

    # ID重複排除
    seen_ids = {}
    unique_records = []
    for r in records:
        if r['id'] not in seen_ids:
            seen_ids[r['id']] = True
            unique_records.append(r)

    return unique_records


async def sync_hydrus_by_url_search(
    config: dict, db_path: str, dry_run: bool = False,
    progress: Optional[Dict] = None, target_username: Optional[str] = None
) -> Dict[str, int]:
    """URLベースでHydrusを検索し、センシティブなツイートにrating:r-18タグを付与
    
    既存のフローと異なり、ローカルファイルの存在を必要としない。
    Hydrusに保存されたknown URLでファイルを検索し、
    gallery-dlでセンシティブ判定を再取得する。
    """
    records = get_all_twitter_records_with_url(db_path, target_username)
    processed_ids = set(progress.get('url_search_processed_ids', [])) if progress else set()

    remaining = [r for r in records if r['id'] not in processed_ids]
    print(f"  URLベース検索対象: {len(records)}件")
    print(f"  処理済み: {len(processed_ids)}件, 残り: {len(remaining)}件")

    if not remaining:
        print("  処理対象なし")
        return {
            'records_checked': 0, 'files_found_in_hydrus': 0,
            'files_tagged': 0, 'files_already_tagged': 0,
            'files_not_in_hydrus': 0, 'sensitive_detected': 0,
            'not_sensitive': 0, 'fetch_failed': 0, 'db_updated': 0,
        }

    stats = {
        'records_checked': 0,
        'files_found_in_hydrus': 0,
        'files_tagged': 0,
        'files_already_tagged': 0,
        'files_not_in_hydrus': 0,
        'sensitive_detected': 0,
        'not_sensitive': 0,
        'fetch_failed': 0,
        'db_updated': 0,
    }

    conn = sqlite3.connect(db_path)

    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("  エラー: Hydrus連携が無効です")
            conn.close()
            return stats
        if not hydrus._session_key:
            print("  エラー: Hydrus APIに接続できません")
            conn.close()
            return stats
        print("  Hydrus API接続OK")

        try:
            for i, record in enumerate(remaining, 1):
                if i % 50 == 1 or i == len(remaining):
                    print(f"\n  [{i}/{len(remaining)}] @{record['username']} ID:{record['id']}")

                tweet_url = record['tweet_url']

                # HydrusでURL検索
                file_hashes = await hydrus.search_files_by_url(tweet_url)

                if not file_hashes:
                    stats['files_not_in_hydrus'] += 1
                    processed_ids.add(record['id'])
                    stats['records_checked'] += 1

                    # 進捗保存（100件ごと）
                    if progress is not None and i % 100 == 0:
                        progress['url_search_processed_ids'] = list(processed_ids)
                        save_progress(progress)
                    continue

                stats['files_found_in_hydrus'] += len(file_hashes)

                # DBでsensitive=1の場合はそのまま使用
                is_sensitive = bool(record.get('sensitive'))

                # sensitive未判定の場合、gallery-dlで再取得
                if not is_sensitive:
                    result = fetch_sensitive_for_single_tweet(
                        record['id'], record['username'], config
                    )
                    if result is None:
                        stats['fetch_failed'] += 1
                        processed_ids.add(record['id'])
                        stats['records_checked'] += 1
                        if progress is not None and i % 100 == 0:
                            progress['url_search_processed_ids'] = list(processed_ids)
                            save_progress(progress)
                        continue
                    is_sensitive = result

                    # DB更新（sensitive=1に）
                    if is_sensitive and not dry_run:
                        for table in ('all_tweets', 'event_tweets', 'log_only_tweets'):
                            try:
                                conn.execute(
                                    f"UPDATE {table} SET sensitive = 1 WHERE id = ? AND (sensitive = 0 OR sensitive IS NULL)",
                                    (record['id'],)
                                )
                            except Exception:
                                pass
                        conn.commit()
                        stats['db_updated'] += 1

                if is_sensitive:
                    stats['sensitive_detected'] += 1

                    # 各ファイルにタグ付与
                    for file_hash in file_hashes:
                        existing_tags = await hydrus._get_file_tags(file_hash)
                        if existing_tags is None:
                            continue

                        if 'rating:r-18' in existing_tags:
                            stats['files_already_tagged'] += 1
                            continue

                        if dry_run:
                            stats['files_tagged'] += 1
                            if i <= 5:
                                print(f"    [DRY-RUN] タグ付与: {file_hash[:16]}... <- rating:r-18")
                        else:
                            success = await hydrus.add_tags(
                                file_hash, ['rating:r-18'],
                                platform='twitter'
                            )
                            if success:
                                stats['files_tagged'] += 1
                            else:
                                print(f"    タグ付与失敗: {file_hash[:16]}...")
                else:
                    stats['not_sensitive'] += 1

                processed_ids.add(record['id'])
                stats['records_checked'] += 1

                # 進捗保存（100件ごと）
                if progress is not None and i % 100 == 0:
                    progress['url_search_processed_ids'] = list(processed_ids)
                    save_progress(progress)

        except KeyboardInterrupt:
            print("\n  中断されました。進捗を保存します...")
            conn.commit()
            if progress is not None:
                progress['url_search_processed_ids'] = list(processed_ids)
                save_progress(progress)
            raise

        finally:
            if progress is not None:
                progress['url_search_processed_ids'] = list(processed_ids)
                save_progress(progress)
            conn.close()

    return stats


# =============================================================================
# メイン
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description='センシティブフラグ再処理スクリプト',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python scripts/fix/reprocess_sensitive.py --all              全フェーズ実行
  python scripts/fix/reprocess_sensitive.py --pixiv-db         Pixiv DB更新のみ
  python scripts/fix/reprocess_sensitive.py --twitter-refetch  Twitter再フェッチのみ
  python scripts/fix/reprocess_sensitive.py --hydrus-sync      Hydrusタグ同期のみ
  python scripts/fix/reprocess_sensitive.py --all --dry-run    変更せずに確認
  python scripts/fix/reprocess_sensitive.py --hydrus-sync --platform pixiv  Pixivのみ同期
        """
    )
    parser.add_argument('--all', action='store_true', help='全フェーズを実行')
    parser.add_argument('--pixiv-db', action='store_true', help='Phase 1: Pixiv DB更新（x_restrict → sensitive）')
    parser.add_argument('--twitter-refetch', action='store_true', help='Phase 2: Twitter gallery-dl再フェッチ')
    parser.add_argument('--hydrus-sync', action='store_true', help='Phase 3: Hydrus rating:r-18タグ同期')
    parser.add_argument('--url-search', action='store_true',
                        help='Phase 3b: URLベースのHydrus検索でR-18タグを付与（ローカルファイル不要）')
    parser.add_argument('--dry-run', action='store_true', help='変更せずに確認のみ')
    parser.add_argument('--reset', action='store_true', help='進捗をリセット')
    parser.add_argument('--platform', choices=['twitter', 'pixiv', 'all'], default='all',
                        help='Hydrus同期の対象プラットフォーム')
    parser.add_argument('--username', nargs='*',
                        help='Twitter再フェッチ / URLベース検索の対象ユーザー（複数指定可、省略時は全ユーザー）')
    args = parser.parse_args()

    # 何も指定されていない場合はヘルプ表示
    if not any([args.all, args.pixiv_db, args.twitter_refetch, args.hydrus_sync, args.url_search]):
        parser.print_help()
        return

    if args.reset:
        clear_progress()

    # 設定読み込み
    with open('config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    db_path = 'data/eventmonitor.db'

    # 進捗管理（dry-runでは進捗を保存しない）
    progress = load_progress() if not args.dry_run else {'twitter_processed_users': [], 'hydrus_processed_ids': [], 'url_search_processed_ids': []}

    print("=" * 60)
    print("センシティブフラグ再処理スクリプト")
    if args.dry_run:
        print("  *** DRY-RUN モード（変更は行いません） ***")
    print("=" * 60)

    # Phase 1: Pixiv DB更新
    if args.all or args.pixiv_db:
        print(f"\n--- Phase 1: Pixiv DB更新（x_restrict → sensitive） ---")
        pixiv_stats = update_pixiv_sensitive(db_path, dry_run=args.dry_run)
        print(f"  完了: pixiv_works={pixiv_stats['pixiv_works_updated']}件, "
              f"pixiv_log_only={pixiv_stats['pixiv_log_only_updated']}件")

    # Phase 2: Twitter再フェッチ
    if args.all or args.twitter_refetch:
        print(f"\n--- Phase 2: Twitter gallery-dl再フェッチ ---")
        try:
            twitter_stats = update_twitter_sensitive(
                db_path, config, dry_run=args.dry_run, progress=progress,
                target_usernames=args.username
            )
            print(f"  完了: ユーザー={twitter_stats['users_processed']}件, "
                  f"アカウント不在={twitter_stats['users_not_found']}件, "
                  f"ツイートチェック={twitter_stats['tweets_checked']}件, "
                  f"DB更新={twitter_stats['tweets_updated']}件")
        except KeyboardInterrupt:
            print("  Phase 2 中断")

    # Phase 3: Hydrus同期
    if args.all or args.hydrus_sync:
        print(f"\n--- Phase 3: Hydrus rating:r-18 タグ同期 ---")
        try:
            hydrus_stats = await sync_hydrus_sensitive_tags(
                config, db_path, dry_run=args.dry_run,
                progress=progress, platform=args.platform
            )
            prefix = "[DRY-RUN] " if args.dry_run else ""
            print(f"\n  {prefix}完了:")
            print(f"    レコード処理: {hydrus_stats['records_checked']}件")
            print(f"    タグ付与: {hydrus_stats['files_tagged']}ファイル")
            print(f"    既にタグ済み: {hydrus_stats['files_already_tagged']}ファイル")
            print(f"    ファイルなし: {hydrus_stats['files_not_found']}ファイル")
            print(f"    Hydrusに未登録: {hydrus_stats['files_not_in_hydrus']}ファイル")
            print(f"    スキップ: {hydrus_stats['files_skipped']}ファイル")
        except KeyboardInterrupt:
            print("  Phase 3 中断")

    # Phase 3b: URLベースHydrus検索
    if args.url_search or (args.all and args.url_search):
        print(f"\n--- Phase 3b: URLベースHydrus検索 + センシティブ判定 ---")
        target_user = args.username[0] if args.username else None
        try:
            url_stats = await sync_hydrus_by_url_search(
                config, db_path, dry_run=args.dry_run,
                progress=progress, target_username=target_user
            )
            prefix = "[DRY-RUN] " if args.dry_run else ""
            print(f"\n  {prefix}完了:")
            print(f"    レコード処理: {url_stats['records_checked']}件")
            print(f"    Hydrusで発見: {url_stats['files_found_in_hydrus']}ファイル")
            print(f"    タグ付与: {url_stats['files_tagged']}ファイル")
            print(f"    既にタグ済み: {url_stats['files_already_tagged']}ファイル")
            print(f"    Hydrusに未登録: {url_stats['files_not_in_hydrus']}件")
            print(f"    センシティブ検出: {url_stats['sensitive_detected']}件")
            print(f"    非センシティブ: {url_stats['not_sensitive']}件")
            print(f"    判定取得失敗: {url_stats['fetch_failed']}件")
            print(f"    DB更新: {url_stats['db_updated']}件")
        except KeyboardInterrupt:
            print("  Phase 3b 中断")

    print(f"\n{'=' * 60}")
    print("全処理完了")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    asyncio.run(main())
