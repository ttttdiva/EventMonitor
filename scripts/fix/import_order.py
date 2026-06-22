#!/usr/bin/env python3
"""
hydrus_folder_import.py の辞書順ソートバグで狂ったインポート順序を修正するスクリプト

問題:
  sorted() が Path の辞書順ソートを使っていたため、
  0.jpeg → 1.jpeg → 10.jpeg → 2.jpeg ... の順でインポートされた。
  正しくは 0.jpeg → 1.jpeg → 2.jpeg → ... → 10.jpeg。

修正方法:
  1. 対象フォルダを自然順で再スキャン
  2. 各ファイルのSHA256ハッシュを計算してHydrus上のファイルを特定
  3. 正しい順序に基づいてインポート時刻を上書き（同一記事内で1秒間隔）

使い方:
  # Dry-run（修正内容の確認のみ）
  python scripts/fix/import_order.py

  # 実行
  python scripts/fix/import_order.py --execute

  # 特定ソースのみ
  python scripts/fix/import_order.py --execute --source fanbox_hieroglyph

  # 特定の記事フォルダのみ（デバッグ用）
  python scripts/fix/import_order.py --execute --article-filter 3927480
"""

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import yaml

logger = logging.getLogger("FixImportOrder")

# ========== 定数 ==========

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.jfif'}

SOURCES = {
    'niconico': {
        'path': Path(r"D:\f\a\07_niconico\お気に入り絵"),
        'description': 'ニコニコお気に入り絵',
    },
    'fanbox_hieroglyph': {
        'path': Path(r"F:\AI\98_model\学習素材用\05_FANBOX\ヒエログリフ"),
        'description': 'FANBOX ヒエログリフ',
    },
    'fanbox_r18': {
        'path': Path(r"F:\AI\98_model\学習素材用\05_FANBOX\R-18"),
        'description': 'FANBOX R-18',
    },
}

SKIP_FOLDERS = {'00_ALL', '01_格納待ち', '02_other', '10_Cool', '11_全能', '20_any', '動画'}

# 基準時刻: 2020-01-01 00:00:00 UTC
BASE_TIMESTAMP = 1577836800.0
# 記事間のギャップ（秒）
ARTICLE_GAP = 3600  # 1時間
# 作者間のギャップ（秒）
CREATOR_GAP = 86400  # 1日


def _natural_sort_key(path: Path):
    """自然順ソート用キー"""
    stem = path.stem
    parts = re.split(r'(\d+)', stem)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def compute_sha256(file_path: Path) -> str:
    """ファイルのSHA256ハッシュを計算"""
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def _would_be_misordered(files: List[Path]) -> bool:
    """辞書順と自然順で結果が異なるか（修正が必要か）"""
    lexicographic = sorted(files)
    natural = sorted(files, key=_natural_sort_key)
    return lexicographic != natural


# ========== フォルダ構造解析 ==========

class ArticleGroup:
    """記事（またはフォルダ）単位のファイルグループ"""
    def __init__(self, name: str, files: List[Path], creator: str, source: str):
        self.name = name
        self.files = files  # 自然順ソート済み
        self.creator = creator
        self.source = source
        self.needs_fix = _would_be_misordered(files)


def scan_fanbox_source(base_path: Path, source_name: str) -> List[ArticleGroup]:
    """FANBOXフォルダを記事単位でスキャン"""
    groups = []
    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return groups

    for creator_folder in sorted(base_path.iterdir()):
        if not creator_folder.is_dir():
            continue
        creator_name = creator_folder.name

        for article_folder in sorted(creator_folder.iterdir()):
            if not article_folder.is_dir():
                continue

            files = [
                f for f in article_folder.iterdir()
                if f.is_file() and is_image_file(f)
            ]
            if not files:
                continue

            natural_sorted = sorted(files, key=_natural_sort_key)
            groups.append(ArticleGroup(
                name=article_folder.name,
                files=natural_sorted,
                creator=creator_name,
                source=source_name,
            ))

    return groups


def scan_niconico_source(base_path: Path) -> List[ArticleGroup]:
    """ニコニコフォルダを作者単位でスキャン"""
    groups = []
    if not base_path.exists():
        logger.warning(f"フォルダが見つかりません: {base_path}")
        return groups

    for subfolder in sorted(base_path.iterdir()):
        if not subfolder.is_dir() or subfolder.name in SKIP_FOLDERS:
            continue

        files = _collect_images_recursive(subfolder)
        if not files:
            continue

        groups.append(ArticleGroup(
            name=subfolder.name,
            files=files,  # already natural-sorted by _collect_images_recursive
            creator=subfolder.name,
            source='niconico',
        ))

    return groups


