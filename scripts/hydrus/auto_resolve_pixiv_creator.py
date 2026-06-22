#!/usr/bin/env python3
"""
Pixiv creator: タグの自動解決スクリプト

Pixivインポート画像で creator: タグが2つあり、片方が user_xxxx のパターンの場合、
user_ 側を自動削除してもう一方を残す。

使用方法:
    python scripts/hydrus/auto_resolve_pixiv_creator.py           # dry-run
    python scripts/hydrus/auto_resolve_pixiv_creator.py --apply   # 実際に適用
"""

import sys
import os
import io
import asyncio
import argparse
import json
import re
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

# Windows cp932 対策: stdout/stderr を UTF-8 に強制
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

# user_xxxx1234 のようなPixivユーザーIDパターン
PIXIV_USER_PATTERN = re.compile(r'^user_[a-z0-9]+$', re.IGNORECASE)


def load_config() -> dict:
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def extract_creator_tags(tags: List[str]) -> List[str]:
    return [t for t in tags if t.startswith('creator:')]


def has_pixiv_source(tags: List[str]) -> bool:
    return any(t == 'source:pixiv' for t in tags)


def is_pixiv_user_id(name: str) -> bool:
    """user_rhhm7283 のようなPixivユーザーIDパターンか判定"""
    return bool(PIXIV_USER_PATTERN.match(name))


