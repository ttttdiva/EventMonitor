"""
Pixivタグ復元 + 余計なURL削除スクリプト

Phase 1: pixiv artworks URLがあるのに source:pixiv / pixiv_id: が欠落しているファイルに
         URLからpixiv_idを抽出してタグを付与する
Phase 2: 全ファイルから不要な i.pximg.net URL と pixiv.net/en/artworks/ URL を削除する

Usage:
    python scripts/fix/fix_pixiv_tags_and_urls.py --dry-run   # 確認のみ
    python scripts/fix/fix_pixiv_tags_and_urls.py --apply      # 実適用
"""

import argparse
import asyncio
import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import aiohttp

API_URL = "http://127.0.0.1:45869"
API_KEY = os.environ.get("HYDRUS_ACCESS_KEY", "")

# .env から読む
if not API_KEY:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("HYDRUS_ACCESS_KEY="):
                    API_KEY = line.strip().split("=", 1)[1]
                    break

BATCH_SIZE = 50  # メタデータ取得バッチ
TAG_BATCH_SIZE = 200  # タグ一括操作バッチ
URL_BATCH_SIZE = 100  # URL削除バッチ

PIXIV_ARTWORKS_RE = re.compile(r"pixiv\.net/artworks/(\d+)")


def get_headers():
    return {
        "Hydrus-Client-API-Access-Key": API_KEY,
        "Content-Type": "application/json",
    }


async def search_files(session: aiohttp.ClientSession, tags: list) -> list:
    """Hydrusファイル検索"""
    async with session.get(
        f"{API_URL}/get_files/search_files",
        headers=get_headers(),
        params={"tags": json.dumps(tags), "return_hashes": "true"},
    ) as resp:
        data = await resp.json()
        return data.get("hashes", [])


async def get_metadata_batch(session: aiohttp.ClientSession, hashes: list) -> list:
    """メタデータをバッチ取得"""
    async with session.get(
        f"{API_URL}/get_files/file_metadata",
        headers=get_headers(),
        params={"hashes": json.dumps(hashes)},
    ) as resp:
        data = await resp.json()
        return data.get("metadata", [])


async def add_tags_bulk(session: aiohttp.ClientSession, hashes: list, tags: list) -> bool:
    """複数ファイルにタグ一括追加（my tags）"""
    data = {
        "hashes": hashes,
        "service_keys_to_actions_to_tags": {
            "6c6f63616c2074616773": {"0": tags}  # my tags
        },
        "override_previously_deleted_mappings": True,
    }
    async with session.post(
        f"{API_URL}/add_tags/add_tags",
        headers=get_headers(),
        json=data,
    ) as resp:
        return resp.status == 200


async def delete_urls(session: aiohttp.ClientSession, file_hash: str, urls: list) -> bool:
    """ファイルからURL一括削除"""
    data = {
        "hash": file_hash,
        "urls_to_delete": urls,
    }
    async with session.post(
        f"{API_URL}/add_urls/associate_url",
        headers=get_headers(),
        json=data,
    ) as resp:
        return resp.status == 200


def extract_pixiv_ids_from_urls(urls: list) -> set:
    """URLリストからpixiv artwork IDを抽出"""
    ids = set()
    for url in urls:
        m = PIXIV_ARTWORKS_RE.search(url)
        if m:
            ids.add(m.group(1))
    return ids


def classify_urls(urls: list) -> dict:
    """URLを分類して、削除対象を返す"""
    to_delete = []
    for url in urls:
        if "i.pximg.net" in url:
            to_delete.append(url)
        elif "pixiv.net/en/artworks/" in url:
            to_delete.append(url)
    return to_delete


