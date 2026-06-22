#!/usr/bin/env python3
"""
リネーム済みアカウントのHydrusタグ修正スクリプト（ワンショット）

recover_renamed_accounts.py で検出・CSV修正済みだが、
Hydrus上の既インポート画像のタグが未修正のアカウントを一括修正する。

処理内容（各アカウントごと）:
  1. creator:{old_username} タグの画像を検索
  2. creator:{old_username} を削除
  3. twitter_user:{old_username} と twitter_user:{new_username} を追加
  4. 他に creator: タグがなければ creator:{display_name} を追加

使用方法:
    python scripts/fix/fix_renamed_hydrus_tags.py           # dry-run（変更なし）
    python scripts/fix/fix_renamed_hydrus_tags.py --apply    # 実際に適用
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Windows コンソールのエンコーディング問題回避
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

load_dotenv(PROJECT_ROOT / ".env", override=True)

from src.hydrus_client import HydrusClient

# recover_renamed_accounts.py --apply 実行結果（@siezer_freek は手動修正済みのため除外）
RENAMED_ACCOUNTS: List[Dict[str, str]] = [
    {"old": "miyodomi", "new": "nakita_sick", "display_name": "失來"},
    {"old": "Sushinante0", "new": "Sushinante01", "display_name": "すしなんて"},
    {"old": "2525monaca", "new": "nenane0833", "display_name": "nenane"},
    {"old": "QebB7Rb9DL9dvUl", "new": "matukituneAtto", "display_name": "まつきつね@画力上げたい"},
    {"old": "gomgodkk25", "new": "jgmwv312mj", "display_name": "ْ"},
    {"old": "daiyayukiorigb", "new": "daiyagb", "display_name": "♢＿＿５分前のわんだーらんど。"},
    {"old": "sumire_sumelagi", "new": "grissoftware", "display_name": "gris（ぐり）"},
    {"old": "warabivi", "new": "yuki_warabi7509", "display_name": "雪蕨"},
    {"old": "sakiika0513", "new": "sakiika010513", "display_name": "さきいか"},
    {"old": "nicomi__chan", "new": "nicomi_chan", "display_name": "にこみちゃん"},
    {"old": "02_nap", "new": "__mp44", "display_name": "⚑︎⚐︎"},
    {"old": "hurusato_syu2", "new": "hurusato_syu", "display_name": "ふるさとしゅう"},
]


def load_config() -> dict:
    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def search_files_by_tag(client: HydrusClient, tag: str) -> List[str]:
    """指定タグを持つ全ファイルのハッシュを取得"""
    headers = client._get_headers()
    params = {
        "tags": json.dumps([tag]),
        "return_hashes": "true",
    }
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
) -> List[Dict[str, Any]]:
    """ファイルハッシュリストからメタデータを一括取得"""
    headers = client._get_headers()
    all_metadata = []
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
                print(f"  警告: メタデータ取得失敗 (HTTP {resp.status})")
                continue
            data = await resp.json()
            all_metadata.extend(data.get("metadata", []))

    return all_metadata


async def process_account(
    client: HydrusClient,
    account: Dict[str, str],
    apply: bool,
) -> Dict[str, int]:
    """1アカウント分のタグ修正を実行"""
    old = account["old"]
    new = account["new"]
    display_name = account["display_name"]
    tag_to_find = f"creator:{old}"

    print(f"\n--- @{old} → @{new} (display: {display_name}) ---")

    # creator:{old_username} で検索
    hashes = await search_files_by_tag(client, tag_to_find)
    if not hashes:
        print(f"  {tag_to_find} を持つファイルなし → スキップ")
        return {"found": 0, "modified": 0, "errors": 0, "creator_added": 0}

    print(f"  {tag_to_find} を持つファイル: {len(hashes)} 件")

    # メタデータ取得して他の creator: タグの有無を確認
    metadata_list = await get_files_metadata(client, hashes)
    hash_to_tags: Dict[str, List[str]] = {}
    for meta in metadata_list:
        h = meta.get("hash")
        if h:
            hash_to_tags[h] = client._extract_display_tags_from_metadata(meta)

    # creator: タグが他にないファイルを特定
    needs_creator = []
    has_other_creator = []
    for h in hashes:
        tags = hash_to_tags.get(h, [])
        other_creators = [
            t for t in tags if t.startswith("creator:") and t != tag_to_find
        ]
        if other_creators:
            has_other_creator.append(h)
        else:
            needs_creator.append(h)

    print(f"  他の creator: タグあり: {len(has_other_creator)} 件")
    print(f"  creator: タグなしになる: {len(needs_creator)} 件 → creator:{display_name} を追加")

    if not apply:
        print(f"  [dry-run] 以下の操作を実行予定:")
        print(f"    全 {len(hashes)} 件: creator:{old} 削除, twitter_user:{old} 追加, twitter_user:{new} 追加")
        if needs_creator:
            print(f"    {len(needs_creator)} 件: creator:{display_name} 追加")
        return {
            "found": len(hashes),
            "modified": 0,
            "errors": 0,
            "creator_added": len(needs_creator),
        }

    # 適用: 全ファイルに対して creator:{old} 削除 + twitter_user 追加
    errors = 0

    ok = await client.remove_tags_bulk(hashes, [tag_to_find], all_services=True)
    if not ok:
        print(f"  エラー: {tag_to_find} の削除に失敗")
        errors += len(hashes)

    ok = await client.add_tags_bulk(
        hashes, [f"twitter_user:{old}", f"twitter_user:{new}"]
    )
    if not ok:
        print(f"  エラー: twitter_user タグの追加に失敗")
        errors += len(hashes)

    # creator: タグがなくなるファイルに display_name を追加
    creator_added = 0
    if needs_creator:
        ok = await client.add_tags_bulk(
            needs_creator, [f"creator:{display_name}"]
        )
        if ok:
            creator_added = len(needs_creator)
        else:
            print(f"  エラー: creator:{display_name} の追加に失敗")
            errors += len(needs_creator)

    modified = len(hashes) if errors == 0 else len(hashes) - errors
    print(f"  完了: 修正 {modified} 件, creator追加 {creator_added} 件, エラー {errors} 件")

    return {
        "found": len(hashes),
        "modified": modified,
        "errors": errors,
        "creator_added": creator_added,
    }


async def main():
    parser = argparse.ArgumentParser(
        description="リネーム済みアカウントのHydrusタグ修正"
    )
    parser.add_argument("--apply", action="store_true", help="変更を実際に適用する")
    args = parser.parse_args()

    config = load_config()

    print("=" * 60)
    print("リネーム済みアカウント Hydrus タグ修正")
    print(f"対象: {len(RENAMED_ACCOUNTS)} アカウント")
    print(f"モード: {'適用' if args.apply else 'dry-run（変更なし）'}")
    print("=" * 60)

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です")
            return

        totals = {"found": 0, "modified": 0, "errors": 0, "creator_added": 0}

        for account in RENAMED_ACCOUNTS:
            result = await process_account(client, account, apply=args.apply)
            for k in totals:
                totals[k] += result[k]

    print(f"\n{'=' * 60}")
    print("合計結果")
    print(f"  対象ファイル: {totals['found']} 件")
    if args.apply:
        print(f"  修正成功: {totals['modified']} 件")
        print(f"  creator追加: {totals['creator_added']} 件")
        print(f"  エラー: {totals['errors']} 件")
    else:
        print(f"  creator追加予定: {totals['creator_added']} 件")
        print("実際に適用するには --apply フラグを付けて再実行してください。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
