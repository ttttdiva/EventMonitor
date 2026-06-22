#!/usr/bin/env python3
"""
Hydrus重複削除操作でタグが汚れた画像を、正しいURLのメタデータで再設定するスクリプト

対話式ループ: 起動するとハッシュとURLを繰り返し聞き、qで終了。
URLからプラットフォームを自動判別（Pixiv / Twitter）。

処理フロー（1件ごと）:
  1. SHA256ハッシュで対象ファイルを特定
  2. URLからメタデータ取得（Pixiv: gallery-dl / Twitter: DB）
  3. 同じURLに紐付く全ファイルも検索
  4. 全対象ファイルの既存タグを削除 → 正しいタグを再付与

使用方法:
    python scripts/hydrus/reset_tags_by_url.py
    python scripts/hydrus/reset_tags_by_url.py --dry-run
"""

import sys
import os
import io
import asyncio
import csv
import json
import re
import argparse
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# Windows環境でのUnicode出力対応
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# プロジェクトのルートディレクトリをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

import yaml
from src.hydrus_client import HydrusClient
from src.pixiv_extractor import PixivExtractor


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def prompt_input(label: str) -> Optional[str]:
    """入力を受け取る。q/quit/exitで None を返す。"""
    try:
        value = input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if value.lower() in ('q', 'quit', 'exit'):
        return None
    return value


def lookup_csv_info(username: str, platform: str) -> Tuple[int, List[str], str]:
    """monitored_accounts.csvからrank・custom_tags・display_nameを取得"""
    csv_path = PROJECT_ROOT / 'monitored_accounts.csv'
    rank = 3
    custom_tags: List[str] = []
    display_name = ''

    if not csv_path.exists():
        return rank, custom_tags, display_name

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('platform') == platform and row.get('username') == username:
                rank = int(row.get('rank', 3) or 3)
                ct = row.get('custom_tags', '')
                if ct:
                    custom_tags = [t.strip() for t in ct.split(',') if t.strip()]
                display_name = row.get('display_name', '')
                break

    return rank, custom_tags, display_name


# ---------------------------------------------------------------------------
# URL判別
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> Optional[str]:
    """URLからプラットフォームを判別"""
    if re.search(r'pixiv\.net/.*artworks/\d+', url):
        return 'pixiv'
    if re.search(r'(twitter\.com|x\.com)/\w+/status/\d+', url):
        return 'twitter'
    return None


def parse_pixiv_artwork_id(url: str) -> Optional[str]:
    match = re.search(r'artworks/(\d+)', url)
    return match.group(1) if match else None


def parse_twitter_url(url: str) -> Optional[Tuple[str, str]]:
    """Twitter URLから (username, tweet_id) を抽出"""
    match = re.search(r'(?:twitter\.com|x\.com)/(\w+)/status/(\d+)', url)
    if match:
        return match.group(1), match.group(2)
    return None


# ---------------------------------------------------------------------------
# Twitter メタデータ取得（DB）
# ---------------------------------------------------------------------------

def get_tweet_from_db(tweet_id: str, config: dict) -> Optional[Dict[str, Any]]:
    """DBからツイートデータを取得"""
    db_path = config.get('database', {}).get('path', 'data/tweets.db')
    db_full = PROJECT_ROOT / db_path
    if not db_full.exists():
        return None

    conn = sqlite3.connect(str(db_full))
    conn.row_factory = sqlite3.Row
    try:
        # all_tweets → event_tweets の順で検索
        for table in ('all_tweets', 'event_tweets'):
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (tweet_id,)
            ).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------------------
# プラットフォーム別処理
# ---------------------------------------------------------------------------

