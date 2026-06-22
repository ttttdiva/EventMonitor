#!/usr/bin/env python3
"""
全Twitter画像の creator:{username} → twitter_user:{username} 一括移行スクリプト

昔のシステムでは username も creator: タグで登録していたため、
現在の方針（creator: は display_name 専用、username は twitter_user:）に合わせて一括修正する。

処理内容（各アカウントごと）:
  1. creator:{username} タグの画像を検索
  2. twitter_user:{username} を追加
  3. username != display_name の場合:
     - creator:{username} を削除
     - 他に creator: タグがなければ creator:{display_name} を追加
     （display_name が空の場合は、同じ creator:{username} を持つ他画像の
      creator: タグから最頻値を取得して使用する）

使用方法:
    python scripts/fix/migrate_creator_to_twitter_user.py           # dry-run
    python scripts/fix/migrate_creator_to_twitter_user.py --apply   # 実際に適用
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

load_dotenv(PROJECT_ROOT / ".env", override=True)

from src.hydrus_client import HydrusClient

MONITORED_CSV = PROJECT_ROOT / "monitored_accounts.csv"
DELETED_CSV = PROJECT_ROOT / "deleted_accounts.csv"


def load_config() -> dict:
    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_twitter_accounts() -> Dict[str, str]:
    """
    monitored_accounts.csv + deleted_accounts.csv から
    Twitter アカウントの username → display_name マッピングを返す
    """
    accounts: Dict[str, str] = {}

    # monitored_accounts.csv
    if MONITORED_CSV.exists():
        with open(MONITORED_CSV, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                platform = (row.get("platform") or "").strip()
                if platform in ("", "twitter"):
                    username = row["username"].strip()
                    display_name = (row.get("display_name") or "").strip()
                    accounts[username] = display_name

    # deleted_accounts.csv
    if DELETED_CSV.exists():
        with open(DELETED_CSV, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                platform = (row.get("platform") or "").strip()
                if platform in ("", "twitter"):
                    username = row["username"].strip()
                    if username not in accounts:
                        display_name = (row.get("display_name") or "").strip()
                        accounts[username] = display_name

    return accounts


def is_ascii_only(s: str) -> bool:
    """文字列がASCII文字のみか"""
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


async def search_files_by_tag(client: HydrusClient, tag: str) -> List[str]:
    """指定タグを持つ全ファイルのハッシュを取得"""
    headers = client._get_headers()
    params = {"tags": json.dumps([tag]), "return_hashes": "true"}
    async with client.session.get(
        f"{client.api_url}/get_files/search_files",
        headers=headers,
        params=params,
    ) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
    return data.get("hashes", [])


async def get_files_metadata(
    client: HydrusClient, hashes: List[str]
) -> Dict[str, List[str]]:
    """ファイルハッシュ → 全タグリスト のマッピングを返す"""
    headers = client._get_headers()
    result: Dict[str, List[str]] = {}
    batch_size = 256

    for i in range(0, len(hashes), batch_size):
        batch = hashes[i : i + batch_size]
        params = {"hashes": json.dumps(batch)}
        async with client.session.get(
            f"{client.api_url}/get_files/file_metadata",
            headers=headers,
            params=params,
        ) as resp:
            if resp.status != 200:
                continue
            data = await resp.json()
            for meta in data.get("metadata", []):
                h = meta.get("hash")
                if h:
                    result[h] = client._extract_display_tags_from_metadata(meta)

    return result


async def process_account(
    client: HydrusClient,
    username: str,
    display_name: str,
    apply: bool,
) -> Dict[str, int]:
    """1アカウント分の移行処理"""
    creator_tag = f"creator:{username}"
    twitter_user_tag = f"twitter_user:{username}"

    hashes = await search_files_by_tag(client, creator_tag)
    if not hashes:
        return {"found": 0, "migrated": 0, "creator_added": 0, "errors": 0}

    # username == display_name の場合
    same_name = display_name.lower() == username.lower() if display_name else False

    if same_name:
        if not is_ascii_only(username):
            # 日本語のusername=display_nameはあり得ない（データ異常）→ スキップ
            print(f"  @{username} ({len(hashes)} 枚): スキップ (非ASCII username=display_name、データ要確認)")
            return {"found": len(hashes), "migrated": 0, "creator_added": 0, "errors": 0}
        # ASCII の場合は twitter_user: の追加だけ
        print(f"  @{username} ({len(hashes)} 枚): twitter_user:{username} 追加のみ (display_name=username)")
        if apply:
            ok = await client.add_tags_bulk(hashes, [twitter_user_tag])
            if not ok:
                return {"found": len(hashes), "migrated": 0, "creator_added": 0, "errors": len(hashes)}
        return {"found": len(hashes), "migrated": len(hashes), "creator_added": 0, "errors": 0}

    # username != display_name → creator:{username} を削除して twitter_user: に移行
    # メタデータ取得して他の creator: タグの有無を確認
    hash_to_tags = await get_files_metadata(client, hashes)

    needs_creator: List[str] = []  # creator: が 0 になるファイル
    has_other_creator: List[str] = []

    for h in hashes:
        tags = hash_to_tags.get(h, [])
        other_creators = [t for t in tags if t.startswith("creator:") and t != creator_tag]
        if other_creators:
            has_other_creator.append(h)
        else:
            needs_creator.append(h)

    # display_name が空の場合、他画像から最頻の creator: タグを取得
    effective_display_name = display_name
    if not effective_display_name and has_other_creator:
        creator_counts: Counter = Counter()
        for h in has_other_creator:
            tags = hash_to_tags.get(h, [])
            for t in tags:
                if t.startswith("creator:") and t != creator_tag:
                    creator_counts[t] += 1
        if creator_counts:
            most_common_tag = creator_counts.most_common(1)[0][0]
            effective_display_name = most_common_tag.replace("creator:", "")
            print(f"  @{username}: display_name 空 → 他画像から推定: {effective_display_name}")

    creator_add_tag = f"creator:{effective_display_name}" if effective_display_name else None

    status = f"  @{username} ({len(hashes)} 枚): creator:{username} → twitter_user:{username}"
    if needs_creator:
        if creator_add_tag:
            status += f", {len(needs_creator)} 枚に {creator_add_tag} 追加"
        else:
            status += f", {len(needs_creator)} 枚が creator: なしになる（display_name 不明）"
    print(status)

    if not apply:
        return {
            "found": len(hashes),
            "migrated": 0,
            "creator_added": len(needs_creator) if creator_add_tag else 0,
            "errors": 0,
            "no_creator": len(needs_creator) if not creator_add_tag else 0,
        }

    errors = 0

    # 全ファイル: creator:{username} 削除 + twitter_user:{username} 追加
    ok = await client.remove_tags_bulk(hashes, [creator_tag], all_services=True)
    if not ok:
        print(f"    エラー: {creator_tag} の削除失敗")
        errors += len(hashes)

    ok = await client.add_tags_bulk(hashes, [twitter_user_tag])
    if not ok:
        print(f"    エラー: {twitter_user_tag} の追加失敗")
        errors += len(hashes)

    # creator: なしになるファイルに display_name を付与
    creator_added = 0
    if needs_creator and creator_add_tag:
        ok = await client.add_tags_bulk(needs_creator, [creator_add_tag])
        if ok:
            creator_added = len(needs_creator)
        else:
            print(f"    エラー: {creator_add_tag} の追加失敗")
            errors += len(needs_creator)

    return {
        "found": len(hashes),
        "migrated": len(hashes) if errors == 0 else len(hashes) - errors,
        "creator_added": creator_added,
        "errors": errors,
        "no_creator": len(needs_creator) if not creator_add_tag else 0,
    }


async def main():
    parser = argparse.ArgumentParser(
        description="全Twitter画像の creator:{username} → twitter_user:{username} 一括移行"
    )
    parser.add_argument("--apply", action="store_true", help="変更を実際に適用する")
    args = parser.parse_args()

    config = load_config()
    accounts = load_twitter_accounts()

    print("=" * 60)
    print("creator:{username} → twitter_user:{username} 一括移行")
    print(f"対象: {len(accounts)} Twitterアカウント")
    print(f"モード: {'適用' if args.apply else 'dry-run（変更なし）'}")
    print("=" * 60)

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です")
            return

        totals = {"found": 0, "migrated": 0, "creator_added": 0, "errors": 0, "no_creator": 0}
        accounts_with_files = 0

        for username, display_name in sorted(accounts.items()):
            result = await process_account(client, username, display_name, apply=args.apply)
            if result["found"] > 0:
                accounts_with_files += 1
            for k in totals:
                totals[k] += result.get(k, 0)

    print(f"\n{'=' * 60}")
    print("合計結果")
    print(f"  対象アカウント: {accounts_with_files} / {len(accounts)}")
    print(f"  対象ファイル: {totals['found']} 件")
    if args.apply:
        print(f"  移行成功: {totals['migrated']} 件")
        print(f"  creator追加: {totals['creator_added']} 件")
        print(f"  エラー: {totals['errors']} 件")
    else:
        print(f"  creator追加予定: {totals['creator_added']} 件")
        if totals.get("no_creator", 0):
            print(f"  creator: なしになる: {totals['no_creator']} 件（要確認）")
        print("実際に適用するには --apply を付けて再実行してください。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
