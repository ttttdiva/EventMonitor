#!/usr/bin/env python3
"""
削除済みPixivアカウントのローカルファイルをHydrusにインポートするスクリプト

ファイル名から作品IDとページ番号を読み取り、
作品ID昇順（古い順）→ページ番号昇順でインポートする。

使用方法:
    python scripts/fix/import_deleted_pixiv_user.py --dry-run
    python scripts/fix/import_deleted_pixiv_user.py
"""

import sys
import os
import re
import asyncio
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

import yaml
from dotenv import load_dotenv

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .envファイルを読み込み
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient


# --- 設定 ---
SOURCE_DIR = Path(r"F:\AI\datasets\training\style\柊 (pixiv_34645256)")
PIXIV_USER_ID = "34645256"
CREATOR_NAME = "柊"
# ファイル名パターン: {artwork_id}_p{page}.{ext}
FILENAME_PATTERN = re.compile(r'^(\d+)_p(\d+)\.(jpg|jpeg|png|webp|gif|bmp|tiff|tif|avif)$', re.IGNORECASE)


def parse_files(source_dir: Path) -> List[Tuple[int, int, Path]]:
    """ファイルを解析して (作品ID, ページ番号, パス) のリストを返す"""
    entries = []
    for f in source_dir.iterdir():
        if not f.is_file():
            continue
        m = FILENAME_PATTERN.match(f.name)
        if m:
            artwork_id = int(m.group(1))
            page_num = int(m.group(2))
            entries.append((artwork_id, page_num, f))
        else:
            print(f"  スキップ（パターン不一致）: {f.name}")
    # 作品ID昇順 → ページ番号昇順
    entries.sort(key=lambda x: (x[0], x[1]))
    return entries


async def main():
    parser = argparse.ArgumentParser(description="削除済みPixivユーザーのファイルをHydrusにインポート")
    parser.add_argument('--dry-run', action='store_true', help='実際にはインポートせず一覧を表示')
    args = parser.parse_args()

    # config読み込み
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # ファイル解析
    print(f"ソースディレクトリ: {SOURCE_DIR}")
    entries = parse_files(SOURCE_DIR)
    print(f"対象ファイル数: {len(entries)}")

    # 作品IDごとにグループ化して表示
    works: Dict[int, List[Tuple[int, Path]]] = defaultdict(list)
    for artwork_id, page_num, path in entries:
        works[artwork_id].append((page_num, path))
    print(f"作品数: {len(works)}")
    print()

    if args.dry_run:
        for artwork_id in sorted(works.keys()):
            pages = works[artwork_id]
            print(f"  作品 {artwork_id} ({len(pages)}ページ): https://www.pixiv.net/artworks/{artwork_id}")
            for page_num, path in sorted(pages):
                print(f"    p{page_num}: {path.name}")
        print(f"\n合計: {len(entries)}ファイル / {len(works)}作品")
        print("--dry-run なのでインポートは実行しません。")
        return

    # Hydrusにインポート
    async with HydrusClient(config) as hydrus:
        success_count = 0
        skip_count = 0
        fail_count = 0

        for i, (artwork_id, page_num, file_path) in enumerate(entries, 1):
            work_url = f"https://www.pixiv.net/artworks/{artwork_id}"
            print(f"[{i}/{len(entries)}] {file_path.name} -> ", end="", flush=True)

            # ファイルインポート
            file_hash = await hydrus.import_file(file_path)
            if not file_hash:
                print("失敗")
                fail_count += 1
                continue

            # URL関連付け
            await hydrus.associate_url(file_hash, work_url)

            # タグ生成・追加
            tags = [
                "source:pixiv",
                "imported_by:eventmonitor",
                f"creator:{CREATOR_NAME}",
                f"pixiv_id:{artwork_id}",
            ]
            tag_result = await hydrus.add_tags(file_hash, tags, platform="pixiv")
            if tag_result:
                success_count += 1
                print("OK")
            else:
                # インポートはできたがタグ追加失敗
                success_count += 1
                print("OK（タグ追加失敗）")

        print(f"\n完了: 成功={success_count}, 失敗={fail_count}")


if __name__ == '__main__':
    asyncio.run(main())