async def process_pixiv(
    file_hash: str,
    url: str,
    hydrus: HydrusClient,
    pixiv: PixivExtractor,
    dry_run: bool,
) -> bool:
    """Pixiv作品のタグリセット"""

    artwork_id = parse_pixiv_artwork_id(url)
    if not artwork_id:
        print(f"  エラー: artwork ID抽出失敗: {url}")
        return False

    print(f"  [Pixiv] artwork ID: {artwork_id}")
    print("  メタデータ取得中...")
    works = pixiv.fetch_user_works_by_artwork_id(artwork_id)
    if not works:
        print(f"  エラー: メタデータ取得失敗")
        return False

    work_data = works[0]
    user_id = work_data.get('username', '')

    # CSV情報（display_nameはCSVを正とする）
    rank, custom_tags, csv_display_name = lookup_csv_info(user_id, 'pixiv')
    display_name = csv_display_name or work_data.get('display_name', '(不明)')
    work_data['display_name'] = display_name
    work_data['rank'] = rank
    work_data['custom_tags'] = custom_tags

    print(f"  タイトル: {work_data.get('text', '(なし)')}")
    print(f"  作者: {display_name} (user_id: {user_id})")
    print(f"  ページ数: {work_data.get('page_count', 1)}")
    print(f"  Pixivタグ: {work_data.get('tags', [])}")

    # タグ生成
    new_tags = hydrus._generate_pixiv_tags(work_data)
    if user_id:
        pixiv_user_tag = f"pixiv_user:{user_id}"
        if pixiv_user_tag not in new_tags:
            new_tags.append(pixiv_user_tag)

    # ノート用テキスト
    note_name = "pixiv description"
    note_text = work_data.get('text', '')

    return await _apply_reset(
        file_hash, url, new_tags, note_name, note_text,
        hydrus, dry_run, platform="pixiv",
    )


async def process_twitter(
    file_hash: str,
    url: str,
    hydrus: HydrusClient,
    config: dict,
    dry_run: bool,
) -> bool:
    """Twitterツイートのタグリセット"""

    parsed = parse_twitter_url(url)
    if not parsed:
        print(f"  エラー: Twitter URL解析失敗: {url}")
        return False

    url_username, tweet_id = parsed
    print(f"  [Twitter] @{url_username} / tweet_id: {tweet_id}")

    # DBからツイートデータ取得
    tweet_row = get_tweet_from_db(tweet_id, config)
    if not tweet_row:
        print(f"  エラー: tweet_id {tweet_id} がDBに見つかりません")
        return False

    username = tweet_row.get('username', url_username)
    display_name = tweet_row.get('display_name', '')
    tweet_text = tweet_row.get('tweet_text', '')
    sensitive = tweet_row.get('sensitive', False)

    # CSV情報（display_nameはCSVを正とする）
    rank, custom_tags, csv_display_name = lookup_csv_info(username, 'twitter')
    display_name = csv_display_name or display_name

    print(f"  作者: {display_name} (@{username})")
    print(f"  本文: {tweet_text[:80]}{'...' if len(tweet_text) > 80 else ''}")
    print(f"  sensitive: {sensitive}")

    # _generate_tags() 用のデータ構築
    tweet_data = {
        'id': tweet_id,
        'username': username,
        'display_name': display_name,
        'content': tweet_text,
        'sensitive': sensitive,
        'rank': rank,
        'custom_tags': custom_tags,
    }

    new_tags = hydrus._generate_tags(tweet_data)

    # ノート用テキスト
    note_name = "twitter description"
    # t.coリンク除去
    cleaned = re.sub(r'https?://t\.co/\S+', '', tweet_text).strip()
    lines = [line.strip() for line in cleaned.split('\n')]
    note_text = '\n'.join(line for line in lines if line)

    # Twitter URLを正規化（x.com → twitter.com）
    canonical_url = f"https://twitter.com/{username}/status/{tweet_id}"

    return await _apply_reset(
        file_hash, canonical_url, new_tags, note_name, note_text,
        hydrus, dry_run, platform="twitter",
    )


# ---------------------------------------------------------------------------
# 共通のタグリセット適用
# ---------------------------------------------------------------------------

