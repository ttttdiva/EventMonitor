#!/usr/bin/env python3
"""
Kemono の過去取りこぼし作品を artwork_retry_queue に戻すスクリプト。

指定したアカウントについて Kemono の全作品一覧を 1 回取得し、
DB 未保存かつ retry queue 未登録の作品だけを queue に積む。

使い方:
  python scripts/fix/backfill_kemono_retry_queue.py
  python scripts/fix/backfill_kemono_retry_queue.py --log-only
  python scripts/fix/backfill_kemono_retry_queue.py --user "fanbox/43115256"
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.database import DatabaseManager
from src.kemono_extractor import KemonoExtractor


logger = logging.getLogger("BackfillKemonoRetryQueue")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kemono の未保存作品を artwork_retry_queue に戻す"
    )
    parser.add_argument(
        "--user",
        action="append",
        dest="users",
        default=[],
        help='対象アカウント。例: "fanbox/43115256"',
    )
    parser.add_argument(
        "--log-only",
        action="store_true",
        help="monitor ではなく log-only 側の retry queue に積む",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.yaml",
        help="config.yaml のパス",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    return args


def load_config(config_path: Path) -> Dict:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_all_kemono_users(csv_path: Path) -> List[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"monitored_accounts.csv が見つかりません: {csv_path}")

    users: List[str] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 5:
                continue
            username = (row[0] or "").strip()
            platform = (row[4] or "").strip().lower()
            if username and platform == "kemono":
                users.append(username)
    return users


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def resolve_target_users(args: argparse.Namespace) -> List[str]:
    users = list(args.users) if args.users else load_all_kemono_users(
        PROJECT_ROOT / "monitored_accounts.csv"
    )
    users = dedupe_keep_order(user.strip() for user in users if user and user.strip())
    if not users:
        raise ValueError("対象の kemono アカウントが 0 件です")
    return users


def queued_ids_for_account(
    db_manager: DatabaseManager,
    account_id: str,
    *,
    is_log_only: bool,
) -> set[str]:
    queued_works = db_manager.get_artwork_retry_works(
        "kemono",
        account_id,
        is_log_only=is_log_only,
    )
    return {
        work.get("id")
        for work in queued_works
        if isinstance(work, dict) and work.get("id")
    }


def enqueue_missing_works(
    db_manager: DatabaseManager,
    account_id: str,
    works: Sequence[Dict],
    *,
    is_log_only: bool,
) -> int:
    queued = 0
    for work in works:
        db_manager.upsert_artwork_retry(
            "kemono",
            account_id,
            work,
            is_log_only=is_log_only,
            error="backfill_missing_work",
        )
        queued += 1
    return queued


def backfill_account(
    extractor: KemonoExtractor,
    db_manager: DatabaseManager,
    account_id: str,
    *,
    is_log_only: bool,
) -> Dict[str, int]:
    works = extractor.fetch_user_works(account_id)
    if not works:
        raise RuntimeError("作品一覧を取得できませんでした")

    existing_ids = db_manager.get_existing_post_ids(account_id, "kemono")
    retry_ids = queued_ids_for_account(
        db_manager,
        account_id,
        is_log_only=is_log_only,
    )

    missing_works: List[Dict] = []
    skipped_invalid = 0
    for work in works:
        work_id = work.get("id")
        if not work_id:
            skipped_invalid += 1
            continue
        if work_id in existing_ids or work_id in retry_ids:
            continue
        missing_works.append(work)

    queued = enqueue_missing_works(
        db_manager,
        account_id,
        missing_works,
        is_log_only=is_log_only,
    )

    return {
        "fetched": len(works),
        "existing": len(existing_ids),
        "already_queued": len(retry_ids),
        "enqueued": queued,
        "invalid": skipped_invalid,
    }


def print_account_result(account_id: str, stats: Dict[str, int]) -> None:
    print(
        f"{account_id}: fetched={stats['fetched']} "
        f"existing={stats['existing']} "
        f"already_queued={stats['already_queued']} "
        f"enqueued={stats['enqueued']} "
        f"invalid={stats['invalid']}"
    )


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
        users = resolve_target_users(args)
    except Exception as e:
        print(f"ERROR: 初期化失敗: {e}")
        return 1

    db_manager = DatabaseManager(config)
    extractor = KemonoExtractor(config)

    scope_label = "log-only" if args.log_only else "monitor"
    print(f"Kemono retry queue バックフィル開始: users={len(users)} scope={scope_label}")

    total = {
        "accounts": 0,
        "fetched": 0,
        "existing": 0,
        "already_queued": 0,
        "enqueued": 0,
        "invalid": 0,
        "failed": 0,
    }

    for index, account_id in enumerate(users, start=1):
        print(f"[{index}/{len(users)}] {account_id}")
        try:
            stats = backfill_account(
                extractor,
                db_manager,
                account_id,
                is_log_only=args.log_only,
            )
        except Exception as e:
            total["failed"] += 1
            print(f"{account_id}: ERROR: {e}")
            continue

        print_account_result(account_id, stats)
        total["accounts"] += 1
        total["fetched"] += stats["fetched"]
        total["existing"] += stats["existing"]
        total["already_queued"] += stats["already_queued"]
        total["enqueued"] += stats["enqueued"]
        total["invalid"] += stats["invalid"]

    print(
        "SUMMARY: "
        f"accounts={total['accounts']} "
        f"failed={total['failed']} "
        f"fetched={total['fetched']} "
        f"existing={total['existing']} "
        f"already_queued={total['already_queued']} "
        f"enqueued={total['enqueued']} "
        f"invalid={total['invalid']}"
    )

    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
