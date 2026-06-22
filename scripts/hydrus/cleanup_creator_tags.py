#!/usr/bin/env python3
"""
Hydrus creator: タグ重複整理ツール

1枚の画像に複数の creator: タグが付いている問題を対話的に解決する。
- 同じ creator タグ組み合わせを持つファイル群をグループ化
- ユーザーが正しい creator 名を選択
- 不要なタグを削除（英数字のみのものは twitter_user: に移動可能）

使用方法:
    python scripts/hydrus/cleanup_creator_tags.py                   # dry-run（変更なし）
    python scripts/hydrus/cleanup_creator_tags.py --apply           # 実際に適用
    python scripts/hydrus/cleanup_creator_tags.py --apply --resume  # 前回の続きから再開
    python scripts/hydrus/cleanup_creator_tags.py --export          # CSV出力のみ

途中中断:
    Ctrl+C で安全に中断可能。進捗は自動保存され、--resume で続きから再開できる。
    creatorタグ変更時は monitored_accounts.csv の display_name も自動更新される。
"""

import sys
import os
import asyncio
import argparse
import json
import csv
import re
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import yaml

env_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=env_path, override=True)

from src.hydrus_client import HydrusClient

LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = LOGS_DIR / "creator_cleanup_progress.json"


def combo_key(creators) -> str:
    """frozensetをソート済み文字列キーに変換（進捗ファイル用）"""
    return '|'.join(sorted(creators))


def load_progress() -> Set[str]:
    """進捗ファイルから処理済みグループを読み込む"""
    if not PROGRESS_FILE.exists():
        return set()
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        completed = set(data.get('completed_groups', []))
        print(f"  進捗ファイル読み込み: {len(completed)} グループ処理済み")
        return completed
    except (json.JSONDecodeError, OSError) as e:
        print(f"  警告: 進捗ファイルの読み込みに失敗 ({e})")
        return set()


def save_progress(completed: Set[str]) -> None:
    """処理済みグループを進捗ファイルに保存"""
    data = {
        'completed_groups': sorted(completed),
        'updated_at': __import__('datetime').datetime.now().isoformat(),
    }
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    """config.yaml を読み込む"""
    config_path = PROJECT_ROOT / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def is_ascii_only(s: str) -> bool:
    """文字列がASCII文字のみか（英数字ハンドル名の判定用）"""
    try:
        s.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


# Pixiv の自動生成ユーザー名パターン (user_xxxx1234)
_PIXIV_USER_PATTERN = re.compile(r'^user_[a-z0-9]+$', re.IGNORECASE)


def is_pixiv_user_id(name: str) -> bool:
    """user_rhhm7283 のような Pixiv ユーザーID パターンか判定"""
    return bool(_PIXIV_USER_PATTERN.match(name))


def build_move_tags(
    remove_tags: List[str],
    all_tags: List[str],
) -> List[str]:
    """
    削除対象の creator: タグから、情報保全用のタグを生成する。
    - Twitter ソース + ASCII名 → twitter_user:xxx
    - Pixiv ソース or user_ パターン → pixiv_user:xxx
    """
    move_tags = []
    is_twitter = 'source:twitter' in all_tags
    is_pixiv = 'source:pixiv' in all_tags
    for r in remove_tags:
        name = r.replace('creator:', '')
        if is_ascii_only(name) and is_twitter:
            move_tags.append(f"twitter_user:{name}")
        elif is_pixiv or is_pixiv_user_id(name):
            move_tags.append(f"pixiv_user:{name}")
    return move_tags


def extract_creator_tags(tags: List[str]) -> List[str]:
    """タグリストから creator: タグのみ抽出"""
    return [t for t in tags if t.startswith('creator:')]


def extract_source_tags(tags: List[str]) -> List[str]:
    """タグリストから source: タグのみ抽出"""
    return [t for t in tags if t.startswith('source:')]


