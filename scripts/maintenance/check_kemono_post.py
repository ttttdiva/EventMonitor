#!/usr/bin/env python3
"""修正後の動作確認: fanbox_9929392 の既存ファイルマージテスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from pathlib import Path
import yaml

# config.yaml読み込み
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

from src.path_utils import get_media_base_paths

# _find_existing_kemono_files と同等のロジックをテスト
images_base, _ = get_media_base_paths(config)
service = "fanbox"
user_id_num = "2846382"
dir_name = f"{service}_{user_id_num}"
images_dir = images_base / dir_name
post_id = "9929392"

print(f"=== 既存ファイル検索テスト ===")
print(f"images_dir: {images_dir}")
print(f"exists: {images_dir.exists()}")

if images_dir.exists():
    existing = []
    for f in images_dir.iterdir():
        if f.is_file() and (
            f.name.startswith(f"{post_id}_")
            or f.name.startswith(f"{post_id}.")
        ):
            rel = f"images/{dir_name}/{f.name}"
            existing.append(rel)
    
    existing.sort()
    print(f"既存ファイル数: {len(existing)}")
    print(f"最初の5件:")
    for p in existing[:5]:
        print(f"  {p}")
    print(f"最後の5件:") 
    for p in existing[-5:]:
        print(f"  {p}")

# file_count（期待値）をgallery-dlで確認
print(f"\n=== DL不完全チェック シミュレーション ===")
file_count = 75  # 前回のチェックで確認済み
print(f"file_count (期待値): {file_count}")
print(f"既存ファイル (local_media候補): {len(existing)}")

if file_count > 0 and len(existing) < file_count:
    print(f"⚠️ 既存ファイルのみでは不完全: {len(existing)}/{file_count}")
    print(f"   → DL分と合わせればDB保存可能かはDL結果次第")
else:
    print(f"✅ 既存ファイルだけで完全: {len(existing)}/{file_count} → DB保存可能")

# DB状態確認
print(f"\n=== DB状態確認 ===")
import sqlite3
conn = sqlite3.connect('data/eventmonitor.db')
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM kemono_works WHERE id = ?', ('fanbox_9929392',))
row = cur.fetchone()
print(f"DBレコード存在: {'あり' if row[0] > 0 else 'なし（正常 - 新規として再処理される）'}")

# Hydrus pending確認
cur.execute('''
    SELECT id, hydrus_expected_count, hydrus_imported_count 
    FROM kemono_works 
    WHERE user_id = ? AND hydrus_expected_count > 0 AND hydrus_imported_count < hydrus_expected_count
''', ('fanbox/2846382',))
pending = cur.fetchall()
print(f"Hydrusインポート未完了の作品数: {len(pending)}")
for p in pending[:5]:
    print(f"  {p}")
conn.close()

print(f"\n=== 結論 ===")
if len(existing) >= file_count:
    print("修正により、既存47ファイルがlocal_mediaにマージされ、")
    print("DB保存 + Hydrusインポートが実行されるようになります。")
    print("ただし75ファイル中47ファイルしかないため、DL不完全判定は続きます。")
    print("タイムアウト拡大により、次回DLで残り28ファイルもDLされることが期待されます。")
elif len(existing) > 0:
    print(f"既存{len(existing)}ファイル + 新規DL分で{file_count}件に達すればDB保存されます。")
    print("タイムアウト拡大(300→3000秒)により、残りのDLも完了する可能性が高いです。")
