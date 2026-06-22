#!/usr/bin/env python3
"""
Twitter ID変更（スクリーンネーム変更）されたアカウントを検出・復旧するスクリプト。

flagged_accounts.json と deleted_accounts.csv のTwitterアカウントについて、
DBに保存された過去ツイートIDからtwscrapeで現在のauthor情報を取得し、
username変更を検出する。

Examples:
    # ドライラン（レポートのみ）
    python scripts/fix/recover_renamed_accounts.py

    # 特定アカウントのみ調査
    python scripts/fix/recover_renamed_accounts.py --username siezer_freek

    # flaggedのみ / deletedのみ
    python scripts/fix/recover_renamed_accounts.py --flagged-only
    python scripts/fix/recover_renamed_accounts.py --deleted-only

    # 自動修正を適用（CSV更新、フラグ解除、deleted→monitored復元）
    python scripts/fix/recover_renamed_accounts.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env", override=True)

from twscrape import API

TWITTER_TABLES = ("all_tweets", "event_tweets", "log_only_tweets")
MONITORED_CSV = PROJECT_ROOT / "monitored_accounts.csv"
DELETED_CSV = PROJECT_ROOT / "deleted_accounts.csv"
FLAGGED_JSON = PROJECT_ROOT / "data" / "flagged_accounts.json"


def load_config() -> Dict[str, Any]:
    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_db_path(config: Dict[str, Any]) -> str:
    db_cfg = config.get("database", {})
    if db_cfg.get("type") != "sqlite":
        raise RuntimeError("このスクリプトはsqliteのみ対応しています")
    return db_cfg["path"]


def load_flagged_accounts() -> Dict[str, Dict[str, Any]]:
    """flagged_accounts.json からTwitterアカウントを読み込む"""
    if not FLAGGED_JSON.exists():
        return {}
    with open(FLAGGED_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    flagged = data.get("flagged", {})
    return {
        username: info
        for username, info in flagged.items()
        if info.get("platform", "twitter") == "twitter"
    }


def load_deleted_accounts() -> Dict[str, Dict[str, str]]:
    """deleted_accounts.csv からTwitterアカウントを読み込む"""
    if not DELETED_CSV.exists():
        return {}
    accounts = {}
    with open(DELETED_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            platform = (row.get("platform") or "").strip()
            if platform in ("", "twitter"):
                accounts[row["username"]] = dict(row)
    return accounts


def get_tweet_ids_for_user(conn: sqlite3.Connection, username: str, limit: int = 5) -> List[str]:
    """DBから指定ユーザーの最新ツイートIDを取得"""
    ids = set()
    for table in TWITTER_TABLES:
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE username = ? ORDER BY tweet_date DESC LIMIT ?",
            (username, limit),
        ).fetchall()
        for (tweet_id,) in rows:
            ids.add(str(tweet_id))
    return sorted(ids, reverse=True)[:limit]


async def initialize_api() -> API:
    """twscrape APIを初期化（TwitterMonitorと同じパターン）"""
    from src.twitter_monitor import TwitterMonitor

    config = load_config()
    monitor = TwitterMonitor(config)
    await monitor._initialize_accounts()
    return monitor.api


async def check_account_via_tweets(
    api: API,
    username: str,
    tweet_ids: List[str],
) -> Dict[str, Any]:
    """ツイートIDからアカウントの現在の状態を調査"""
    for tweet_id in tweet_ids:
        try:
            tweet = await asyncio.wait_for(
                api.tweet_details(int(tweet_id)),
                timeout=30,
            )
            if tweet is None:
                continue

            current_username = tweet.user.username
            twitter_id = tweet.user.id
            display_name = tweet.user.displayname

            if current_username.lower() != username.lower():
                return {
                    "status": "RENAMED",
                    "old_username": username,
                    "new_username": current_username,
                    "twitter_id": twitter_id,
                    "display_name": display_name,
                    "checked_tweet": tweet_id,
                }
            else:
                return {
                    "status": "STILL_EXISTS",
                    "username": username,
                    "twitter_id": twitter_id,
                    "display_name": display_name,
                    "checked_tweet": tweet_id,
                }
        except asyncio.TimeoutError:
            print(f"  [WARN] ツイート {tweet_id} のチェックがタイムアウト")
            continue
        except Exception as e:
            print(f"  [WARN] ツイート {tweet_id} のチェック失敗: {e}")
            continue

    return {"status": "DELETED", "username": username}


def apply_rename_to_monitored_csv(old_username: str, new_username: str, display_name: str, twitter_id: int) -> bool:
    """monitored_accounts.csv のusernameを更新（display_nameは変更しない）"""
    if not MONITORED_CSV.exists():
        return False

    rows = []
    updated = False
    with MONITORED_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if "twitter_id" not in fieldnames:
            fieldnames.append("twitter_id")
        for row in reader:
            if row.get("username") == old_username:
                row["username"] = new_username
                # display_name は既存値を維持（手動管理のため上書きしない）
                row["twitter_id"] = str(twitter_id)
                updated = True
            rows.append(row)

    if updated:
        with MONITORED_CSV.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return updated


def restore_from_deleted_csv(
    username: str,
    new_username: str,
    display_name: str,
    twitter_id: int,
    deleted_row: Dict[str, str],
) -> bool:
    """deleted_accounts.csv から行を削除し、monitored_accounts.csv に復元"""
    # deleted_accounts.csv から該当行を削除
    if not DELETED_CSV.exists():
        return False

    rows = []
    removed = False
    with DELETED_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        deleted_fieldnames = list(reader.fieldnames or [])
        for row in reader:
            if row.get("username") == username:
                removed = True
            else:
                rows.append(row)

    if not removed:
        return False

    # deleted_accounts.csv を書き戻し
    with DELETED_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=deleted_fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # monitored_accounts.csv に追記
    monitored_fieldnames = ["username", "display_name", "notification", "account_type", "platform", "custom_tags", "rank", "twitter_id"]
    file_exists = MONITORED_CSV.exists()

    # 既存ファイルのfieldnamesを読む
    if file_exists:
        with MONITORED_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                monitored_fieldnames = list(reader.fieldnames)
                if "twitter_id" not in monitored_fieldnames:
                    monitored_fieldnames.append("twitter_id")

    with MONITORED_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=monitored_fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "username": new_username,
            "display_name": display_name,
            "notification": deleted_row.get("notification", ""),
            "account_type": deleted_row.get("account_type", ""),
            "platform": deleted_row.get("platform", ""),
            "custom_tags": deleted_row.get("custom_tags", ""),
            "rank": deleted_row.get("rank", ""),
            "twitter_id": str(twitter_id),
        })

    return True


def remove_from_flagged(username: str) -> bool:
    """flagged_accounts.json から該当エントリを削除"""
    if not FLAGGED_JSON.exists():
        return False

    with open(FLAGGED_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    if username not in data.get("flagged", {}):
        return False

    del data["flagged"][username]

    with open(FLAGGED_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return True


async def main():
    parser = argparse.ArgumentParser(description="ID変更されたTwitterアカウントを検出・復旧")
    parser.add_argument("--apply", action="store_true", help="検出結果を自動適用（CSV更新、フラグ解除）")
    parser.add_argument("--username", type=str, help="特定のusernameのみ調査")
    parser.add_argument("--flagged-only", action="store_true", help="flagged_accounts.json のみ調査")
    parser.add_argument("--deleted-only", action="store_true", help="deleted_accounts.csv のみ調査")
    args = parser.parse_args()

    config = load_config()
    db_path = get_db_path(config)
    conn = sqlite3.connect(PROJECT_ROOT / db_path)

    # 対象アカウント収集
    targets: Dict[str, Dict[str, Any]] = {}  # username -> {"source": "flagged"|"deleted", "info": ...}

    if not args.deleted_only:
        flagged = load_flagged_accounts()
        for username, info in flagged.items():
            targets[username] = {"source": "flagged", "info": info}

    if not args.flagged_only:
        deleted = load_deleted_accounts()
        for username, info in deleted.items():
            if username not in targets:  # flagged優先
                targets[username] = {"source": "deleted", "info": info}

    if args.username:
        if args.username in targets:
            targets = {args.username: targets[args.username]}
        else:
            # 指定されたusernameがどちらにもない場合でもDB検索は試みる
            targets = {args.username: {"source": "manual", "info": {}}}

    if not targets:
        print("調査対象のアカウントがありません")
        conn.close()
        return

    print(f"\n{'='*60}")
    print(f"Twitter ID変更検出スクリプト")
    print(f"対象アカウント数: {len(targets)}")
    print(f"モード: {'自動適用' if args.apply else 'ドライラン（レポートのみ）'}")
    print(f"{'='*60}\n")

    # twscrape API初期化
    print("twscrape API を初期化中...")
    api = await initialize_api()
    print("初期化完了\n")

    # 結果集計
    results = {"RENAMED": [], "DELETED": [], "STILL_EXISTS": [], "NO_TWEETS": []}

    for username, target_info in targets.items():
        source = target_info["source"]
        info = target_info["info"]
        display_name = info.get("display_name", "")

        print(f"--- @{username} (display: {display_name}, source: {source}) ---")

        # DB からツイートID取得
        tweet_ids = get_tweet_ids_for_user(conn, username)
        if not tweet_ids:
            print(f"  結果: NO_TWEETS（DBにツイート記録なし）")
            results["NO_TWEETS"].append({
                "username": username,
                "source": source,
                "display_name": display_name,
            })
            continue

        print(f"  DB内ツイートID: {len(tweet_ids)}件")

        # twscrapeで調査
        result = await check_account_via_tweets(api, username, tweet_ids)
        status = result["status"]

        if status == "RENAMED":
            print(f"  結果: RENAMED  @{username} → @{result['new_username']} "
                  f"(twitter_id={result['twitter_id']}, display_name={result['display_name']})")
            result["source"] = source
            result["deleted_info"] = info
            results["RENAMED"].append(result)

        elif status == "STILL_EXISTS":
            print(f"  結果: STILL_EXISTS "
                  f"(twitter_id={result['twitter_id']}, display_name={result['display_name']})")
            result["source"] = source
            results["STILL_EXISTS"].append(result)

        elif status == "DELETED":
            print(f"  結果: DELETED（全ツイートがアクセス不能）")
            result["source"] = source
            result["display_name"] = display_name
            results["DELETED"].append(result)

        # API負荷軽減
        await asyncio.sleep(1)

    conn.close()

    # サマリー出力
    print(f"\n{'='*60}")
    print(f"結果サマリー")
    print(f"{'='*60}")
    print(f"  RENAMED      : {len(results['RENAMED'])}件")
    print(f"  DELETED      : {len(results['DELETED'])}件")
    print(f"  STILL_EXISTS : {len(results['STILL_EXISTS'])}件")
    print(f"  NO_TWEETS    : {len(results['NO_TWEETS'])}件")

    if results["RENAMED"]:
        print(f"\n--- RENAMED アカウント ---")
        for r in results["RENAMED"]:
            print(f"  @{r['old_username']} → @{r['new_username']} "
                  f"(id={r['twitter_id']}, name={r['display_name']}, source={r['source']})")

    if results["STILL_EXISTS"]:
        print(f"\n--- STILL_EXISTS アカウント（一時凍結の解除等） ---")
        for r in results["STILL_EXISTS"]:
            print(f"  @{r['username']} (id={r['twitter_id']}, name={r['display_name']}, source={r['source']})")

    if results["DELETED"]:
        print(f"\n--- DELETED アカウント ---")
        for r in results["DELETED"]:
            print(f"  @{r['username']} (source={r['source']})")

    if results["NO_TWEETS"]:
        print(f"\n--- NO_TWEETS アカウント ---")
        for r in results["NO_TWEETS"]:
            print(f"  @{r['username']} (source={r['source']})")

    # --apply で自動修正
    if args.apply and (results["RENAMED"] or results["STILL_EXISTS"]):
        print(f"\n{'='*60}")
        print(f"自動修正を適用中...")
        print(f"{'='*60}")

        for r in results["RENAMED"]:
            old_username = r["old_username"]
            new_username = r["new_username"]
            display_name = r["display_name"]
            twitter_id = r["twitter_id"]
            source = r["source"]

            if source == "flagged":
                # monitored_accounts.csv のusernameを更新
                if apply_rename_to_monitored_csv(old_username, new_username, display_name, twitter_id):
                    print(f"  [CSV更新] @{old_username} → @{new_username} (monitored_accounts.csv)")
                else:
                    print(f"  [SKIP] @{old_username} がmonitored_accounts.csvに見つからない")
                # flagged_accounts.json から削除
                if remove_from_flagged(old_username):
                    print(f"  [フラグ解除] @{old_username} (flagged_accounts.json)")

            elif source == "deleted":
                # deleted_accounts.csv から復元
                deleted_info = r.get("deleted_info", {})
                if restore_from_deleted_csv(old_username, new_username, display_name, twitter_id, deleted_info):
                    print(f"  [復元] @{old_username} → @{new_username} (deleted → monitored)")
                else:
                    print(f"  [SKIP] @{old_username} がdeleted_accounts.csvに見つからない")

        for r in results["STILL_EXISTS"]:
            username = r["username"]
            source = r["source"]

            if source == "flagged":
                if remove_from_flagged(username):
                    print(f"  [フラグ解除] @{username} (到達可能と確認)")
            elif source == "deleted":
                deleted = load_deleted_accounts()
                deleted_info = deleted.get(username, {})
                twitter_id = r["twitter_id"]
                display_name = r["display_name"]
                if restore_from_deleted_csv(username, username, display_name, twitter_id, deleted_info):
                    print(f"  [復元] @{username} (deleted → monitored)")

        print("\n適用完了")
    elif not args.apply and (results["RENAMED"] or results["STILL_EXISTS"]):
        print(f"\n変更を適用するには --apply フラグを付けて再実行してください")


if __name__ == "__main__":
    asyncio.run(main())