def _collect_images_recursive(folder: Path) -> List[Path]:
    """フォルダ内の画像を再帰的に自然順で収集"""
    images = []
    for item in sorted(folder.iterdir(), key=_natural_sort_key):
        if item.is_file() and is_image_file(item):
            images.append(item)
        elif item.is_dir():
            images.extend(_collect_images_recursive(item))
    return images


# ========== Hydrus API ==========

class HydrusOrderFixer:
    """Hydrusのインポート時刻を修正"""

    def __init__(self, api_url: str, access_key: str):
        self.api_url = api_url.rstrip('/')
        self.access_key = access_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.stats = {
            'fixed': 0,
            'not_found': 0,
            'already_correct': 0,
            'errors': 0,
            'skipped_no_fix_needed': 0,
        }

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        headers = {'Hydrus-Client-API-Access-Key': self.access_key}
        async with self.session.get(
            f"{self.api_url}/verify_access_key", headers=headers
        ) as resp:
            if resp.status != 200:
                raise ConnectionError(f"Hydrus API認証失敗: status={resp.status}")
            logger.info("Hydrus API接続成功")

        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _headers(self) -> Dict[str, str]:
        return {
            'Hydrus-Client-API-Access-Key': self.access_key,
            'Content-Type': 'application/json',
        }

    async def get_file_info(self, file_hash: str) -> Optional[Tuple[float, str]]:
        """ファイルのインポート時刻と実際のサービスキーを取得

        Returns:
            (import_time, file_service_key) or None if not found
        """
        headers = {'Hydrus-Client-API-Access-Key': self.access_key}
        import json as json_mod
        params = {
            'hashes': json_mod.dumps([file_hash]),
        }
        try:
            async with self.session.get(
                f"{self.api_url}/get_files/file_metadata",
                headers=headers,
                params=params,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    metadata_list = data.get('metadata', [])
                    if metadata_list:
                        file_services = metadata_list[0].get('file_services', {})
                        current_info = file_services.get('current', {})
                        for svc_key, svc_data in current_info.items():
                            if 'time_imported' in svc_data:
                                return (svc_data['time_imported'], svc_key)
                return None
        except Exception as e:
            logger.error(f"メタデータ取得エラー: {e}")
            return None

    async def set_import_time(self, file_hash: str, timestamp: float,
                              file_service_key: str) -> bool:
        """インポート時刻を設定"""
        data = {
            'hash': file_hash,
            'timestamp': timestamp,
            'timestamp_type': 3,  # file import time
            'file_service_key': file_service_key,
        }
        try:
            async with self.session.post(
                f"{self.api_url}/edit_times/set_time",
                headers=self._headers(),
                json=data,
            ) as resp:
                if resp.status == 200:
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"set_time APIエラー: {resp.status} - {error_text}")
                    return False
        except Exception as e:
            logger.error(f"set_time エラー: {e}")
            return False

    async def fix_article_group(self, group: ArticleGroup, base_ts: float, dry_run: bool) -> int:
        """
        記事グループのインポート順序を修正

        Returns:
            修正したファイル数
        """
        if not group.needs_fix:
            self.stats['skipped_no_fix_needed'] += len(group.files)
            return 0

        fixed_count = 0

        for i, file_path in enumerate(group.files):
            target_ts = base_ts + i  # 1秒間隔

            file_hash = compute_sha256(file_path)

            if dry_run:
                # 辞書順での位置と自然順での位置を表示
                lex_sorted = sorted(group.files)
                lex_pos = lex_sorted.index(file_path)
                nat_pos = i
                marker = " ← 順序変更" if lex_pos != nat_pos else ""
                print(f"    [{nat_pos}] {file_path.name} (辞書順では[{lex_pos}]){marker}")
                fixed_count += 1
            else:
                # ファイルのメタデータから実際のサービスキーを取得
                file_info = await self.get_file_info(file_hash)
                if file_info is None:
                    logger.warning(f"  Hydrusに見つかりません: {file_path.name}")
                    self.stats['not_found'] += 1
                    continue

                current_ts, actual_service_key = file_info
                success = await self.set_import_time(
                    file_hash, target_ts, actual_service_key
                )
                if success:
                    self.stats['fixed'] += 1
                    fixed_count += 1
                else:
                    self.stats['errors'] += 1

        return fixed_count


# ========== メイン ==========