async def phase1_fix_tags(session: aiohttp.ClientSession, apply: bool):
    """Phase 1: pixiv_id / source:pixiv タグ復元"""
    print("=" * 60)
    print("Phase 1: pixiv_id / source:pixiv タグ復元")
    print("=" * 60)

    # source:pixiv が欠落しているファイルを検索
    hashes = await search_files(session, [
        "system:has url matching regex pixiv\\.net/artworks/",
        "-source:pixiv",
    ])
    print(f"対象ファイル: {len(hashes)} 件")

    if not hashes:
        print("対象なし。スキップ。")
        return 0

    # pixiv_id ごとにグルーピング
    # artwork_id -> [hash, ...]
    id_to_hashes: dict[str, list[str]] = {}
    processed = 0

    for i in range(0, len(hashes), BATCH_SIZE):
        batch = hashes[i : i + BATCH_SIZE]
        metadata = await get_metadata_batch(session, batch)

        for meta in metadata:
            file_hash = meta.get("hash", "")
            urls = meta.get("known_urls", [])
            pixiv_ids = extract_pixiv_ids_from_urls(urls)

            for pid in pixiv_ids:
                if pid not in id_to_hashes:
                    id_to_hashes[pid] = []
                id_to_hashes[pid].append(file_hash)

        processed += len(batch)
        if processed % 500 == 0:
            print(f"  メタデータ取得中... {processed}/{len(hashes)}")

    print(f"  ユニーク pixiv_id: {len(id_to_hashes)} 件")

    # タグ追加: source:pixiv は全ファイルに、pixiv_id:{id} は各グループに
    tag_added = 0

    # まず source:pixiv を全ファイルに一括追加
    all_hashes = list(set(h for hs in id_to_hashes.values() for h in hs))
    print(f"  source:pixiv を {len(all_hashes)} ファイルに追加...")

    if apply:
        for i in range(0, len(all_hashes), TAG_BATCH_SIZE):
            batch = all_hashes[i : i + TAG_BATCH_SIZE]
            ok = await add_tags_bulk(session, batch, ["source:pixiv"])
            if not ok:
                print(f"  エラー: source:pixiv 追加失敗 (batch {i})")
            tag_added += len(batch)
            if tag_added % 1000 == 0:
                print(f"    source:pixiv 追加済み: {tag_added}/{len(all_hashes)}")
        print(f"  source:pixiv 追加完了: {tag_added} ファイル")
    else:
        print(f"  [dry-run] source:pixiv を {len(all_hashes)} ファイルに追加予定")

    # pixiv_id:{id} を各グループに追加
    id_added = 0
    # 同じ pixiv_id を持つハッシュをまとめてバッチ処理
    for pid, pid_hashes in id_to_hashes.items():
        tag = f"pixiv_id:{pid}"
        if apply:
            for i in range(0, len(pid_hashes), TAG_BATCH_SIZE):
                batch = pid_hashes[i : i + TAG_BATCH_SIZE]
                ok = await add_tags_bulk(session, batch, [tag])
                if not ok:
                    print(f"  エラー: {tag} 追加失敗")
        id_added += len(pid_hashes)
        if id_added % 1000 == 0:
            print(f"    pixiv_id タグ追加済み: {id_added}/{len(all_hashes)}")

    if apply:
        print(f"  pixiv_id タグ追加完了: {id_added} ファイル")
    else:
        print(f"  [dry-run] pixiv_id タグを {id_added} ファイルに追加予定")

    return len(all_hashes)


async def phase2_cleanup_urls(session: aiohttp.ClientSession, apply: bool):
    """Phase 2: 不要URL削除（pximg, /en/artworks/）"""
    print()
    print("=" * 60)
    print("Phase 2: 不要URL削除")
    print("=" * 60)

    # pximg URL を持つファイル
    pximg_hashes = await search_files(session, [
        "system:has url matching regex i\\.pximg\\.net",
    ])
    print(f"pximg URL持ち: {len(pximg_hashes)} 件")

    # /en/artworks/ URL を持つファイル
    en_hashes = await search_files(session, [
        "system:has url matching regex pixiv\\.net/en/artworks/",
    ])
    print(f"/en/artworks/ URL持ち: {len(en_hashes)} 件")

    # 両方を統合（重複排除）
    all_target = list(set(pximg_hashes + en_hashes))
    print(f"統合対象: {len(all_target)} 件")

    if not all_target:
        print("対象なし。スキップ。")
        return 0

    urls_deleted = 0
    files_processed = 0

    for i in range(0, len(all_target), BATCH_SIZE):
        batch = all_target[i : i + BATCH_SIZE]
        metadata = await get_metadata_batch(session, batch)

        for meta in metadata:
            file_hash = meta.get("hash", "")
            urls = meta.get("known_urls", [])
            to_delete = classify_urls(urls)

            if not to_delete:
                continue

            if apply:
                ok = await delete_urls(session, file_hash, to_delete)
                if not ok:
                    print(f"  エラー: URL削除失敗 {file_hash[:12]}")
            urls_deleted += len(to_delete)

        files_processed += len(batch)
        if files_processed % 1000 == 0:
            print(f"  処理中... {files_processed}/{len(all_target)} ({urls_deleted} URL削除)")

    if apply:
        print(f"  URL削除完了: {urls_deleted} URL ({files_processed} ファイル処理)")
    else:
        print(f"  [dry-run] {urls_deleted} URL を {files_processed} ファイルから削除予定")

    return urls_deleted


async def main():
    parser = argparse.ArgumentParser(description="Pixivタグ復元 + URL清掃")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="確認のみ")
    group.add_argument("--apply", action="store_true", help="実適用")
    args = parser.parse_args()

    apply = args.apply

    if apply:
        print("*** 実適用モード ***")
    else:
        print("*** dry-run モード ***")

    async with aiohttp.ClientSession() as session:
        # 接続確認
        try:
            async with session.get(
                f"{API_URL}/verify_access_key",
                headers=get_headers(),
            ) as resp:
                if resp.status != 200:
                    print(f"Hydrus API 接続失敗: {resp.status}")
                    return
                print("Hydrus API 接続OK")
        except Exception as e:
            print(f"Hydrus API 接続エラー: {e}")
            return

        tags_fixed = await phase1_fix_tags(session, apply)
        urls_cleaned = await phase2_cleanup_urls(session, apply)

        print()
        print("=" * 60)
        print("完了サマリー")
        print("=" * 60)
        print(f"  Phase 1: タグ復元 {tags_fixed} ファイル")
        print(f"  Phase 2: URL削除 {urls_cleaned} URL")


if __name__ == "__main__":
    asyncio.run(main())
