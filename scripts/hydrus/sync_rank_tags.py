#!/usr/bin/env python3
"""
Hydrus ランクタグ同期スクリプト

monitored_accounts.csv のランク設定を参照し、
Hydrus上の rank:N タグをCSVに合わせて修正する。
- CSVがrank:1なのにHydrus上にrank:2がある → rank:2を削除してrank:1を付与
- 正しいタグが既にある → スキップ
- rankタグが未付与 → 付与

使用方法:
    python scripts/hydrus/sync_rank_tags.py
    python scripts/hydrus/sync_rank_tags.py --dry-run
    python scripts/hydrus/sync_rank_tags.py --platform pixiv
"""

import sys
import os
import asyncio
import argparse
import csv
import json
import re
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
PROGRESS_FILE = LOGS_DIR / "hydrus_sync_rank_tags_progress.json"

# rank:N のパターン
RANK_TAG_PATTERN = re.compile(r'^rank:(\d+)$')

# プラットフォーム別テーブル定義
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
# Hydrus タグ操作
# =============================================================================

def extract_rank_tags(tags: List[str]) -> List[int]:
    """タグリストからrank:Nタグを抽出し、ランク値のリストを返す"""
    ranks = []
    for tag in tags:
        m = RANK_TAG_PATTERN.match(tag)
        if m:
            ranks.append(int(m.group(1)))
    return ranks


async def remove_tags_from_hydrus(hydrus: HydrusClient, file_hash: str,
                                   tags: List[str], platform: str) -> bool:
    """Hydrus APIでタグを削除（action '1' = delete from local）"""
    if not tags:
        return True

    try:
        headers = hydrus._get_headers()
        headers['Content-Type'] = 'application/json'

        service_name = hydrus._platform_to_service_name.get(platform) if platform else None
        if service_name:
            data = {
                'hashes': [file_hash],
                'service_names_to_actions_to_tags': {
                    service_name: {
                        '1': tags  # action 1 = delete
                    }
                },
            }
        else:
            data = {
                'hashes': [file_hash],
                'service_keys_to_actions_to_tags': {
                    hydrus._legacy_tag_service_key: {
                        '1': tags
                    }
                },
            }

        async with hydrus.session.post(
            f"{hydrus.api_url}/add_tags/add_tags",
            headers=headers,
            json=data
        ) as resp:
            return resp.status == 200

    except Exception as e:
        print(f"    タグ削除エラー: {e}")
        return False


# =============================================================================
# メイン処理
# =============================================================================

async def sync_rank_tags(
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
        'already_correct': 0,
        'tags_updated': 0,
        'tags_added': 0,
        'tags_removed': 0,
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

            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            if not cursor.fetchone():
                continue

            platform_ranks = rank_map.get(platform, {})
            if not platform_ranks:
                continue

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

                progress_key = f"{table_name}:{record_id}"
                if progress_key in processed_set:
                    stats['already_correct'] += 1
                    continue

                expected_rank = platform_ranks.get(user_id, DEFAULT_RANK)
                expected_tag = f"rank:{expected_rank}"

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
                    if not dry_run:
                        progress.setdefault("processed", {})[progress_key] = "not_found"
                    continue

                stats['files_found'] += len(file_hashes)

                for file_hash in file_hashes:
                    # 既存タグ取得
                    try:
                        current_tags = await hydrus._get_file_tags(file_hash)
                    except Exception as e:
                        stats['errors'] += 1
                        if stats['errors'] <= 5:
                            print(f"    エラー: タグ取得失敗 {file_hash[:16]}...: {e}")
                        continue

                    if current_tags is None:
                        stats['errors'] += 1
                        continue

                    # 現在のrank:Nタグを確認
                    current_rank_tags = [t for t in current_tags if RANK_TAG_PATTERN.match(t)]
                    wrong_tags = [t for t in current_rank_tags if t != expected_tag]
                    has_correct = expected_tag in current_rank_tags

                    if has_correct and not wrong_tags:
                        # 正しいタグが既にあり、不要なタグもない → スキップ
                        stats['already_correct'] += 1
                        continue

                    if dry_run:
                        if wrong_tags:
                            print(f"    [DRY-RUN] {file_hash[:16]}... 削除:{wrong_tags} → 付与:{expected_tag} ({user_id})")
                            stats['tags_removed'] += len(wrong_tags)
                        if not has_correct:
                            if not wrong_tags:
                                print(f"    [DRY-RUN] {file_hash[:16]}... 付与:{expected_tag} ({user_id})")
                            stats['tags_added'] += 1
                        stats['tags_updated'] += 1
                        continue

                    # 不正なrank:Nタグを削除
                    if wrong_tags:
                        ok = await remove_tags_from_hydrus(hydrus, file_hash, wrong_tags, platform)
                        if ok:
                            stats['tags_removed'] += len(wrong_tags)
                        else:
                            stats['errors'] += 1
                            continue

                    # 正しいタグを付与
                    if not has_correct:
                        try:
                            ok = await hydrus.add_tags(file_hash, [expected_tag], platform=platform)
                            if ok:
                                stats['tags_added'] += 1
                            else:
                                stats['errors'] += 1
                                continue
                        except Exception as e:
                            stats['errors'] += 1
                            if stats['errors'] <= 5:
                                print(f"    エラー: タグ付与失敗: {e}")
                            continue

                    stats['tags_updated'] += 1

                if not dry_run:
                    progress.setdefault("processed", {})[progress_key] = expected_tag

                # 定期的に進捗保存
                if not dry_run and stats['urls_searched'] % 100 == 0:
                    save_progress(progress)
                    print(f"    ... {stats['urls_searched']}件検索済み / 更新:{stats['tags_updated']}件 / 正常:{stats['already_correct']}件")

    conn.close()
    return stats


# =============================================================================
# エントリポイント
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Hydrus ランクタグ同期（CSV→Hydrus）")
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

    rank_map = load_rank_map(csv_path)
    total_accounts = sum(len(v) for v in rank_map.values())
    print("=" * 60)
    print("Hydrus ランクタグ同期スクリプト（CSV→Hydrus）")
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

    stats = asyncio.run(sync_rank_tags(
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
    print(f"  タグ更新:         {stats['tags_updated']}  (削除:{stats['tags_removed']} / 付与:{stats['tags_added']})")
    print(f"  既に正しい:       {stats['already_correct']}")
    print(f"  Hydrusに未登録:   {stats['not_in_hydrus']}")
    print(f"  エラー:           {stats['errors']}")
    print("=" * 60)


if __name__ == '__main__':
    main()
