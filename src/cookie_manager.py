#!/usr/bin/env python3
"""
Twitter Cookie管理ユーティリティ
"""

import os
import logging
from pathlib import Path


class CookieManager:
    """Twitter Cookie管理クラス"""
    
    def __init__(self):
        self.logger = logging.getLogger("EventMonitor.CookieManager")

    def get_cookie_files(self) -> list[Path]:
        """利用可能なcookieファイルのリストを取得"""
        cookies_dir = Path('cookies')
        if not cookies_dir.exists():
            self.logger.warning("cookies directory does not exist")
            return []
        
        # x.com_cookies*.txt パターンのファイルを検索
        patterns = [
            "x.com_cookies.txt",
            "x.com_cookies_*.txt"
        ]
        
        cookie_files = []
        for pattern in patterns:
            cookie_files.extend(cookies_dir.glob(pattern))
        
        # 重複を除去してソート
        cookie_files = sorted(list(set(cookie_files)))
        
        return cookie_files

    def parse_cookie_file(self, cookie_file: Path) -> dict[str, str] | None:
        """
        Netscape形式のCookieファイルからauth_tokenとct0を抽出
        
        Args:
            cookie_file: Cookieファイルのパス
            
        Returns:
            {"auth_token": "xxx", "ct0": "yyy"} または None
        """
        if not cookie_file.exists():
            self.logger.warning(f"Cookie file not found: {cookie_file}")
            return None
        
        auth_token = None
        ct0 = None
        
        try:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # コメント行やヘッダーをスキップ
                    if not line or line.startswith('#'):
                        continue
                    
                    # Netscape形式: domain \t flag \t path \t secure \t expiry \t name \t value
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        name = parts[5]
                        value = parts[6]
                        
                        if name == 'auth_token':
                            auth_token = value
                        elif name == 'ct0':
                            ct0 = value
                    
                    # 両方見つかったら終了
                    if auth_token and ct0:
                        break
            
            if auth_token and ct0:
                self.logger.debug(f"Extracted credentials from {cookie_file.name}")
                return {"auth_token": auth_token, "ct0": ct0}
            else:
                self.logger.warning(f"Missing credentials in {cookie_file.name}: auth_token={bool(auth_token)}, ct0={bool(ct0)}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error parsing cookie file {cookie_file}: {e}")
            return None

    def get_all_cookie_credentials(self) -> list[tuple[str, str, str]]:
        """
        すべてのCookieファイルからクレデンシャルを取得
        
        Returns:
            [(username, auth_token, ct0), ...] のリスト
            usernameはファイル名から生成される識別子
        """
        credentials = []
        cookie_files = self.get_cookie_files()
        
        for i, cookie_file in enumerate(cookie_files, start=1):
            parsed = self.parse_cookie_file(cookie_file)
            if parsed:
                # ファイル名からユーザー名を生成（例: x.com_cookies_01.txt -> cookie_user_01）
                # ファイル番号を抽出
                filename = cookie_file.stem  # x.com_cookies_01
                if '_' in filename:
                    parts = filename.rsplit('_', 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        username = f"cookie_user_{parts[1]}"
                    else:
                        username = f"cookie_user_{i:02d}"
                else:
                    username = f"cookie_user_{i:02d}"
                
                credentials.append((username, parsed["auth_token"], parsed["ct0"]))
                self.logger.debug(f"Loaded credentials from {cookie_file.name} as {username}")
        
        return credentials