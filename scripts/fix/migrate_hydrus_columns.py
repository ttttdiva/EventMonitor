#!/usr/bin/env python3
"""
SQLiteのHydrus時刻カラムを削除する安全移行スクリプト。
対象: all_tweets / event_tweets の
  - hydrus_imported_at
  - hydrus_last_attempt_at
を削除する。
"""

import sqlite3
from datetime import datetime
from pathlib import Path


DB_PATH = Path("data/eventmonitor.db")
DROP_COLUMNS = {"hydrus_imported_at", "hydrus_last_attempt_at"}
TARGET_TABLES = ("all_tweets", "event_tweets")


def backup_db(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_suffix(f".db.bak_{timestamp}")
    backup_path.write_bytes(db_path.read_bytes())
    return backup_path


def get_index_sql(conn: sqlite3.Connection, table_name: str) -> list[str]:
    sqls: list[str] = []
    rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    for row in rows:
        index_name = row[1]
        origin = row[3]  # c=created, u=unique constraint, pk=primary key
        if origin != "c":
            continue
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        if sql_row and sql_row[0]:
            sqls.append(sql_row[0])
    return sqls


def rebuild_table(conn: sqlite3.Connection, table_name: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    keep_columns = [c for c in columns if c[1] not in DROP_COLUMNS]
    if len(keep_columns) == len(columns):
        return

    index_sqls = get_index_sql(conn, table_name)

    col_defs = []
    col_names = []
    for cid, name, col_type, notnull, default_value, pk in keep_columns:
        col_sql = f"{name} {col_type}".strip()
        if notnull:
            col_sql += " NOT NULL"
        if default_value is not None:
            col_sql += f" DEFAULT {default_value}"
        if pk:
            col_sql += " PRIMARY KEY"
        col_defs.append(col_sql)
        col_names.append(name)

    new_table = f"{table_name}_new"
    conn.execute(f"CREATE TABLE {new_table} ({', '.join(col_defs)})")
    cols_csv = ", ".join(col_names)
    conn.execute(
        f"INSERT INTO {new_table} ({cols_csv}) SELECT {cols_csv} FROM {table_name}"
    )
    conn.execute(f"DROP TABLE {table_name}")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO {table_name}")

    for sql in index_sqls:
        conn.execute(sql)


def main() -> None:
    if not DB_PATH.exists():
        print(f"DBが見つかりません: {DB_PATH}")
        return

    backup_path = backup_db(DB_PATH)
    print(f"バックアップ作成: {backup_path}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in TARGET_TABLES:
            rebuild_table(conn, table)
        conn.commit()
    finally:
        conn.close()

    print("移行完了")


if __name__ == "__main__":
    main()
