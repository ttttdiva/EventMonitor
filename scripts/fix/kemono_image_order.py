#!/usr/bin/env python3
"""
Kemono作品のHydrusインポート順序を修正するスクリプト (v3)

問題:
    gallery-dlのKemonoエクストラクターは attachments→file の順で出力するため、
    file（カバー/1枚目）が最後にインポートされていた。
    正しい表示順は file→attachments（カバー画像が先頭）。

v2の問題:
    DBのmedia_urlsとlocal_mediaが別々の順序で保存されていたため、
    zipペアリング+{num}による並べ替えが壊れていた。

v3の修正:
    Kemono APIから直接 file/attachments の正しい順序を取得し、
    CDNハッシュでHydrusファイルとマッチングする。

使用方法:
    python scripts/fix/kemono_image_order.py --dry-run
    python scripts/fix/kemono_image_order.py
    python scripts/fix/kemono_image_order.py --username fanbox/3316400
    python scripts/fix/kemono_image_order.py --limit 100
    python scripts/fix/kemono_image_order.py --update-db

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
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set, Tuple
import yaml
from dotenv import load_dotenv

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .envファイルを読み込み
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

# Kemono CDN URLからSHA256ハッシュを抽出する正規表現
_KEMONO_HASH_RE = re.compile(r'/([0-9a-f]{64})\.', re.IGNORECASE)
# work_urlからservice/user_id/post_idを抽出
_KEMONO_URL_RE = re.compile(
    r'https://kemono\.cr/(\w+)/user/(\w+)/post/(\w+)'
)

# 進捗管理
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "fix_kemono_image_order_v3_progress.json"


def extract_hash_from_url(url: str) -> Optional[str]:
    """Kemono CDN URLまたはパスからSHA256ハッシュを抽出"""
    match = _KEMONO_HASH_RE.search(url)
    return match.group(1).lower() if match else None


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
            print(f"  [WARN] progress load failed: {e}")
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
            print(f"  [WARN] progress save failed: {e}")

    def is_processed(self, work_id: str) -> bool:
        return work_id in self.processed_ids or work_id in self.buffer


class KemonoOrderResolver:
    """
    Kemono APIから正しいファイル順序(file→attachments)のCDNハッシュを取得。
    ユーザー単位でバッチ取得しキャッシュする。
    """

    _API_BASE = "https://kemono.cr/api"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36"
        ),
        "Accept": "text/css",
    }
    _PAGE_SIZE = 50
    _REQUEST_INTERVAL = 0.4  # API rate limit (秒)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self._HEADERS)
        # cache: work_id ("fanbox_12345") -> [hash, hash, ...] 正しい順序
        self._cache: Dict[str, List[str]] = {}
        # 全投稿フェッチ済みユーザー
        self._user_fetched: Set[str] = set()

    def get_correct_hash_order(self, work_id: str, work_url: str) -> Optional[List[str]]:
        """
        作品の正しいCDNハッシュ順序を返す (file→attachments)

        Returns:
            [hash1, hash2, ...] 正しい順序。取得失敗時はNone。
        """
        if work_id in self._cache:
            return self._cache[work_id]

        m = _KEMONO_URL_RE.match(work_url)
        if not m:
            return None

        service, user_id, post_id = m.groups()
        user_key = f"{service}/{user_id}"

        # まだフェッチしていないユーザーならバッチ取得
        if user_key not in self._user_fetched:
            self._fetch_user_posts(service, user_id)
            self._user_fetched.add(user_key)

        # バッチで見つからなかった場合、per-post APIでフォールバック
        if work_id not in self._cache:
            self._fetch_single_post(service, user_id, post_id)

        return self._cache.get(work_id)

    def _extract_post_hashes(self, post: Dict, service: str) -> Tuple[str, List[str]]:
        """投稿データからwork_idと正しいハッシュ順序を抽出"""
        post_id = str(post.get('id', ''))
        work_id = f"{service}_{post_id}"

        hashes = []
        # file first (カバー/1枚目)
        file_info = post.get('file', {})
        if isinstance(file_info, dict):
            path = file_info.get('path') or file_info.get('url', '')
            h = extract_hash_from_url(path)
            if h:
                hashes.append(h)
        # attachments after (2枚目以降)
        for att in post.get('attachments', []):
            if isinstance(att, dict):
                path = att.get('path') or att.get('url', '')
                h = extract_hash_from_url(path)
                if h:
                    hashes.append(h)

        return work_id, hashes

    def _fetch_user_posts(self, service: str, user_id: str):
        """ユーザーの全投稿をKemono APIから取得してキャッシュ"""
        offset = 0
        total = 0
        while True:
            url = (
                f"{self._API_BASE}/v1/{service}/user/{user_id}"
                f"/posts?o={offset}"
            )
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code != 200:
                    print(
                        f"  [API] {service}/{user_id} posts "
                        f"offset={offset}: HTTP {resp.status_code}"
                    )
                    break

                posts = resp.json()
                if not posts:
                    break

                for post in posts:
                    wid, hashes = self._extract_post_hashes(post, service)
                    if hashes:
                        self._cache[wid] = hashes

                total += len(posts)
                if len(posts) < self._PAGE_SIZE:
                    break
                offset += self._PAGE_SIZE
                time.sleep(self._REQUEST_INTERVAL)

            except Exception as e:
                print(f"  [API] fetch error {service}/{user_id}: {e}")
                break

        if total > 0:
            print(f"  [API] cached {total} posts for {service}/{user_id}")

    def _fetch_single_post(self, service: str, user_id: str, post_id: str):
        """単一投稿をKemono APIから取得"""
        url = (
            f"{self._API_BASE}/v1/{service}/user/{user_id}"
            f"/post/{post_id}"
        )
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code != 200:
                print(
                    f"  [API] post {service}/{user_id}/{post_id}: "
                    f"HTTP {resp.status_code}"
                )
                return

            data = resp.json()
            # per-post APIは {"post": {...}} 形式で返す場合がある
            post = data.get('post', data) if isinstance(data, dict) else data
            if isinstance(post, dict):
                wid, hashes = self._extract_post_hashes(post, service)
                if hashes:
                    self._cache[wid] = hashes
            time.sleep(self._REQUEST_INTERVAL)

        except Exception as e:
            print(f"  [API] single post error: {e}")


def get_kemono_works(
    db_path: str,
    limit: Optional[int] = None,
    skip_ids: Optional[Set[str]] = None,
    user_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """kemono_works + kemono_log_only_works から複数画像レコードをwork_date昇順で取得"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()

    table_configs = {
        'kemono_works': {
            'select': "id, user_id, display_name, title, work_date, work_url, "
                      "media_urls, local_media, file_count",
            'where_extra': "AND file_count > 1",
        },
        'kemono_log_only_works': {
            'select': "id, user_id, display_name, title, work_date, work_url, "
                      "media_urls",
            'where_extra': "",
        },
    }

    all_records = []
    for table, tcfg in table_configs.items():
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        if not cursor.fetchone():
            continue

        query = f"""
            SELECT {tcfg['select']}
            FROM {table}
            WHERE media_urls IS NOT NULL AND length(media_urls) > 2
            {tcfg['where_extra']}
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

        skipped = 0
        for row in rows:
            record = dict(zip(columns, row))
            record['_table'] = table

            if skip_ids and record['id'] in skip_ids:
                skipped += 1
                continue

            try:
                record['media_urls_list'] = json.loads(record['media_urls'])
            except Exception:
                record['media_urls_list'] = []

            if 'file_count' not in record:
                record['file_count'] = len(record['media_urls_list'])

            if len(record['media_urls_list']) < 2:
                continue

            all_records.append(record)

        if skipped > 0:
            print(f"  skip {skipped} already-processed works in {table}")

    conn.close()

    all_records.sort(key=lambda r: r.get('work_date', ''))
    return all_records


def parse_work_date(work_date_str: str) -> float:
    """work_date文字列をUnixタイムスタンプに変換"""
    if isinstance(work_date_str, datetime):
        dt = work_date_str
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.strptime(work_date_str, fmt)
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

    raise ValueError(f"work_date parse failed: {work_date_str}")


def update_db_media_urls(
    db_path: str, work_id: str, table: str,
    correct_hashes: List[str], current_urls: List[str],
) -> bool:
    """DBのmedia_urlsを正しい順序に更新（CDNハッシュで照合）"""
    # 現在のURLをハッシュ→URL辞書に変換
    hash_to_url: Dict[str, str] = {}
    for url in current_urls:
        h = extract_hash_from_url(url)
        if h:
            hash_to_url[h] = url

    # correct_hashesの順序でURLを並べ替え
    new_urls = []
    for h in correct_hashes:
        if h in hash_to_url:
            new_urls.append(hash_to_url[h])
    # APIに存在しないURLも末尾に追加（データ欠損防止）
    used = set(new_urls)
    for url in current_urls:
        if url not in used:
            new_urls.append(url)

    if new_urls == current_urls:
        return False

    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            f"UPDATE {table} SET media_urls = ? WHERE id = ?",
            (json.dumps(new_urls), work_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"  DB update error: {e}")
        return False


async def process_work(
    hydrus: HydrusClient,
    work: Dict[str, Any],
    file_service_key: str,
    resolver: KemonoOrderResolver,
    dry_run: bool = False,
    do_update_db: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """1件のKemono作品のインポート時刻を修正"""
    result = {
        'files_found': 0,
        'files_reordered': 0,
        'files_not_found': 0,
        'api_miss': 0,
        'already_correct': 0,
        'errors': 0,
        'db_updated': 0,
    }

    work_id = work['id']
    work_url = work['work_url']
    media_urls = work['media_urls_list']

    # work_dateをタイムスタンプに変換
    try:
        base_timestamp = parse_work_date(work['work_date'])
    except ValueError as e:
        print(f"  ERROR: {e}")
        result['errors'] += 1
        return result

    # Kemono APIから正しいハッシュ順序を取得
    correct_hashes = resolver.get_correct_hash_order(work_id, work_url)
    if not correct_hashes or len(correct_hashes) < 2:
        result['api_miss'] += 1
        return result

    # DB更新
    if do_update_db and db_path and not dry_run:
        table = work.get('_table', 'kemono_works')
        if update_db_media_urls(db_path, work_id, table, correct_hashes, media_urls):
            result['db_updated'] = 1

    # URLでHydrus内のファイルを検索
    hydrus_hashes = await hydrus.search_files_by_url(work_url)
    if not hydrus_hashes:
        result['files_not_found'] += 1
        return result

    result['files_found'] = len(hydrus_hashes)
    hydrus_hash_set = {h.lower() for h in hydrus_hashes}

    # 正しい順序でインポート時刻を設定
    matched = 0
    for position, expected_hash in enumerate(correct_hashes):
        if expected_hash not in hydrus_hash_set:
            continue

        timestamp = base_timestamp + position
        dt_display = datetime.fromtimestamp(
            timestamp, tz=timezone.utc
        ).strftime('%Y-%m-%d %H:%M:%S')

        if dry_run:
            label = "file(cover)" if position == 0 else f"att[{position}]"
            print(
                f"  [DRY-RUN] [{position}] {expected_hash[:16]}... "
                f"-> {dt_display} UTC  ({label})"
            )
            result['files_reordered'] += 1
        else:
            success = await hydrus.set_file_import_time(
                expected_hash, timestamp, file_service_key
            )
            if success:
                result['files_reordered'] += 1
            else:
                result['errors'] += 1

        matched += 1
        await asyncio.sleep(0.05)

    if matched == 0:
        result['files_not_found'] += 1

    return result


async def main():
    parser = argparse.ArgumentParser(
        description='Kemono作品のHydrusインポート順序を修正 (v3: Kemono API照合)'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='dry-run mode')
    parser.add_argument('--limit', type=int,
                        help='max works to process')
    parser.add_argument('--username', nargs='*',
                        help='user_id filter (e.g. fanbox/3316400)')
    parser.add_argument('--update-db', action='store_true',
                        help='DBのmedia_urlsも正しい順序に更新')
    parser.add_argument('--reset', action='store_true',
                        help='reset progress')
    args = parser.parse_args()

    print("=" * 60)
    print("Kemono Hydrus import order fix v3 (Kemono API)")
    if args.dry_run:
        print("[DRY-RUN]")
    if args.update_db:
        print("[UPDATE-DB]")
    print("=" * 60)

    # reset
    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("progress reset")

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

    # progress
    progress = ProgressManager()

    # kemono works (両テーブル)
    works = get_kemono_works(
        db_path,
        limit=args.limit,
        skip_ids=progress.processed_ids,
        user_ids=args.username,
    )

    if not works:
        print("no works to process")
        return

    print(f"target: {len(works)} kemono works (2+ files, both tables)")
    print()

    # Kemono API resolver
    resolver = KemonoOrderResolver()

    # Hydrus
    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("ERROR: Hydrus disabled")
            return

        if not hydrus._session_key:
            print("ERROR: Hydrus API connection failed")
            return

        print("Hydrus API OK")

        file_service_key = await hydrus.get_file_service_key()
        if not file_service_key:
            print("ERROR: file service key unavailable")
            print("  -> Hydrus API key needs 'Edit Times' permission")
            return

        print(f"file service key: {file_service_key[:16]}...")
        print()

        # stats
        total_found = 0
        total_reordered = 0
        total_not_found = 0
        total_api_miss = 0
        total_correct = 0
        total_errors = 0
        total_db_updated = 0

        try:
            for idx, work in enumerate(works, 1):
                work_id = work['id']
                title = (work.get('title') or '')[:40]
                display_name = work.get('display_name', '')
                file_count = work.get('file_count', 0)
                table_label = (
                    'log' if work.get('_table', '').endswith('log_only_works')
                    else 'mon'
                )
                work_date_str = (
                    str(work['work_date'])[:10] if work.get('work_date')
                    else '?'
                )

                print(
                    f"[{idx}/{len(works)}] [{table_label}] {work_id} "
                    f"@{display_name} \"{title}\" "
                    f"({file_count}files, {work_date_str})"
                )

                r = await process_work(
                    hydrus, work, file_service_key, resolver,
                    args.dry_run,
                    do_update_db=args.update_db,
                    db_path=db_path,
                )

                total_found += r['files_found']
                total_reordered += r['files_reordered']
                total_not_found += r['files_not_found']
                total_api_miss += r['api_miss']
                total_correct += r['already_correct']
                total_errors += r['errors']
                total_db_updated += r.get('db_updated', 0)

                if r['files_found'] > 0:
                    msg = (
                        f"  found {r['files_found']}, "
                        f"reordered {r['files_reordered']}"
                    )
                    if r.get('db_updated'):
                        msg += " (DB updated)"
                    print(msg)
                elif r['api_miss'] > 0:
                    print(f"  API: hash not found (skip)")
                elif r['files_not_found'] > 0:
                    print(f"  not in Hydrus (skip)")
                elif r['already_correct'] > 0:
                    print(f"  already correct (skip)")

                if r['errors'] > 0:
                    print(f"  errors: {r['errors']}")

                if not args.dry_run:
                    progress.add(work_id)

                if idx % 50 == 0:
                    print(f"\n=== progress: {idx}/{len(works)} ===")
                    print(
                        f"  reordered: {total_reordered}, "
                        f"not_found: {total_not_found}, "
                        f"api_miss: {total_api_miss}, "
                        f"errors: {total_errors}"
                    )
                    print()

        except KeyboardInterrupt:
            print("\n\ninterrupted, saving progress...")
        finally:
            if not args.dry_run:
                progress.flush()

    # summary
    print()
    print("=" * 60)
    print("done!")
    print(f"  files found in Hydrus: {total_found}")
    print(f"  reordered:             {total_reordered}")
    print(f"  not in Hydrus:         {total_not_found}")
    print(f"  API miss:              {total_api_miss}")
    print(f"  already correct:       {total_correct}")
    print(f"  DB updated:            {total_db_updated}")
    print(f"  errors:                {total_errors}")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