async def _apply_reset(
    file_hash: str,
    url: str,
    new_tags: List[str],
    note_name: str,
    note_text: str,
    hydrus: HydrusClient,
    dry_run: bool,
    platform: str,
) -> bool:
    """タグ削除→再付与の共通処理"""

    # 対象ファイル確認
    current_tags = await hydrus._get_file_tags(file_hash)
    if current_tags is None:
        print(f"  エラー: ハッシュ {file_hash} がHydrusに見つかりません")
        return False

    print(f"  現在のタグ ({len(current_tags)}個): {sorted(current_tags)}")

    # URL検索で同URLの全ファイル取得
    url_hashes = await hydrus.search_files_by_url(url)
    all_hashes = list(dict.fromkeys([file_hash] + url_hashes))

    print(f"  対象ファイル: {len(all_hashes)}件")
    for h in all_hashes:
        tags = await hydrus._get_file_tags(h)
        tag_count = len(tags) if tags else 0
        marker = " *" if h == file_hash else ""
        print(f"    {h[:16]}...{marker} ({tag_count}タグ)")

    print(f"  新しいタグ ({len(new_tags)}個): {sorted(new_tags)}")

    # dry-run
    if dry_run:
        for h in all_hashes:
            old = await hydrus._get_file_tags(h)
            if old is None:
                continue
            old_set, new_set = set(old), set(new_tags)
            removed = sorted(old_set - new_set)
            added = sorted(new_set - old_set)
            marker = " *" if h == file_hash else ""
            print(f"    {h[:16]}...{marker}: -{len(removed)} +{len(added)}")
        print("  [ドライラン] 変更なし")
        return True

    # 実行確認
    answer = input(f"  {len(all_hashes)}件リセット実行? [Y/n]: ").strip().lower()
    if answer == 'n':
        print("  スキップ")
        return False

    # タグリセット
    success_count = 0
    for h in all_hashes:
        old_tags = await hydrus._get_file_tags(h)
        if old_tags is None:
            continue

        if old_tags:
            ok = await hydrus.remove_tags_bulk([h], old_tags, all_services=True)
            if not ok:
                print(f"    {h[:16]}... タグ削除失敗")
                continue

        ok = await hydrus.add_tags(h, new_tags, platform=platform)
        if not ok:
            print(f"    {h[:16]}... タグ追加失敗")
            continue

        await hydrus.associate_url(h, url)
        if note_text:
            await hydrus.add_note(h, note_name, note_text)

        success_count += 1

    print(f"  完了: {success_count}/{len(all_hashes)}件")
    return True


# ---------------------------------------------------------------------------
# 対話ループ
# ---------------------------------------------------------------------------

async def interactive_loop(dry_run: bool = False) -> None:
    config = load_config()
    pixiv = PixivExtractor(config)

    print("=== Hydrus タグリセット ===")
    print("対応: Pixiv artwork URL / Twitter(X) status URL")
    if dry_run:
        print("[ドライラン] 変更は行いません")
    print("qで終了\n")

    async with HydrusClient(config) as hydrus:
        count = 0
        while True:
            print(f"--- #{count + 1} ---")

            file_hash = prompt_input("SHA256ハッシュ")
            if file_hash is None:
                break
            if not file_hash:
                print("  ハッシュが空です\n")
                continue

            url = prompt_input("正しいURL")
            if url is None:
                break
            if not url:
                print("  URLが空です\n")
                continue

            platform = detect_platform(url)
            if platform == 'pixiv':
                ok = await process_pixiv(file_hash, url, hydrus, pixiv, dry_run)
            elif platform == 'twitter':
                ok = await process_twitter(file_hash, url, hydrus, config, dry_run)
            else:
                print(f"  エラー: 未対応のURL形式です: {url}")
                print("  対応: pixiv.net/artworks/... / twitter.com(x.com)/.../status/...")
                ok = False

            if ok:
                count += 1
            print()

    print(f"\n合計 {count}件 処理しました。")


def main():
    parser = argparse.ArgumentParser(
        description='Hydrus重複削除でタグが汚れた画像を正しいURLのメタデータでリセット（対話式）'
    )
    parser.add_argument('--dry-run', action='store_true', help='実際には変更せず確認のみ')
    args = parser.parse_args()

    asyncio.run(interactive_loop(dry_run=args.dry_run))


if __name__ == '__main__':
    main()
