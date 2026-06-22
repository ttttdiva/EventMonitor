#!/usr/bin/env python3
"""
Sync existing Twitter records using account-level sensitive settings.

This script:
1. Resolves current `user.possibly_sensitive` for target X accounts.
2. Marks existing Twitter records as `sensitive=1` in SQLite.
3. Adds `rating:r-18` to matching Hydrus files found by tweet URL.

Examples:
    python scripts/fix/sync_twitter_account_sensitive.py --dry-run
    python scripts/fix/sync_twitter_account_sensitive.py --username CostRa777
    python scripts/fix/sync_twitter_account_sensitive.py --db-only
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env", override=True)

from src.database import DatabaseManager
from src.event_detector import EventDetector
from src.hydrus_client import HydrusClient
from src.twitter_monitor import TwitterMonitor


TWITTER_TABLES = ("all_tweets", "event_tweets", "log_only_tweets")


def load_config() -> Dict[str, Any]:
    with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_db_path(config: Dict[str, Any]) -> str:
    db_cfg = config.get("database", {})
    if db_cfg.get("type") != "sqlite":
        raise RuntimeError("This script currently supports sqlite only.")
    return db_cfg["path"]


def iter_distinct_usernames(conn: sqlite3.Connection) -> Iterable[str]:
    seen = set()
    for table in TWITTER_TABLES:
        rows = conn.execute(f"SELECT DISTINCT username FROM {table}").fetchall()
        for (username,) in rows:
            if username and username not in seen:
                seen.add(username)
                yield username


async def resolve_account_sensitive(
    usernames: List[str],
    config: Dict[str, Any],
) -> Dict[str, bool]:
    db_manager = DatabaseManager(config)
    event_detector = EventDetector(config)
    monitor = TwitterMonitor(config, db_manager, event_detector)
    resolved: Dict[str, bool] = {}

    try:
        await monitor._initialize_accounts()
        for username in usernames:
            try:
                resolved[username] = await monitor._resolve_account_sensitive(username)
                print(f"[account] @{username}: account_sensitive={resolved[username]}")
            except Exception as exc:
                resolved[username] = False
                print(f"[account] @{username}: failed to resolve ({exc})")
    finally:
        await monitor.cleanup()

    return resolved


def update_sensitive_flags(
    conn: sqlite3.Connection,
    usernames: List[str],
    dry_run: bool,
) -> Dict[str, int]:
    stats: Dict[str, int] = {f"{table}_updated": 0 for table in TWITTER_TABLES}

    for table in TWITTER_TABLES:
        for username in usernames:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE username = ? AND (sensitive = 0 OR sensitive IS NULL)",
                (username,),
            ).fetchone()[0]
            stats[f"{table}_updated"] += count
            if count and not dry_run:
                conn.execute(
                    f"UPDATE {table} SET sensitive = 1 WHERE username = ? AND (sensitive = 0 OR sensitive IS NULL)",
                    (username,),
                )

    if not dry_run:
        conn.commit()

    return stats


def load_tweets_for_hydrus(conn: sqlite3.Connection, usernames: List[str]) -> List[Dict[str, str]]:
    tweets: Dict[str, Dict[str, str]] = {}
    placeholders = ",".join("?" for _ in usernames)
    for table in TWITTER_TABLES:
        query = (
            f"SELECT id, username, tweet_url FROM {table} "
            f"WHERE username IN ({placeholders}) AND tweet_url IS NOT NULL AND tweet_url != ''"
        )
        for tweet_id, username, tweet_url in conn.execute(query, usernames).fetchall():
            tweets[str(tweet_id)] = {
                "id": str(tweet_id),
                "username": username,
                "tweet_url": tweet_url,
            }
    return sorted(tweets.values(), key=lambda item: item["id"])


async def sync_hydrus_tags(
    records: List[Dict[str, str]],
    config: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, int]:
    stats = {
        "tweets_checked": 0,
        "files_found": 0,
        "files_not_found": 0,
        "files_already_tagged": 0,
        "files_tagged": 0,
        "files_failed": 0,
    }

    async with HydrusClient(config) as hydrus:
        if not hydrus.enabled:
            print("[hydrus] disabled, skipping tag sync")
            return stats

        for record in records:
            stats["tweets_checked"] += 1
            hashes = await hydrus.search_files_by_url(record["tweet_url"])
            if not hashes:
                stats["files_not_found"] += 1
                continue

            stats["files_found"] += len(hashes)
            for file_hash in hashes:
                existing_tags = await hydrus._get_file_tags(file_hash)
                if existing_tags is None:
                    stats["files_failed"] += 1
                    continue

                if "rating:r-18" in existing_tags:
                    stats["files_already_tagged"] += 1
                    continue

                if dry_run:
                    stats["files_tagged"] += 1
                    continue

                ok = await hydrus.add_tags(file_hash, ["rating:r-18"], platform="twitter")
                if ok:
                    stats["files_tagged"] += 1
                else:
                    stats["files_failed"] += 1

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync existing Twitter records for account-level sensitive settings."
    )
    parser.add_argument("--username", nargs="+", help="Specific Twitter usernames to process.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing.")
    parser.add_argument("--db-only", action="store_true", help="Update SQLite only.")
    parser.add_argument("--hydrus-only", action="store_true", help="Sync Hydrus tags only.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if args.db_only and args.hydrus_only:
        print("Choose only one of --db-only or --hydrus-only.")
        return 2

    config = load_config()
    db_path = get_db_path(config)
    conn = sqlite3.connect(db_path)

    try:
        usernames = args.username or list(iter_distinct_usernames(conn))
        if not usernames:
            print("No Twitter usernames found.")
            return 0

        resolved = await resolve_account_sensitive(usernames, config)
        sensitive_users = [username for username, is_sensitive in resolved.items() if is_sensitive]

        print("")
        print(f"Checked users: {len(usernames)}")
        print(f"Account-sensitive users: {len(sensitive_users)}")

        if not sensitive_users:
            print("No account-sensitive users found.")
            return 0

        if not args.hydrus_only:
            db_stats = update_sensitive_flags(conn, sensitive_users, dry_run=args.dry_run)
            print("")
            print("[db]")
            for key, value in db_stats.items():
                print(f"  {key}: {value}")

        if not args.db_only:
            records = load_tweets_for_hydrus(conn, sensitive_users)
            hydrus_stats = await sync_hydrus_tags(records, config, dry_run=args.dry_run)
            print("")
            print("[hydrus]")
            for key, value in hydrus_stats.items():
                print(f"  {key}: {value}")

        if args.dry_run:
            print("")
            print("Dry-run only. No changes were written.")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
