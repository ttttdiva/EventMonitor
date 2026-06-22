#!/usr/bin/env python3
"""Search tweet text stored in the local EventMonitor database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "eventmonitor.db"

TABLES = {
    "all": ("all_tweets",),
    "event": ("event_tweets",),
    "log": ("log_only_tweets",),
    "all-tweets": ("all_tweets",),
    "event-tweets": ("event_tweets",),
    "log-only": ("log_only_tweets",),
    "all-tables": ("all_tweets", "event_tweets", "log_only_tweets"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DBに保存済みのツイート本文を検索します。",
        epilog=(
            "例: search.bat コミケ / "
            "search.bat コミケ --username akiba --table event --limit 20"
        ),
    )
    parser.add_argument("query", nargs="*", help="検索語。複数指定した場合は空白で連結します。")
    parser.add_argument(
        "-t",
        "--table",
        choices=sorted(TABLES),
        default="all-tables",
        help="検索対象。既定は all-tables です。",
    )
    parser.add_argument("-u", "--username", help="@なしのユーザー名で絞り込みます。")
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=30,
        help="最大表示件数。既定は30件です。",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DBファイルパス。")
    parser.add_argument(
        "--full",
        action="store_true",
        help="本文を省略せずに表示します。",
    )
    return parser.parse_args()


def build_sql(table_names: tuple[str, ...], has_username: bool) -> str:
    selects = []
    for table_name in table_names:
        where = ["tweet_text LIKE :query"]
        if has_username:
            where.append("username = :username")
        selects.append(
            f"""
            SELECT
                :table_{len(selects)} AS source_table,
                id,
                username,
                display_name,
                tweet_date,
                tweet_url,
                tweet_text
            FROM {table_name}
            WHERE {" AND ".join(where)}
            """
        )

    return "\nUNION ALL\n".join(selects) + "\nORDER BY tweet_date DESC\nLIMIT :limit"


def compact_text(value: str, full: bool) -> str:
    normalized = " ".join((value or "").split())
    if full or len(normalized) <= 220:
        return normalized
    return normalized[:217] + "..."


def main() -> int:
    args = parse_args()
    query = " ".join(args.query).strip()
    if not query:
        print("検索語を指定してください。例: search.bat コミケ")
        return 2

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"DBが見つかりません: {db_path}", file=sys.stderr)
        return 1

    table_names = TABLES[args.table]
    params = {
        "query": f"%{query}%",
        "limit": max(args.limit, 1),
    }
    for index, table_name in enumerate(table_names):
        params[f"table_{index}"] = table_name
    if args.username:
        params["username"] = args.username.lstrip("@")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(build_sql(table_names, bool(args.username))),
                params,
            ).mappings().all()
    except SQLAlchemyError as exc:
        print(f"検索に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(f'query="{query}" table={args.table} hits={len(rows)} db={db_path}')
    for row in rows:
        display_name = f' ({row["display_name"]})' if row["display_name"] else ""
        print()
        print(f'[{row["source_table"]}] {row["tweet_date"]} @{row["username"]}{display_name}')
        print(row["tweet_url"])
        print(compact_text(row["tweet_text"], args.full))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
