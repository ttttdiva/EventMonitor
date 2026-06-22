#!/usr/bin/env python3
"""
cookiesフォルダのCookieファイルから.envにTWITTER_ACCOUNT環境変数を追記するスクリプト

使用方法:
    python scripts/maintenance/sync_cookies.py

これにより、cookies/x.com_cookies_*.txt からauth_tokenとct0を抽出し、
.envに TWITTER_ACCOUNT_*_TOKEN と TWITTER_ACCOUNT_*_CT0 として追記します。
"""

import sys
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.cookie_manager import CookieManager


def sync_cookies_to_env():
    """cookiesフォルダから.envにTWITTER_ACCOUNT環境変数を追記"""
    
    env_file = project_root / ".env"
    
    # CookieManagerでクレデンシャルを取得
    cm = CookieManager()
    credentials = cm.get_all_cookie_credentials()
    
    if not credentials:
        print("No cookie files found in cookies/ folder")
        return
    
    print(f"Found {len(credentials)} cookie files")
    
    # 既存の.envを読み込み
    existing_lines = []
    if env_file.exists():
        with open(env_file, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()
    
    # 既存のTWITTER_ACCOUNT_*行を削除（重複を避けるため）
    filtered_lines = []
    removed_count = 0
    for line in existing_lines:
        if line.strip().startswith('TWITTER_ACCOUNT_') and ('_TOKEN=' in line or '_CT0=' in line):
            removed_count += 1
            continue
        filtered_lines.append(line)
    
    if removed_count > 0:
        print(f"Removed {removed_count} existing TWITTER_ACCOUNT_* lines")
    
    # 末尾の空行を整理
    while filtered_lines and filtered_lines[-1].strip() == '':
        filtered_lines.pop()
    
    # 新しい環境変数を追加
    new_lines = ["\n", "# Twitter Accounts (auto-generated from cookies/ folder)\n"]
    
    for i, (username, auth_token, ct0) in enumerate(credentials, start=1):
        new_lines.append(f"TWITTER_ACCOUNT_{i}_TOKEN={auth_token}\n")
        new_lines.append(f"TWITTER_ACCOUNT_{i}_CT0={ct0}\n")
        print(f"  Added TWITTER_ACCOUNT_{i}_TOKEN and TWITTER_ACCOUNT_{i}_CT0")
    
    # .envを書き込み
    with open(env_file, 'w', encoding='utf-8') as f:
        f.writelines(filtered_lines)
        f.writelines(new_lines)
    
    print(f"\nSuccessfully updated {env_file}")
    print(f"Total: {len(credentials)} accounts configured")


if __name__ == "__main__":
    sync_cookies_to_env()
