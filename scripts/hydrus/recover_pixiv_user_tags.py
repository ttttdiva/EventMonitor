#!/usr/bin/env python3
"""
Pixiv user_ タグのリカバリスクリプト

auto_resolve_pixiv_creator.py で creator:user_xxx を削除した際に
pixiv_user:user_xxx タグを付与し忘れた分をリカバリする。

マッピングファイル (logs/pixiv_recovery_mapping.json) を使い、
各 keep_tag を持つファイルに pixiv_user:user_xxx を追加する。

使用方法:
    python scripts/hydrus/recover_pixiv_user_tags.py           # dry-run
    python scripts/hydrus/recover_pixiv_user_tags.py --apply   # 実際に適用
"""

import sys
import os
import io
import asyncio
import argparse
import json
from pathlib import Path
from typing import Dict, List

# Windows cp932 対策
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

MAPPING_FILE = PROJECT_ROOT / "logs" / "pixiv_recovery_mapping.json"


def load_config() -> dict:
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


async def search_files_by_tags(client, tags: List[str]) -> List[str]:
    """複数タグのAND検索でファイルハッシュを取得"""
    import aiohttp
    headers = client._get_headers()
    params = {
        'tags': json.dumps(tags),
        'return_hashes': 'true',
    }
    async with client.session.get(
        f"{client.api_url}/get_files/search_files",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
    return data.get('hashes', [])


async def main():
    parser = argparse.ArgumentParser(
        description='Pixiv user_ タグのリカバリ'
    )
    parser.add_argument('--apply', action='store_true', help='変更を実際に適用する')
    args = parser.parse_args()

    if not MAPPING_FILE.exists():
        print(f"エラー: マッピングファイルが見つかりません: {MAPPING_FILE}")
        return

    with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
        mapping: Dict[str, str] = json.load(f)

    print(f"マッピング読み込み: {len(mapping)} ペア")

    config = load_config()

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です。")
            return

        total_success = 0
        total_errors = 0

        for user_name, keep_name in sorted(mapping.items(), key=lambda x: x[1]):
            keep_tag = f"creator:{keep_name}"
            pixiv_user_tag = f"pixiv_user:{user_name}"

            # keep_tag + source:pixiv を持つファイルを検索
            hashes = await search_files_by_tags(client, [keep_tag, "source:pixiv"])
            if not hashes:
                # source:pixiv がないかもしれないので keep_tag だけで再検索
                hashes = await search_files_by_tags(client, [keep_tag])
                if not hashes:
                    print(f"  スキップ: {keep_tag} のファイルが見つかりません")
                    continue

            print(f"  {pixiv_user_tag} を追加 → {keep_tag} ({len(hashes)} 枚)")

            if args.apply:
                ok = await client.add_tags_bulk(hashes, [pixiv_user_tag])
                if ok:
                    total_success += len(hashes)
                else:
                    print(f"    エラー: タグ追加に失敗")
                    total_errors += len(hashes)
            else:
                total_success += len(hashes)

        mode = "適用" if args.apply else "dry-run"
        print(f"\n完了 ({mode}): 成功 {total_success}, エラー {total_errors}")
        if not args.apply:
            print("実際に適用するには --apply を付けてください")


if __name__ == '__main__':
    asyncio.run(main())
