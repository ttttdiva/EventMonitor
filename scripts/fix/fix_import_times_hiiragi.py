#!/usr/bin/env python3
"""
削除済みPixivアカウント「柊」(34645256) のインポート時刻修正スクリプト

重複削除・タグ統合後にインポート順序が崩れるため、
作品ID昇順（古い順）→ ページ番号順にインポート時刻を再設定する。

ファイルの特定方法:
  1. 作品URL (https://www.pixiv.net/artworks/{id}) でHydrus内を検索
  2. 見つかったファイルのインポート時刻を作品順に上書き

使用方法:
    python scripts/fix/fix_import_times_hiiragi.py --dry-run
    python scripts/fix/fix_import_times_hiiragi.py

前提:
    - Hydrus APIキーに "Edit Times" 権限が必要
    - 重複削除が完了してから実行すること
"""

import sys
import os
import re
import asyncio
import argparse
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Tuple

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient


# --- 設定 ---
SOURCE_DIR = Path(r"F:\AI\datasets\image\style\柊 (pixiv_34645256)")
FILENAME_PATTERN = re.compile(r'^(\d+)_p(\d+)\.(jpg|jpeg|png|webp|gif|bmp|tiff|tif|avif)$', re.IGNORECASE)

# 基準時刻: 2019-01-01 00:00:00 UTC（作品ID 72451848 は2019年頃）
# 作品間は1時間、ページ間は1秒の間隔で設定
BASE_TIMESTAMP = datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp()
WORK_GAP = 3600      # 作品間: 1時間
PAGE_GAP = 1          # ページ間: 1秒


def get_sorted_files(source_dir: Path) -> List[Tuple[int, int, Path]]:
    """ディレクトリからファイルを (作品ID, ページ番号, パス) のリストで取得し、作品ID昇順→ページ番号昇順でソート"""
    files = []
    for f in source_dir.iterdir():
        if not f.is_file():
            continue
        m = FILENAME_PATTERN.match(f.name)
        if m:
            files.append((int(m.group(1)), int(m.group(2)), f))
    files.sort(key=lambda x: (x[0], x[1]))
    return files


def sha256_hash(filepath: Path) -> str:
    """ファイルのSHA256ハッシュを計算（Hydrusのファイル識別子）"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


async def main():
    parser = argparse.ArgumentParser(description="柊(34645256)のインポート時刻修正")
    parser.add_argument('--dry-run', action='store_true', help='変更せずに確認のみ')
    args = parser.parse_args()

    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    sorted_files = get_sorted_files(SOURCE_DIR)
    artwork_ids = sorted(set(aid for aid, _, _ in sorted_files))
    print(f"対象ファイル数: {len(sorted_files)}, 対象作品数: {len(artwork_ids)}")

    # 作品IDごとにグループ化
    work_files: Dict[int, List[Tuple[int, Path]]] = {}
    for aid, page, path in sorted_files:
        work_files.setdefault(aid, []).append((page, path))

    # 全ファイルのSHA256を事前計算
    print("SHA256ハッシュ計算中...")
    file_hashes: Dict[str, str] = {}  # filepath -> sha256
    for _, _, path in sorted_files:
        file_hashes[str(path)] = sha256_hash(path)
    print(f"ハッシュ計算完了: {len(file_hashes)}ファイル")
    print()

    async with HydrusClient(config) as hydrus:
        file_service_key = await hydrus.get_file_service_key()
        if not file_service_key:
            print("エラー: ファイルサービスキーが取得できません")
            return

        updated_total = 0
        skipped_total = 0

        for work_idx, artwork_id in enumerate(artwork_ids):
            pages = work_files[artwork_id]
            base_ts = BASE_TIMESTAMP + (work_idx * WORK_GAP)

            for page_num, path in pages:
                file_hash = file_hashes[str(path)]
                timestamp = base_ts + (page_num * PAGE_GAP)
                dt_display = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                if args.dry_run:
                    print(f"  [{work_idx+1}/{len(artwork_ids)}] {artwork_id}_p{page_num}: "
                          f"{file_hash[:16]}... -> {dt_display} UTC")
                    updated_total += 1
                else:
                    success = await hydrus.set_file_import_time(file_hash, timestamp, file_service_key)
                    if success:
                        updated_total += 1
                    else:
                        print(f"  スキップ: {artwork_id}_p{page_num} ({file_hash[:16]}...) - Hydrusに存在しない可能性")
                        skipped_total += 1

            if not args.dry_run and pages:
                print(f"  [{work_idx+1}/{len(artwork_ids)}] 作品 {artwork_id}: {len(pages)}ファイル処理")

            await asyncio.sleep(0.05)

        print()
        print(f"完了: {updated_total}ファイル更新, {skipped_total}ファイルスキップ")
        if args.dry_run:
            print("[DRY-RUN モード - 変更は行っていません]")


if __name__ == '__main__':
    asyncio.run(main())
