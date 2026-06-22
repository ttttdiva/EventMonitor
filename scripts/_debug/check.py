#!/usr/bin/env python3
"""デバッグ: 対象tweet IDのDB状態、Hydrus状態を確認"""
import sys, os, sqlite3, json, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / '.env', override=True)

DB_PATH = 'data/eventmonitor.db'
TARGET_ID = '2020948354637930861'
TARGET_USERNAME = 'youyumekun'

def check_db():
    conn = sqlite3.connect(DB_PATH)

    # テーブル一覧
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"テーブル一覧: {[t[0] for t in tables]}")
    
    # all_tweets のカラム取得
    cols = conn.execute("PRAGMA table_info(all_tweets)").fetchall()
    col_names = [c[1] for c in cols]
    print(f"\nall_tweets カラム: {col_names}")

    # ID型確認
    c = conn.execute("SELECT typeof(id) FROM all_tweets LIMIT 1")
    row = c.fetchone()
    print(f"id の型: {row}")
    
    # youyumekun のレコード確認
    c = conn.execute(
        "SELECT id, username, sensitive, substr(local_media, 1, 300) FROM all_tweets WHERE username = ?",
        (TARGET_USERNAME,)
    )
    rows = c.fetchall()
    print(f"\n{TARGET_USERNAME} のレコード数: {len(rows)}")
    for r in rows[:5]:
        print(f"  ID={r[0]}, sensitive={r[2]}, media={r[3]}")

    # 数値・文字列両方で検索
    for tid in [TARGET_ID, int(TARGET_ID)]:
        c = conn.execute("SELECT id, username, sensitive FROM all_tweets WHERE id = ?", (tid,))
        result = c.fetchall()
        print(f"\nall_tweets WHERE id={tid} (type={type(tid).__name__}): {result}")

    # event_tweets
    for tid in [TARGET_ID, int(TARGET_ID)]:
        try:
            c = conn.execute("SELECT id, username, sensitive FROM event_tweets WHERE id = ?", (tid,))
            result = c.fetchall()
            print(f"event_tweets WHERE id={tid}: {result}")
        except Exception as e:
            print(f"event_tweets エラー: {e}")

    # log_only_tweets
    for tid in [TARGET_ID, int(TARGET_ID)]:
        try:
            c = conn.execute("SELECT id, username, sensitive FROM log_only_tweets WHERE id = ?", (tid,))
            result = c.fetchall()
            print(f"log_only_tweets WHERE id={tid}: {result}")
        except Exception as e:
            print(f"log_only_tweets エラー: {e}")

    conn.close()

check_db()
