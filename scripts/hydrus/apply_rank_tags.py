#!/usr/bin/env python3
"""
Hydrus ランクタグ一括付与スクリプト

monitored_accounts.csv のランク設定を参照し、
既にHydrusにインポート済みの全画像に rank:N タグを付与する。

使用方法:
    python scripts/hydrus/apply_rank_tags.py
    python scripts/hydrus/apply_rank_tags.py --dry-run
    python scripts/hydrus/apply_rank_tags.py --platform pixiv
    python scripts/hydrus/apply_rank_tags.py --reset
"""

import sys
import os
import asyncio
import argparse
import csv
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "hydrus_rank_tags_progress.json"

# プラットフォーム別テーブル定義
# (テーブル名, user_idカラム名, URLカラム名, platform識別子)
PLATFORM_TABLES = [
    ("all_tweets", "username", "tweet_url", "twitter"),
    ("pixiv_works", "user_id", "work_url", "pixiv"),
    ("pixiv_log_only_works", "user_id", "work_url", "pixiv"),
    ("kemono_works", "user_id", "work_url", "kemono"),
    ("kemono_log_only_works", "user_id", "work_url", "kemono"),
    ("tinami_works", "user_id", "work_url", "tinami"),
    ("tinami_log_only_works", "user_id", "work_url", "tinami"),
    ("poipiku_works", "user_id", "work_url", "poipiku"),
    ("poipiku_log_only_works", "user_id", "work_url", "poipiku"),
    ("fantia_works", "user_id", "work_url", "fantia"),
    ("fantia_log_only_works", "user_id", "work_url", "fantia"),
    ("nijie_works", "user_id", "work_url", "nijie"),
    ("nijie_log_only_works", "user_id", "work_url", "nijie"),
    ("skeb_works", "user_id", "work_url", "skeb"),
    ("skeb_log_only_works", "user_id", "work_url", "skeb"),
    ("misskey_works", "user_id", "work_url", "misskey"),
    ("misskey_log_only_works", "user_id", "work_url", "misskey"),
    ("gelbooru_works", "user_id", "work_url", "gelbooru"),
    ("gelbooru_log_only_works", "user_id", "work_url", "gelbooru"),
    ("fanbox_works", "user_id", "work_url", "fanbox"),
    ("fanbox_log_only_works", "user_id", "work_url", "fanbox"),
]

DEFAULT_RANK = 3


# =============================================================================
# 進捗管理
# =============================================================================

def load_progress() -> Dict:
    if not PROGRESS_FILE.exists():
        return {"processed": {}}
    try:
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"processed": {}}


def save_progress(progress: Dict) -> None:
    progress['last_updated'] = datetime.now().isoformat()
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  警告: 進捗保存失敗: {e}")


# =============================================================================
# CSV読み込み
# =============================================================================

def load_rank_map(csv_path: str) -> Dict[str, Dict[str, int]]:
    """CSVからplatform:username → rank のマッピングを作成"""
    rank_map: Dict[str, Dict[str, int]] = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = row.get('username', '').strip()
            if not username:
                continue
            platform = (row.get('platform') or '').strip() or 'twitter'
            rank_raw = (row.get('rank') or '').strip()
            rank = int(rank_raw) if rank_raw in ('1', '2', '3') else DEFAULT_RANK

            if platform not in rank_map:
                rank_map[platform] = {}
            rank_map[platform][username] = rank

    return rank_map


# =============================================================================
# メイン処理
# =============================================================================