async def fetch_all_creator_files(client: HydrusClient) -> Dict[str, List[str]]:
    """
    Hydrus から creator: タグを持つ全ファイルを取得し、
    ハッシュ → creator:タグリスト のマッピングを返す。
    known_urls も同時に取得する。
    """
    import aiohttp

    print("Phase 1: Hydrus からデータを収集中...")

    headers = client._get_headers()

    # creator: タグを持つファイルを検索
    search_tags = ['creator:*']
    params = {
        'tags': json.dumps(search_tags),
        'file_sort_type': 6,  # import time
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
                print(f"  {wait}秒後にリトライ...")
                await asyncio.sleep(wait)
            else:
                print(f"エラー: ファイル検索に5回失敗。中断します")
                return {}

    file_ids = data.get('file_ids', [])
    print(f"  creator: タグを持つファイル: {len(file_ids)} 件")

    if not file_ids:
        return {}

    # バッチでメタデータを取得
    hash_to_creators: Dict[str, List[str]] = {}
    hash_to_all_tags: Dict[str, List[str]] = {}
    hash_to_urls: Dict[str, List[str]] = {}
    batch_size = 256

    for i in range(0, len(file_ids), batch_size):
        batch = file_ids[i:i + batch_size]
        progress = min(i + batch_size, len(file_ids))
        print(f"  メタデータ取得中... {progress}/{len(file_ids)}", end='\r')

        params = {
            'file_ids': json.dumps(batch),
        }

        max_retries = 5
        for attempt in range(max_retries):
            try:
                async with client.session.get(
                    f"{client.api_url}/get_files/file_metadata",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        print(f"\n  警告: バッチ {i} のメタデータ取得に失敗 (HTTP {resp.status})")
                        break
                    data = await resp.json()
                break  # 成功
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"\n  ネットワークエラー (batch {i}, retry {attempt+1}/{max_retries}): {e}")
                    print(f"  {wait}秒後にリトライ...")
                    await asyncio.sleep(wait)
                else:
                    print(f"\n  エラー: バッチ {i} のメタデータ取得に{max_retries}回失敗。スキップ")
                    data = {'metadata': []}  # 空データでスキップ

        for metadata in data.get('metadata', []):
            file_hash = metadata.get('hash')
            if not file_hash:
                continue

            all_tags = client._extract_display_tags_from_metadata(metadata)
            creator_tags = extract_creator_tags(all_tags)

            if len(creator_tags) >= 2:
                hash_to_creators[file_hash] = creator_tags
                hash_to_all_tags[file_hash] = all_tags
                hash_to_urls[file_hash] = metadata.get('known_urls', [])

    print(f"\n  複数 creator: タグを持つファイル: {len(hash_to_creators)} 件")

    return hash_to_creators, hash_to_all_tags, hash_to_urls


TWITTER_URL_PATTERN = re.compile(
    r'https?://(?:x\.com|twitter\.com)/([^/]+)/status/', re.IGNORECASE
)

PIXIV_URL_PATTERN = re.compile(
    r'https?://www\.pixiv\.net/(?:en/)?users/(\d+)', re.IGNORECASE
)


def extract_twitter_usernames_from_urls(urls: List[str]) -> Set[str]:
    """known_urls から Twitter/X の URL のユーザー名を抽出（小文字化して返す）"""
    usernames = set()
    for url in urls:
        m = TWITTER_URL_PATTERN.match(url)
        if m:
            usernames.add(m.group(1).lower())
    return usernames


def extract_csv_usernames_from_urls(urls: List[str]) -> Set[str]:
    """known_urls から CSV の username 列に対応する値を抽出する。
    Twitter → screen_name（小文字）、Pixiv → user_id。"""
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


async def auto_resolve_by_url(
    hash_to_creators: Dict[str, List[str]],
    hash_to_all_tags: Dict[str, List[str]],
    hash_to_urls: Dict[str, List[str]],
    client: HydrusClient,
    apply: bool = False,
) -> Tuple[int, List[Tuple[str, str]]]:
    """
    URL情報を使って自動判別可能な creator: タグを一括処理する。

    条件:
      - ファイルの known_urls に Twitter/X URL が含まれる
      - Twitter URL から抽出されるユニークユーザー名が1つだけ
      - そのユーザー名と一致する creator: タグが存在する

    処理:
      - 一致する creator: タグを削除し twitter_user: に移動
      - 残りの creator: タグはそのまま維持

    Returns:
        (自動処理されたファイル数, display_name変更リスト[(csv_username, new_display_name), ...])
    """
    resolved_count = 0
    display_name_changes: List[Tuple[str, str]] = []
    # username(小文字) → 処理対象ハッシュ群の集約
    resolve_groups: Dict[str, Dict] = defaultdict(lambda: {
        'hashes': [], 'creator_tag': '', 'keep_tags': [],
    })

    for file_hash, creators in hash_to_creators.items():
        urls = hash_to_urls.get(file_hash, [])
        if not urls:
            continue

        twitter_users = extract_twitter_usernames_from_urls(urls)
        if len(twitter_users) != 1:
            continue

        twitter_username = twitter_users.pop()

        # creator: タグの中から Twitter ユーザー名と一致するものを探す
        matching_tag = None
        for c in creators:
            name = c.replace('creator:', '')
            if name.lower() == twitter_username:
                matching_tag = c
                break

        if not matching_tag:
            continue

        remaining_creators = [c for c in creators if c != matching_tag]
        key = matching_tag.lower()
        resolve_groups[key]['hashes'].append(file_hash)
        resolve_groups[key]['creator_tag'] = matching_tag
        resolve_groups[key]['keep_tags'] = remaining_creators

    if not resolve_groups:
        return 0, []

    total_files = sum(len(g['hashes']) for g in resolve_groups.values())
    print(f"\nPhase 1.5: URL自動判別")
    print(f"  自動判別可能: {total_files} ファイル ({len(resolve_groups)} ユーザー名)")

    bg_tasks: List[asyncio.Task] = []

    for key, group in sorted(resolve_groups.items(), key=lambda x: -len(x[1]['hashes'])):
        creator_tag = group['creator_tag']
        hashes = group['hashes']
        name = creator_tag.replace('creator:', '')
        twitter_user_tag = f"twitter_user:{name}"

        keep_info = ', '.join(group['keep_tags'][:3])
        if len(group['keep_tags']) > 3:
            keep_info += '...'
        print(f"    {creator_tag} → {twitter_user_tag} ({len(hashes)} 枚, 残: {keep_info})")

        if apply:
            task = asyncio.create_task(
                _bg_apply_group(client, None, [creator_tag], [twitter_user_tag])
            )
            bg_tasks.append(task)

        # display_name 変更を記録（URLからCSVのusernameを特定）
        group_usernames: Set[str] = set()
        for h in hashes:
            group_usernames.update(
                extract_csv_usernames_from_urls(hash_to_urls.get(h, []))
            )
        if group['keep_tags']:
            new_display = group['keep_tags'][0].replace('creator:', '')
            for csv_username in group_usernames:
                display_name_changes.append((csv_username, new_display))

        resolved_count += len(hashes)

    # バックグラウンドタスク完了待ち
    if bg_tasks:
        pending = [t for t in bg_tasks if not t.done()]
        if pending:
            print(f"  適用待機中... ({len(pending)} 件)")
        results = await asyncio.gather(*bg_tasks, return_exceptions=True)
        total_success = 0
        total_errors = 0
        for r in results:
            if isinstance(r, Exception):
                total_errors += 1
                print(f"    エラー: {r}")
            else:
                total_success += r[0]
                total_errors += r[1]
        print(f"  自動判別適用結果: 成功 {total_success}, エラー {total_errors}")
    elif not apply:
        print(f"  [dry-run] 実際に適用するには --apply を付けてください")

    return resolved_count, display_name_changes


def group_by_creator_combination(
    hash_to_creators: Dict[str, List[str]],
    tag_count: Optional[int] = None,
) -> List[Tuple[frozenset, List[str]]]:
    """
    同じ creator タグの組み合わせを持つファイルをグループ化。
    ファイル数が多い順にソート。
    tag_count が指定された場合、そのタグ数のグループのみ返す。
    """
    combo_to_hashes: Dict[frozenset, List[str]] = defaultdict(list)

    for file_hash, creators in hash_to_creators.items():
        key = frozenset(creators)
        if tag_count is not None and len(key) != tag_count:
            continue
        combo_to_hashes[key].append(file_hash)

    # ファイル数の多い順にソート
    groups = sorted(combo_to_hashes.items(), key=lambda x: -len(x[1]))
    return groups


def get_tag_count_distribution(
    hash_to_creators: Dict[str, List[str]]
) -> List[Tuple[int, int]]:
    """
    タグ個数ごとのファイル数を返す。[(タグ数, ファイル数), ...] をタグ数昇順で。
    """
    count_dist: Dict[int, int] = defaultdict(int)
    for creators in hash_to_creators.values():
        count_dist[len(creators)] += 1
    return sorted(count_dist.items())


def suggest_canonical(creators: List[str]) -> int:
    """
    正しい creator 名を推測し、推奨インデックス（0始まり）を返す。
    - 日本語を含むものを優先
    - 長い名前を優先（短い英数字はID的）
    """
    scores = []
    for c in creators:
        name = c.replace('creator:', '')
        score = 0
        # 日本語を含むものを大きく優先
        if not is_ascii_only(name):
            score += 100
        # 長い名前を優先
        score += len(name)
        # 数字のみはペナルティ
        if name.isdigit():
            score -= 200
        scores.append(score)

    return scores.index(max(scores))


async def search_files_by_tag(client: HydrusClient, tag: str) -> List[str]:
    """指定タグを持つ全ファイルのハッシュを取得する"""
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


async def apply_group_changes(
    client: HydrusClient,
    keep_tag: Optional[str],
    remove_tags: List[str],
    add_tags: List[str],
    quiet: bool = False,
) -> Tuple[int, int]:
    """
    1グループ分の変更をHydrusに即時適用する。
    削除対象タグを持つ全ファイルをHydrusから検索し、タグの差し替えを行う。
    これにより、重複ファイルだけでなく削除対象タグしか持たないファイルも処理される。
    """
    total_success = 0
    total_errors = 0

    for tag in remove_tags:
        # このタグを持つ全ファイルを検索
        hashes = await search_files_by_tag(client, tag)
        if not hashes:
            continue

        if not quiet:
            print(f"    {tag} を持つファイル: {len(hashes)} 枚")

        # タグ削除
        ok = await client.remove_tags_bulk(hashes, [tag], all_services=True)
        if not ok:
            total_errors += len(hashes)
            continue

        # 正しいcreatorタグを付与（keep_tagがある場合）
        tags_to_add = []
        if keep_tag:
            tags_to_add.append(keep_tag)
        # twitter_user: 等の追加タグ
        for at in add_tags:
            if at != keep_tag:
                tags_to_add.append(at)

        if tags_to_add:
            ok = await client.add_tags_bulk(hashes, tags_to_add)
            if not ok:
                total_errors += len(hashes)
                continue

        total_success += len(hashes)

    return total_success, total_errors


async def async_input(prompt: str) -> str:
    """非同期版input()。event loopをブロックせずユーザー入力を待つ。"""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


async def _bg_apply_group(
    client: HydrusClient,
    keep_tag: Optional[str],
    remove_tags: List[str],
    add_tags: List[str],
) -> Tuple[int, int]:
    """バックグラウンドでタグ操作をサイレント実行する。"""
    return await apply_group_changes(client, keep_tag, remove_tags, add_tags, quiet=True)


async def _bg_bulk_remove(
    client: HydrusClient,
    hashes: List[str],
    tags: List[str],
) -> Tuple[int, int]:
    """バックグラウンドで全削除をサイレント実行する。"""
    ok = await client.remove_tags_bulk(hashes, tags, all_services=True)
    s = len(hashes) if ok else 0
    e = 0 if ok else len(hashes)
    return s, e


async def interactive_review(
    groups: List[Tuple[frozenset, List[str]]],
    hash_to_all_tags: Dict[str, List[str]],
    hash_to_urls: Dict[str, List[str]],
    client: HydrusClient,
    apply: bool = False,
    resume: bool = False,
) -> Tuple[List[dict], List[Tuple[str, str]], bool]:
    """
    対話的にレビューし、変更リストを返す。
    --apply時はタグ操作をバックグラウンドで実行し、入力待ちをブロックしない。

    Returns:
        (変更操作リスト, display_name変更リスト[(csv_username, new_display_name), ...],
         ユーザーがq/Ctrl+Cで中断したか)
    """
    changes = []
    display_name_changes: List[Tuple[str, str]] = []  # (csv_username, new_display_name)
    bg_tasks: List[asyncio.Task] = []
    total = len(groups)

    # 進捗読み込み
    completed_groups: Set[str] = set()
    if resume:
        completed_groups = load_progress()

    # 未処理グループをフィルタ
    pending_groups = []
    skipped = 0
    for combo, hashes in groups:
        key = combo_key(combo)
        if key in completed_groups:
            skipped += 1
        else:
            pending_groups.append((combo, hashes))

    if skipped > 0:
        print(f"\n  前回の進捗から {skipped} グループをスキップ")

    remaining = len(pending_groups)
    print(f"\nPhase 2: 対話的レビュー ({remaining} グループ未処理 / 全 {total} グループ)")
    print("=" * 60)
    print("操作: 番号=選択  Enter=推奨を選択  s=スキップ  q=終了")
    print("      a=全削除（creator:を全部消す）  m=手動入力")
    if apply:
        print("      ※ タグ操作はバックグラウンドで実行されます")
    print("=" * 60)

    user_quit = False

    try:
        for idx, (combo, hashes) in enumerate(pending_groups):
            creators = sorted(combo)
            suggested = suggest_canonical(creators)
            file_count = len(hashes)

            first_hash = hashes[0]
            all_tags = hash_to_all_tags.get(first_hash, [])
            sources = extract_source_tags(all_tags)
            source_info = ', '.join(s.replace('source:', '') for s in sources) if sources else '不明'

            print(f"\n[{skipped + idx + 1}/{total}] {file_count} 枚 (source: {source_info})")
            for i, c in enumerate(creators):
                marker = " ←推奨" if i == suggested else ""
                print(f"  [{i + 1}] {c}{marker}")

            group_changes = []
            kept_name = None
            removed_names = []

            while True:
                choice = (await async_input(
                    f"選択 (Enter={suggested + 1}/s=スキップ/q=終了/m=自由入力): "
                )).strip().lower()

                if choice == 'q':
                    print("レビューを終了します。")
                    user_quit = True
                    break
                elif choice == 's':
                    break
                elif choice == 'a':
                    for h in hashes:
                        group_changes.append({
                            'hash': h,
                            'remove_tags': list(creators),
                            'add_tags': [],
                        })
                    print(f"  → 全 creator: タグを {file_count} 枚から削除予定")
                    break
                elif choice == 'm':
                    new_name = (await async_input("  正しい creator 名を入力: ")).strip()
                    if new_name:
                        keep_tag = f"creator:{new_name}"
                        remove = [c for c in creators if c != keep_tag]
                        add = [keep_tag] if keep_tag not in creators else []
                        move_tags = build_move_tags(remove, all_tags)
                        for h in hashes:
                            group_changes.append({
                                'hash': h,
                                'remove_tags': remove,
                                'add_tags': add + move_tags,
                            })
                        kept_name = new_name
                        removed_names = [r.replace('creator:', '') for r in remove]
                        print(f"  → creator:{new_name} を設定、他を削除 ({file_count} 枚)")
                        break
                    else:
                        print("  名前が空です。もう一度入力してください。")
                elif choice == '' or choice.isdigit():
                    num = suggested if choice == '' else int(choice) - 1
                    if 0 <= num < len(creators):
                        keep = creators[num]
                        remove = [c for c in creators if c != keep]

                        move_tags = build_move_tags(remove, all_tags)

                        for h in hashes:
                            group_changes.append({
                                'hash': h,
                                'remove_tags': remove,
                                'add_tags': move_tags,
                            })

                        kept_name = keep.replace('creator:', '')
                        removed_names = [r.replace('creator:', '') for r in remove]
                        move_info = f" + {', '.join(move_tags)} を追加" if move_tags else ""
                        print(f"  → {keep} を残し、{', '.join(remove)} を削除{move_info} ({file_count} 枚)")
                        break
                    else:
                        print(f"  1-{len(creators)} の番号を入力してください。")
                else:
                    print("  無効な入力です。番号/Enter/s/q/a/m を入力してください。")

            if user_quit:
                break

            if group_changes:
                changes.extend(group_changes)

                # display_name変更を記録（URLからCSVのusernameを特定）
                if kept_name:
                    # グループ内のファイルURLからCSVのusernameを特定
                    group_usernames: Set[str] = set()
                    for h in hashes:
                        group_usernames.update(
                            extract_csv_usernames_from_urls(hash_to_urls.get(h, []))
                        )
                    for csv_username in group_usernames:
                        display_name_changes.append((csv_username, kept_name))

                # --apply時はバックグラウンドで適用
                if apply:
                    if kept_name:
                        keep = f"creator:{kept_name}"
                        remove = [f"creator:{n}" for n in removed_names]
                        add = group_changes[0]['add_tags']
                        task = asyncio.create_task(
                            _bg_apply_group(client, keep, remove, add)
                        )
                    else:
                        all_hashes = [c['hash'] for c in group_changes]
                        remove = group_changes[0]['remove_tags']
                        task = asyncio.create_task(
                            _bg_bulk_remove(client, all_hashes, remove)
                        )
                    bg_tasks.append(task)

                # 進捗保存
                key = combo_key(combo)
                completed_groups.add(key)
                save_progress(completed_groups)

    except KeyboardInterrupt:
        print(f"\n\n中断されました。進捗は保存済みです（{len(completed_groups)} グループ処理済み）。")
        user_quit = True

    # バックグラウンドタスクの完了を待つ
    if bg_tasks:
        pending = [t for t in bg_tasks if not t.done()]
        if pending:
            print(f"\n残り {len(pending)} 件の適用処理を待機中...")
        results = await asyncio.gather(*bg_tasks, return_exceptions=True)
        total_success = 0
        total_errors = 0
        error_details = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                error_details.append(f"  タスク {i + 1}: {r}")
                total_errors += 1
            else:
                total_success += r[0]
                total_errors += r[1]

        print(f"\n合計適用結果: 成功 {total_success}, エラー {total_errors}")
        if error_details:
            print("エラー詳細:")
            for detail in error_details:
                print(detail)
            # エラーログをファイルにも保存
            error_log_path = LOGS_DIR / "creator_cleanup_errors.log"
            with open(error_log_path, 'a', encoding='utf-8') as f:
                f.write(f"\n--- {__import__('datetime').datetime.now().isoformat()} ---\n")
                for detail in error_details:
                    f.write(detail + '\n')
            print(f"  エラーログ: {error_log_path}")

    return changes, display_name_changes, user_quit


async def apply_changes(
    client: HydrusClient,
    changes: List[dict],
    dry_run: bool = True,
) -> None:
    """変更を適用する"""
    if not changes:
        print("\n変更はありません。")
        return

    # 変更サマリーを計算
    total_removes = sum(len(c['remove_tags']) for c in changes)
    total_adds = sum(len(c['add_tags']) for c in changes)
    unique_hashes = len(set(c['hash'] for c in changes))

    print(f"\n{'=' * 60}")
    print(f"変更サマリー:")
    print(f"  対象ファイル数: {unique_hashes}")
    print(f"  削除タグ操作数: {total_removes}")
    print(f"  追加タグ操作数: {total_adds}")
    print(f"  モード: {'dry-run（変更なし）' if dry_run else '適用'}")
    print(f"{'=' * 60}")

    if dry_run:
        # dry-run: 変更内容をログ出力
        log_path = LOGS_DIR / "creator_tag_cleanup_dryrun.log"
        with open(log_path, 'w', encoding='utf-8') as f:
            for c in changes:
                f.write(f"HASH: {c['hash']}\n")
                for tag in c['remove_tags']:
                    f.write(f"  REMOVE: {tag}\n")
                for tag in c['add_tags']:
                    f.write(f"  ADD: {tag}\n")
                f.write("\n")
        print(f"\ndry-run ログ: {log_path}")
        print("実際に適用するには --apply フラグを付けて再実行してください。")
        return

    # 実際に適用
    success = 0
    errors = 0

    for i, c in enumerate(changes):
        file_hash = c['hash']
        print(f"  適用中... {i + 1}/{len(changes)}", end='\r')

        # タグ削除
        if c['remove_tags']:
            ok = await client.remove_tags(file_hash, c['remove_tags'], all_services=True)
            if not ok:
                errors += 1
                continue

        # タグ追加
        if c['add_tags']:
            ok = await client.add_tags(file_hash, c['add_tags'])
            if not ok:
                errors += 1
                continue

        success += 1

        # レート制限対策
        if (i + 1) % 50 == 0:
            await asyncio.sleep(0.5)

    print(f"\n完了: 成功 {success}, エラー {errors}")

    # 適用ログ保存
    log_path = LOGS_DIR / "creator_tag_cleanup_applied.log"
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write(f"適用日時: {__import__('datetime').datetime.now().isoformat()}\n")
        f.write(f"成功: {success}, エラー: {errors}\n\n")
        for c in changes:
            f.write(f"HASH: {c['hash']}\n")
            for tag in c['remove_tags']:
                f.write(f"  REMOVE: {tag}\n")
            for tag in c['add_tags']:
                f.write(f"  ADD: {tag}\n")
            f.write("\n")
    print(f"適用ログ: {log_path}")


def update_monitored_csv(display_name_changes: List[Tuple[str, str]]) -> None:
    """
    monitored_accounts.csv の display_name を更新する。
    display_name_changes: [(csv_username, new_display_name), ...]
    usernameで行を特定し、display_nameを上書きする。
    """
    if not display_name_changes:
        return

    csv_path = PROJECT_ROOT / "monitored_accounts.csv"
    if not csv_path.exists():
        print("\n警告: monitored_accounts.csv が見つかりません。display_name の更新をスキップします。")
        return

    # 変更マップを構築（username → new_display_name）
    # 同じusernameに対して複数の変更がある場合は最後のものを使用
    change_map: Dict[str, str] = {}
    for csv_username, new_display in display_name_changes:
        change_map[csv_username] = new_display

    if not change_map:
        return

    # CSV読み込み
    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # usernameで行を特定し display_name を更新
    updated_count = 0
    for row in rows:
        username = row.get('username', '')
        if username in change_map:
            old_display = row.get('display_name', '')
            new_display = change_map[username]
            if old_display != new_display:
                print(f"  CSV更新: {username} の display_name: {old_display} → {new_display}")
                row['display_name'] = new_display
                updated_count += 1

    if updated_count == 0:
        print("\nCSVに更新対象の display_name はありませんでした。")
        return

    # CSV書き戻し
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV更新完了: {updated_count} アカウントの display_name を更新しました。")


async def export_csv(
    groups: List[Tuple[frozenset, List[str]]],
    hash_to_all_tags: Dict[str, List[str]],
) -> None:
    """グループ情報をCSV出力"""
    csv_path = LOGS_DIR / "creator_tag_duplicates.csv"

    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['group_id', 'file_count', 'creator_tags', 'sources', 'suggested'])

        for idx, (combo, hashes) in enumerate(groups):
            creators = sorted(combo)
            suggested_idx = suggest_canonical(creators)
            suggested = creators[suggested_idx]

            first_hash = hashes[0]
            all_tags = hash_to_all_tags.get(first_hash, [])
            sources = '|'.join(s.replace('source:', '') for s in extract_source_tags(all_tags))

            writer.writerow([
                idx + 1,
                len(hashes),
                '|'.join(creators),
                sources,
                suggested,
            ])

    print(f"\nCSV出力: {csv_path}")
    print(f"  {len(groups)} グループ")