async def fetch_all_creator_files(client: HydrusClient):
    """Hydrus から複数 creator: タグを持つ全ファイルのメタデータを取得"""
    import aiohttp

    print("Phase 1: Hydrus からデータを収集中...")

    headers = client._get_headers()
    search_tags = ['creator:*']
    params = {
        'tags': json.dumps(search_tags),
        'file_sort_type': 6,
    }

    for attempt in range(5):
        try:
            async with client.session.get(
                f"{client.api_url}/get_files/search_files",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    print(f"エラー: ファイル検索に失敗 (HTTP {resp.status})")
                    return None
                data = await resp.json()
            break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < 4:
                wait = 2 ** attempt
                print(f"  ネットワークエラー (retry {attempt+1}/5): {e}")
                await asyncio.sleep(wait)
            else:
                print(f"エラー: ファイル検索に5回失敗。中断します")
                return None

    file_ids = data.get('file_ids', [])
    print(f"  creator: タグを持つファイル: {len(file_ids)} 件")

    if not file_ids:
        return None

    hash_to_creators: Dict[str, List[str]] = {}
    hash_to_all_tags: Dict[str, List[str]] = {}
    batch_size = 256

    for i in range(0, len(file_ids), batch_size):
        batch = file_ids[i:i + batch_size]
        progress = min(i + batch_size, len(file_ids))
        print(f"  メタデータ取得中... {progress}/{len(file_ids)}", end='\r')

        params = {'file_ids': json.dumps(batch)}

        for attempt in range(5):
            try:
                async with client.session.get(
                    f"{client.api_url}/get_files/file_metadata",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                else:
                    data = {'metadata': []}

        for metadata in data.get('metadata', []):
            file_hash = metadata.get('hash')
            if not file_hash:
                continue

            all_tags = client._extract_display_tags_from_metadata(metadata)
            creator_tags = extract_creator_tags(all_tags)

            if len(creator_tags) >= 2:
                hash_to_creators[file_hash] = creator_tags
                hash_to_all_tags[file_hash] = all_tags

    print(f"\n  複数 creator: タグを持つファイル: {len(hash_to_creators)} 件")
    return hash_to_creators, hash_to_all_tags


async def search_files_by_tag(client: HydrusClient, tag: str) -> List[str]:
    headers = client._get_headers()
    params = {
        'tags': json.dumps([tag]),
        'return_hashes': 'true',
    }
    async with client.session.get(
        f"{client.api_url}/get_files/search_files",
        headers=headers,
        params=params
    ) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
    return data.get('hashes', [])


async def main():
    parser = argparse.ArgumentParser(
        description='Pixiv creator: タグの user_xxxx 自動解決'
    )
    parser.add_argument('--apply', action='store_true', help='変更を実際に適用する')
    args = parser.parse_args()

    config = load_config()

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です。")
            return

        result = await fetch_all_creator_files(client)
        if not result:
            print("対象ファイルがありません。")
            return

        hash_to_creators, hash_to_all_tags = result

        # Pixivソースで、2つのcreatorタグのうち片方がuser_パターンのグループを集約
        # キー: (keep_tag, remove_tag) → ハッシュリスト
        resolve_groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        skipped_non_pixiv = 0
        skipped_no_user_pattern = 0
        skipped_both_user = 0

        for file_hash, creators in hash_to_creators.items():
            all_tags = hash_to_all_tags.get(file_hash, [])

            # Pixivソースのみ対象
            if not has_pixiv_source(all_tags):
                skipped_non_pixiv += 1
                continue

            # 2つのcreatorタグのみ対象
            if len(creators) != 2:
                continue

            names = [c.replace('creator:', '') for c in creators]
            user_flags = [is_pixiv_user_id(n) for n in names]

            if user_flags[0] and user_flags[1]:
                # 両方 user_ パターン → スキップ
                skipped_both_user += 1
                continue

            if not user_flags[0] and not user_flags[1]:
                # どちらも user_ パターンでない → スキップ
                skipped_no_user_pattern += 1
                continue

            # 片方だけ user_ → 自動解決可能
            if user_flags[0]:
                remove_tag = creators[0]
                keep_tag = creators[1]
            else:
                remove_tag = creators[1]
                keep_tag = creators[0]

            resolve_groups[(keep_tag, remove_tag)].append(file_hash)

        total_files = sum(len(h) for h in resolve_groups.values())
        print(f"\nPhase 2: Pixiv user_ パターン自動解決")
        print(f"  自動解決可能: {total_files} ファイル ({len(resolve_groups)} グループ)")
        print(f"  スキップ:")
        print(f"    Pixiv以外: {skipped_non_pixiv}")
        print(f"    user_パターンなし: {skipped_no_user_pattern}")
        print(f"    両方user_パターン: {skipped_both_user}")

        if not resolve_groups:
            print("\n自動解決可能なグループはありません。")
            return

        # 一覧表示
        print(f"\n{'='*60}")
        print(f"自動処理一覧:")
        print(f"{'='*60}")
        for (keep_tag, remove_tag), hashes in sorted(
            resolve_groups.items(), key=lambda x: -len(x[1])
        ):
            print(f"  {keep_tag} を残す / {remove_tag} を削除 ({len(hashes)} 枚)")

        if not args.apply:
            print(f"\n[dry-run] 実際に適用するには --apply を付けてください")
            return

        # 適用
        print(f"\n適用中...")
        total_success = 0
        total_errors = 0

        for (keep_tag, remove_tag), _hashes in resolve_groups.items():
            # remove_tag を持つ全ファイルを検索して処理
            hashes = await search_files_by_tag(client, remove_tag)
            if not hashes:
                continue

            # 削除
            ok = await client.remove_tags_bulk(hashes, [remove_tag], all_services=True)
            if not ok:
                print(f"  エラー: {remove_tag} の削除に失敗")
                total_errors += len(hashes)
                continue

            # keep_tag + pixiv_user: を付与
            remove_name = remove_tag.replace('creator:', '')
            pixiv_user_tag = f"pixiv_user:{remove_name}"
            ok = await client.add_tags_bulk(hashes, [keep_tag, pixiv_user_tag])
            if not ok:
                print(f"  エラー: {keep_tag} / {pixiv_user_tag} の付与に失敗")
                total_errors += len(hashes)
                continue

            total_success += len(hashes)
            print(f"  {remove_tag} → 削除, {keep_tag} + {pixiv_user_tag} を付与 ({len(hashes)} 枚)")

        print(f"\n完了: 成功 {total_success}, エラー {total_errors}")


if __name__ == '__main__':
    asyncio.run(main())