async def apply_rank_tags(
    config: dict,
    db_path: str,
    rank_map: Dict[str, Dict[str, int]],
    target_platform: Optional[str],
    dry_run: bool,
    progress: Dict,
) -> Dict[str, int]:
    stats = {
        'tables_processed': 0,
        'records_checked': 0,
        'urls_searched': 0,
        'files_found': 0,
        'tags_added': 0,
        'already_done': 0,
        'not_in_hydrus': 0,
        'errors': 0,
    }

    conn = sqlite3.connect(db_path)
    processed_set: Set[str] = set(progress.get("processed", {}).keys())

    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("  エラー: Hydrus連携が無効です")
            conn.close()
            return stats

        for table_name, user_col, url_col, platform in PLATFORM_TABLES:
            if target_platform and platform != target_platform:
                continue

            # テーブル存在チェック
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            if not cursor.fetchone():
                continue

            platform_ranks = rank_map.get(platform, {})
            if not platform_ranks:
                continue

            # レコード取得
            try:
                cursor = conn.execute(
                    f"SELECT id, {user_col}, {url_col} FROM {table_name}"
                )
                records = cursor.fetchall()
            except Exception as e:
                print(f"  警告: {table_name} の読み込み失敗: {e}")
                continue

            if not records:
                continue

            stats['tables_processed'] += 1
            print(f"\n  [{platform}] {table_name}: {len(records)}件")

            for record_id, user_id, url in records:
                stats['records_checked'] += 1
                if not url:
                    continue

                # 進捗チェック（テーブル名+IDで一意）
                progress_key = f"{table_name}:{record_id}"
                if progress_key in processed_set:
                    stats['already_done'] += 1
                    continue

                rank = platform_ranks.get(user_id, DEFAULT_RANK)
                tag = f"rank:{rank}"

                # Hydrus URL検索
                try:
                    stats['urls_searched'] += 1
                    file_hashes = await hydrus.search_files_by_url(url)
                except Exception as e:
                    stats['errors'] += 1
                    if stats['errors'] <= 5:
                        print(f"    エラー: URL検索失敗 {url}: {e}")
                    continue

                if not file_hashes:
                    stats['not_in_hydrus'] += 1
                    # Hydrusにない = 進捗に記録して次回スキップ
                    if not dry_run:
                        progress.setdefault("processed", {})[progress_key] = "not_found"
                    continue

                stats['files_found'] += len(file_hashes)

                for file_hash in file_hashes:
                    if dry_run:
                        stats['tags_added'] += 1
                        if stats['tags_added'] <= 10:
                            print(f"    [DRY-RUN] {file_hash[:16]}... <- {tag} ({user_id})")
                    else:
                        try:
                            success = await hydrus.add_tags(file_hash, [tag], platform=platform)
                            if success:
                                stats['tags_added'] += 1
                            else:
                                stats['errors'] += 1
                        except Exception as e:
                            stats['errors'] += 1
                            if stats['errors'] <= 5:
                                print(f"    エラー: タグ付与失敗: {e}")

                if not dry_run:
                    progress.setdefault("processed", {})[progress_key] = tag

                # 定期的に進捗保存
                if not dry_run and stats['urls_searched'] % 100 == 0:
                    save_progress(progress)
                    print(f"    ... {stats['urls_searched']}件検索済み / {stats['tags_added']}件タグ付与")

    conn.close()
    return stats


# =============================================================================
# エントリポイント
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Hydrus ランクタグ一括付与")
    parser.add_argument("--dry-run", action="store_true", help="変更を行わず確認のみ")
    parser.add_argument("--platform", type=str, help="対象プラットフォーム（例: pixiv, twitter）")
    parser.add_argument("--reset", action="store_true", help="進捗をリセットして最初から")
    args = parser.parse_args()

    if args.reset:
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
            print("進捗ファイルを削除しました")

    with open('config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    db_path = 'data/eventmonitor.db'
    csv_path = 'monitored_accounts.csv'

    if not Path(db_path).exists():
        print(f"エラー: DB が見つかりません: {db_path}")
        return

    # CSV読み込み
    rank_map = load_rank_map(csv_path)
    total_accounts = sum(len(v) for v in rank_map.values())
    print("=" * 60)
    print("Hydrus ランクタグ一括付与スクリプト")
    if args.dry_run:
        print("  *** DRY-RUN モード（変更は行いません） ***")
    if args.platform:
        print(f"  対象プラットフォーム: {args.platform}")
    print(f"  CSVアカウント数: {total_accounts}")
    for platform, accounts in sorted(rank_map.items()):
        rank_dist = {}
        for r in accounts.values():
            rank_dist[r] = rank_dist.get(r, 0) + 1
        dist_str = ", ".join(f"rank:{k}={v}件" for k, v in sorted(rank_dist.items()))
        print(f"    {platform}: {len(accounts)}件 ({dist_str})")
    print("=" * 60)

    progress = load_progress() if not args.dry_run else {"processed": {}}
    already = len(progress.get("processed", {}))
    if already > 0:
        print(f"  前回の進捗: {already}件処理済み（スキップ）")

    stats = asyncio.run(apply_rank_tags(
        config, db_path, rank_map, args.platform, args.dry_run, progress
    ))

    if not args.dry_run:
        save_progress(progress)

    print("\n" + "=" * 60)
    print("結果サマリ:")
    print(f"  テーブル数:       {stats['tables_processed']}")
    print(f"  レコード確認:     {stats['records_checked']}")
    print(f"  URL検索:          {stats['urls_searched']}")
    print(f"  Hydrusファイル:   {stats['files_found']}")
    print(f"  タグ付与:         {stats['tags_added']}")
    print(f"  前回処理済み:     {stats['already_done']}")
    print(f"  Hydrusに未登録:   {stats['not_in_hydrus']}")
    print(f"  エラー:           {stats['errors']}")
    print("=" * 60)


if __name__ == '__main__':
    main()