async def main():
    parser = argparse.ArgumentParser(description='Hydrus creator: タグ重複整理ツール')
    parser.add_argument('--apply', action='store_true', help='変更を実際に適用する')
    parser.add_argument('--export', action='store_true', help='CSV出力のみ')
    parser.add_argument('--resume', action='store_true', help='前回の続きから再開する')
    args = parser.parse_args()

    config = load_config()

    async with HydrusClient(config) as client:
        if not client.enabled:
            print("エラー: Hydrus連携が無効です。config.yaml を確認してください。")
            return

        # データ収集
        result = await fetch_all_creator_files(client)
        if not result or not result[0]:
            print("複数 creator: タグを持つファイルはありません。")
            return

        hash_to_creators, hash_to_all_tags, hash_to_urls = result

        if args.export:
            groups = group_by_creator_combination(hash_to_creators)
            print(f"  creator タグ組み合わせのグループ数: {len(groups)}")
            await export_csv(groups, hash_to_all_tags)
            return

        all_display_name_changes: List[Tuple[str, str]] = []  # (csv_username, new_display_name)

        # Phase 1.5: URL自動判別（対話レビュー前に一括実行）
        auto_resolved, auto_dn_changes = await auto_resolve_by_url(
            hash_to_creators, hash_to_all_tags, hash_to_urls,
            client, apply=args.apply,
        )
        all_display_name_changes.extend(auto_dn_changes)

        confirmed_tag_counts: Set[int] = {2}  # 2個は確認不要

        # 毎ラウンド再取得し、最小タグ数のグループから処理するループ
        is_first_fetch = True
        while True:
            # データ取得（初回で自動判別した場合も再取得、2回目以降も再取得）
            if not is_first_fetch or auto_resolved > 0:
                print("\nデータを再取得中...")
                result = await fetch_all_creator_files(client)
                if not result or not result[0]:
                    print("処理完了: 複数 creator: タグを持つファイルはありません。")
                    break
                hash_to_creators, hash_to_all_tags, hash_to_urls = result
            is_first_fetch = False

            # タグ個数の分布を表示
            dist = get_tag_count_distribution(hash_to_creators)
            print(f"\ncreator タグ個数の分布:")
            for tag_count, file_count in dist:
                print(f"  {tag_count} 個: {file_count} ファイル")

            # 最小タグ数のグループを取得
            min_tag_count = dist[0][0]
            groups = group_by_creator_combination(hash_to_creators, tag_count=min_tag_count)
            if not groups:
                break

            total_files = sum(len(h) for _, h in groups)

            # 3個以上で未確認のタグ数は確認を挟む
            if min_tag_count >= 3 and min_tag_count not in confirmed_tag_counts:
                print(f"\n{'=' * 60}")
                print(f"creator タグ {min_tag_count} 個のグループ: {len(groups)} グループ ({total_files} ファイル)")
                print(f"{'=' * 60}")
                confirm = input(f"処理を開始しますか？ (y/n): ").strip().lower()
                if confirm != 'y':
                    print("処理を終了します。")
                    break
                confirmed_tag_counts.add(min_tag_count)
            else:
                print(f"\ncreator タグ {min_tag_count} 個のグループ: {len(groups)} グループ ({total_files} ファイル)")

            # 対話的レビュー
            changes, display_name_changes, user_quit = await interactive_review(
                groups, hash_to_all_tags, hash_to_urls, client,
                apply=args.apply, resume=args.resume,
            )

            # dry-runモードではバッチでログ出力
            if not args.apply:
                await apply_changes(client, changes, dry_run=True)

            all_display_name_changes.extend(display_name_changes)

            if user_quit:
                break

        # CSV の display_name を更新
        if all_display_name_changes:
            update_monitored_csv(all_display_name_changes)


if __name__ == '__main__':
    asyncio.run(main())