def main():
    parser = argparse.ArgumentParser(
        description='hydrus_folder_import.py のソート順バグによるインポート順序を修正'
    )
    parser.add_argument(
        '--execute', action='store_true',
        help='実際に修正を実行する（デフォルトはdry-run）'
    )
    parser.add_argument(
        '--source', choices=list(SOURCES.keys()), nargs='+',
        default=list(SOURCES.keys()),
        help='対象ソース（デフォルト: 全て）'
    )
    parser.add_argument(
        '--article-filter', type=str, default=None,
        help='特定の記事フォルダ名でフィルタ（部分一致）'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='詳細ログを表示'
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # .envファイルを読み込み
    env_path = PROJECT_ROOT / '.env'
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value

    # フォルダスキャン
    all_groups: List[ArticleGroup] = []

    for source in args.source:
        desc = SOURCES[source]['description']
        path = SOURCES[source]['path']
        print(f"\n📁 {desc} をスキャン中... ({path})")

        if source == 'niconico':
            groups = scan_niconico_source(path)
        else:
            groups = scan_fanbox_source(path, source)

        if args.article_filter:
            groups = [g for g in groups if args.article_filter in g.name]

        needs_fix = [g for g in groups if g.needs_fix]
        print(f"   → {len(groups)} グループ中 {len(needs_fix)} グループが修正対象")
        all_groups.extend(groups)

    fix_groups = [g for g in all_groups if g.needs_fix]
    total_files = sum(len(g.files) for g in fix_groups)

    if not fix_groups:
        print("\n✅ 修正が必要なグループはありません")
        return

    print(f"\n📊 修正対象: {len(fix_groups)} グループ, {total_files} ファイル")

    if not args.execute:
        # Dry-run: 修正内容を表示
        print("\n" + "=" * 70)
        print("📋 Dry-Run: 順序変更の詳細")
        print("=" * 70)

        for group in fix_groups:
            print(f"\n  📂 {group.creator}/{group.name} ({len(group.files)} ファイル)")
            lex_sorted = sorted(group.files)
            for i, file_path in enumerate(group.files):
                lex_pos = lex_sorted.index(file_path)
                marker = " ← 順序変更" if lex_pos != i else ""
                print(f"    [{i}] {file_path.name} (辞書順では[{lex_pos}]){marker}")

        print("\n" + "=" * 70)
        print("💡 実際に修正するには --execute フラグを追加してください")
        print("=" * 70)
    else:
        # 実行モード
        asyncio.run(_execute_fix(fix_groups))


async def _execute_fix(groups: List[ArticleGroup]):
    """修正を実行"""
    config_path = PROJECT_ROOT / 'config.yaml'
    if not config_path.exists():
        logger.error(f"config.yaml が見つかりません: {config_path}")
        return

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    hydrus_config = config.get('hydrus', {})
    api_url = hydrus_config.get('api_url', 'http://127.0.0.1:45869')
    access_key = os.environ.get('HYDRUS_ACCESS_KEY') or hydrus_config.get('access_key')

    if not access_key:
        logger.error("HYDRUS_ACCESS_KEY が設定されていません")
        return

    total_files = sum(len(g.files) for g in groups)
    print(f"\n🔧 修正開始: {len(groups)} グループ, {total_files} ファイル")

    async with HydrusOrderFixer(api_url, access_key) as fixer:
        start_time = time.time()
        current_ts = BASE_TIMESTAMP
        processed = 0

        for gi, group in enumerate(groups, 1):
            print(
                f"\r  [{gi}/{len(groups)}] "
                f"{group.creator}/{group.name} ({len(group.files)} ファイル)"
                f" | ✅{fixer.stats['fixed']}"
                f" ❌{fixer.stats['errors']}"
                f" ❓{fixer.stats['not_found']}",
                end='', flush=True,
            )

            await fixer.fix_article_group(group, current_ts, dry_run=False)
            current_ts += len(group.files) + ARTICLE_GAP
            processed += len(group.files)

            # API負荷軽減
            if gi % 5 == 0:
                await asyncio.sleep(0.2)

        print()
        elapsed = time.time() - start_time

        print("\n" + "=" * 70)
        print("📊 修正結果")
        print("=" * 70)
        print(f"  処理時間:       {elapsed:.1f}秒")
        print(f"  インポート時刻修正: {fixer.stats['fixed']}")
        print(f"  修正不要:       {fixer.stats['skipped_no_fix_needed']}")
        print(f"  Hydrus未検出:   {fixer.stats['not_found']}")
        print(f"  エラー:         {fixer.stats['errors']}")
        print("=" * 70)


if __name__ == '__main__':
    main()
