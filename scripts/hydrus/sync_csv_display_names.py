#!/usr/bin/env python3
"""
CSV display_name 整合性修正スクリプト（ワンショット）

Hydrus上のcreator:タグを正とし、monitored_accounts.csv の display_name を同期する。

方針:
  1. Hydrusからcreator:タグ付き全ファイルのメタデータ（creator:タグ + known_urls）を取得
  2. URLからCSVのusernameを逆引き → そのファイルのcreator:タグ名を紐付け
  3. username毎に最頻のcreator名を集計（= cleanupで選ばれた正しい名前）
  4. CSVのdisplay_nameと比較し、不一致があれば修正

使用方法:
    python scripts/hydrus/sync_csv_display_names.py            # dry-run（差分表示のみ）
    python scripts/hydrus/sync_csv_display_names.py --apply    # CSVを実際に更新
"""

import sys
import os
import io
import asyncio
import argparse
import json
import csv
import re
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

# Windows cp932 でのエンコードエラーを回避
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

TWITTER_URL_PATTERN = re.compile(
    r'https?://(?:x\.com|twitter\.com)/([^/]+)/status/', re.IGNORECASE
)
PIXIV_URL_PATTERN = re.compile(
    r'https?://www\.pixiv\.net/(?:en/)?users/(\d+)', re.IGNORECASE
)


def extract_csv_username_from_urls(urls: List[str]) -> Set[str]:
    """URLからCSVのusername列に対応する値を抽出"""
    usernames: Set[str] = set()
    for url in urls:
        m = TWITTER_URL_PATTERN.match(url)
        if m:
            usernames.add(m.group(1).lower())
            continue
        m = PIXIV_URL_PATTERN.search(url)
        if m:
            usernames.add(m.group(1))
    return usernames


def load_csv_accounts(csv_path: Path) -> List[Dict[str, str]]:
    """CSVを読み込んで行リストを返す"""
    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    return fieldnames, rows


async def fetch_creator_url_mapping(client: HydrusClient) -> Dict[str, Counter]:
    """
    Hydrusからcreator:タグ付き全ファイルを取得し、
    csv_username → Counter({creator名: ファイル数}) のマッピングを構築。
    """
    import aiohttp

    print("Hydrus からデータを収集中...")
    headers = client._get_headers()

    # creator: タグを持つファイルを検索
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
                    return {}
                data = await resp.json()
            break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < 4:
                wait = 2 ** attempt
                print(f"  ネットワークエラー (retry {attempt+1}/5): {e}")
                await asyncio.sleep(wait)
            else:
                print(f"エラー: ファイル検索に5回失敗。中断します")
                return {}

    file_ids = data.get('file_ids', [])
    print(f"  creator: タグ付きファイル: {len(file_ids)} 件")

    if not file_ids:
        return {}

    # username → creator名のカウンタ
    username_to_creators: Dict[str, Counter] = defaultdict(Counter)
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
                    batch_data = await resp.json()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                else:
                    batch_data = {'metadata': []}

        for metadata in batch_data.get('metadata', []):
            all_tags = client._extract_display_tags_from_metadata(metadata)
            creator_tags = [t for t in all_tags if t.startswith('creator:')]
            urls = metadata.get('known_urls', [])

            if not creator_tags or not urls:
                continue

            csv_usernames = extract_csv_username_from_urls(urls)
            for username in csv_usernames:
                for ct in creator_tags:
                    name = ct.replace('creator:', '')
                    username_to_creators[username][name] += 1

    print(f"\n  URL紐付け済みアカウント: {len(username_to_creators)} 件")
    return username_to_creators


async def main():
    parser = argparse.ArgumentParser(description='CSV display_name 整合性修正')
    parser.add_argument('--apply', action='store_true', help='CSVを実際に更新する')
    args = parser.parse_args()

    csv_path = PROJECT_ROOT / "monitored_accounts.csv"
    if not csv_path.exists():
        print("エラー: monitored_accounts.csv が見つかりません")
        return

    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    fieldnames, rows = load_csv_accounts(csv_path)

    # CSVのusername → 行インデックス + 現在のdisplay_name
    csv_map: Dict[str, Tuple[int, str]] = {}
    for i, row in enumerate(rows):
        csv_map[row['username']] = (i, row['display_name'])

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です")
            return

        username_to_creators = await fetch_creator_url_mapping(client)

    if not username_to_creators:
        print("Hydrusにデータがありません")
        return

    def normalize_for_compare(s: str) -> str:
        """Hydrusの正規化（小文字化+全角スペース→半角）を模擬して比較用に正規化"""
        return s.lower().replace('\u3000', ' ')

    # 差分検出
    mismatches: List[Tuple[str, str, str, int]] = []  # (username, csv_name, hydrus_name, file_count)
    ambiguous: List[Tuple[str, str, Counter]] = []  # (username, csv_name, counter)

    for username, (row_idx, csv_display) in csv_map.items():
        if username not in username_to_creators:
            continue

        counter = username_to_creators[username]
        top_name, top_count = counter.most_common(1)[0]
        total = sum(counter.values())

        # Hydrusは小文字化+スペース正規化するため、その差異のみの場合はスキップ
        if normalize_for_compare(csv_display) == normalize_for_compare(top_name):
            continue

        # 最頻が圧倒的多数（80%以上）なら確定
        if top_count / total >= 0.8:
            mismatches.append((username, csv_display, top_name, top_count))
        else:
            # 複数のcreator名が拮抗 → 報告のみ
            ambiguous.append((username, csv_display, counter))

    # 結果表示
    if not mismatches and not ambiguous:
        print("\n全アカウントのdisplay_nameは整合しています。")
        return

    if mismatches:
        print(f"\n不一致: {len(mismatches)} 件")
        print("-" * 70)
        for username, csv_name, hydrus_name, count in mismatches:
            print(f"  {username}: CSV「{csv_name}」→ Hydrus「{hydrus_name}」({count} 枚)")

    if ambiguous:
        print(f"\n判定保留（creator名が複数拮抗）: {len(ambiguous)} 件")
        print("-" * 70)
        for username, csv_name, counter in ambiguous:
            names = ', '.join(f'{n}({c}枚)' for n, c in counter.most_common(5))
            print(f"  {username}: CSV「{csv_name}」/ Hydrus: {names}")

    # 適用
    if mismatches and args.apply:
        print(f"\nCSVを更新中...")
        updated = 0
        for username, csv_name, hydrus_name, _ in mismatches:
            row_idx, _ = csv_map[username]
            rows[row_idx]['display_name'] = hydrus_name
            updated += 1

        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"CSV更新完了: {updated} アカウント")
    elif mismatches and not args.apply:
        print(f"\n[dry-run] 実際に更新するには --apply を付けてください")


if __name__ == '__main__':
    asyncio.run(main())
