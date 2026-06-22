#!/usr/bin/env python3
"""
Pixiv作品取得・メディアダウンロード
gallery-dlを使用してPixivの作品メタデータとメディアを取得
"""

import sys
import json
import subprocess
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from .path_utils import get_media_base_paths
from .subprocess_utils import run_with_idle_timeout


class PixivExtractor:
    """gallery-dlを使用してPixiv作品を取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.PixivExtractor")
        self.pixiv_config = config.get('pixiv', {})

        # OAuth refresh-token（.envから取得、なければ対話的に取得）
        self.refresh_token = os.environ.get('PIXIV_REFRESH_TOKEN', '')
        if not self.refresh_token:
            self.logger.warning("PIXIV_REFRESH_TOKEN not set. Starting OAuth flow...")
            self.refresh_token = self._run_oauth_flow()
            if not self.refresh_token:
                self.logger.error("Pixiv OAuth failed. Pixiv accounts will be skipped.")

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'pixiv'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------

    def _run_oauth_flow(self) -> str:
        """
        Pixiv OAuthフローを完全自動実行。
        undetected-chromedriverで自動ログイン → pixiv://スキームの
        コンソールログからcode取得 → refresh-tokenに交換 → .envに保存。
        """
        import hashlib
        import binascii
        import re
        import time
        from urllib.parse import urlparse, parse_qs

        try:
            from gallery_dl import util
            from gallery_dl.extractor import pixiv as pixiv_mod
        except ImportError:
            self.logger.error("gallery-dl is not installed")
            return ''

        try:
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            print()
            print("ERROR: 必要なパッケージがインストールされていません。")
            print("  pip install undetected-chromedriver")
            print()
            return ''

        # .envからPixiv資格情報を読み取り
        pixiv_email = None
        pixiv_password = None
        env_path = Path('.env')
        if env_path.exists():
            for line in env_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('# email:'):
                    pixiv_email = line.split(':', 1)[1].strip()
                elif line.startswith('# pass:'):
                    pixiv_password = line.split(':', 1)[1].strip()

        if not pixiv_email or not pixiv_password:
            self.logger.error("Pixiv credentials not found in .env")
            print()
            print("ERROR: .envにPixiv資格情報がありません。")
            print("  以下の形式でコメントとして記述してください:")
            print("  # email:your@email.com")
            print("  # pass:yourpassword")
            print()
            return ''

        # PKCE code_verifier / code_challenge 生成
        code_verifier = util.generate_token(32)
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = (
            binascii.b2a_base64(digest)[:-2]
            .decode()
            .replace("+", "-")
            .replace("/", "_")
        )

        login_url = (
            "https://app-api.pixiv.net/web/v1/login"
            f"?code_challenge={code_challenge}"
            "&code_challenge_method=S256"
            "&client=pixiv-android"
        )

        print()
        print("Pixiv OAuth: 自動ログイン中...")

        # Chromeバージョン自動検出
        chrome_ver = self._detect_chrome_version()

        driver = None
        try:
            kwargs = {}
            if chrome_ver:
                kwargs['version_main'] = chrome_ver
            driver = uc.Chrome(**kwargs)
        except Exception as e:
            self.logger.error(f"Browser launch failed: {e}")
            print(f"ERROR: ブラウザ起動失敗: {e}")
            print("  pip install -U undetected-chromedriver")
            return ''

        code = None
        try:
            driver.get(login_url)

            # ログインフォーム待機・入力
            email_input = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[autocomplete*='username']")
                )
            )
            email_input.clear()
            email_input.send_keys(pixiv_email)

            pass_input = driver.find_element(
                By.CSS_SELECTOR, "input[type='password']"
            )
            pass_input.clear()
            pass_input.send_keys(pixiv_password)

            # 「ログイン」ボタンをテキストで特定してクリック
            buttons = driver.find_elements(By.CSS_SELECTOR, "button")
            login_btn = None
            for btn in buttons:
                txt = btn.text.strip()
                if txt in ("ログイン", "Login"):
                    login_btn = btn
                    break
            if not login_btn:
                form = email_input.find_element(By.XPATH, "ancestor::form")
                login_btn = form.find_element(
                    By.CSS_SELECTOR, "button[type='submit']"
                )
            login_btn.click()

            # pixiv://スキームのcodeをコンソールログから取得
            # (Pixivはcallback URLではなくpixiv://でcodeを返す)
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    logs = driver.get_log("browser")
                    for log in logs:
                        msg = log.get("message", "")
                        if "pixiv://" in msg and "code=" in msg:
                            m = re.search(r"pixiv://[^\s'\"]+", msg)
                            if m:
                                parsed = urlparse(m.group(0))
                                code = parse_qs(parsed.query).get(
                                    "code", [None]
                                )[0]
                                if code:
                                    break
                except Exception:
                    pass
                if code:
                    break
                time.sleep(1)

            if not code:
                self.logger.error("Failed to capture OAuth code from browser logs")
                print("ERROR: OAuth codeを取得できませんでした。")
                return ''

            self.logger.info("OAuth code captured successfully")

        except Exception as e:
            self.logger.error(f"OAuth flow failed: {e}")
            print(f"ERROR: OAuth認証に失敗しました: {e}")
            return ''

        finally:
            try:
                driver.quit()
            except Exception:
                pass

        # refresh-tokenを取得
        token = self._exchange_code_for_token(
            code, code_verifier, pixiv_mod.PixivAppAPI
        )
        if not token:
            return ''

        # .envに自動保存
        self._save_token_to_env(token)
        os.environ['PIXIV_REFRESH_TOKEN'] = token

        print("refresh-token を .env に保存しました。")
        print()
        return token

    @staticmethod
    def _detect_chrome_version() -> int:
        """インストール済みChromeのメジャーバージョンを検出"""
        import re as _re
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Google\Chrome\BLBeacon"
            )
            ver, _ = winreg.QueryValueEx(key, "version")
            winreg.CloseKey(key)
            return int(ver.split('.')[0])
        except Exception:
            pass
        # レジストリから取れなければNone (undetected-chromedriverの自動検出に任せる)
        return None

    @staticmethod
    def _exchange_code_for_token(code: str, code_verifier: str, api_class) -> str:
        """OAuthコードをrefresh-tokenに交換"""
        import requests as req

        url = "https://oauth.secure.pixiv.net/auth/token"
        headers = {
            "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
        }
        data = {
            "client_id": api_class.CLIENT_ID,
            "client_secret": api_class.CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback",
        }

        try:
            resp = req.post(url, headers=headers, data=data)
            result = resp.json()
        except Exception as e:
            print(f"Token exchange failed: {e}")
            return ''

        if "error" in result:
            error_msg = result.get("error", "unknown")
            if error_msg in ("invalid_request", "invalid_grant"):
                print("\ncodeが失効しています。もう一度やり直してください。")
            else:
                print(f"\nエラー: {result}")
            return ''

        token = result.get("refresh_token", "")
        if not token:
            print("No refresh_token in response")
        return token

    @staticmethod
    def _save_token_to_env(token: str) -> None:
        """refresh-tokenを.envファイルに書き込む（既存行があれば上書き）"""
        env_path = Path('.env')

        if env_path.exists():
            lines = env_path.read_text(encoding='utf-8').splitlines()
        else:
            lines = []

        # 既存のPIXIV_REFRESH_TOKEN行を探して上書き
        found = False
        for i, line in enumerate(lines):
            if line.startswith('PIXIV_REFRESH_TOKEN'):
                lines[i] = f'PIXIV_REFRESH_TOKEN={token}'
                found = True
                break

        if not found:
            # なければ末尾に追加
            if lines and lines[-1].strip():
                lines.append('')  # 空行
            lines.append(f'PIXIV_REFRESH_TOKEN={token}')

        env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_user_works_by_artwork_id(self, artwork_id: str) -> List[Dict[str, Any]]:
        """
        Pixiv作品IDから作品メタデータ（user_id含む）を取得する。
        gallery-dlで作品URLを直接指定し、1件だけ取得してユーザー情報を抽出する。

        Args:
            artwork_id: Pixiv作品ID（数値文字列）

        Returns:
            作品情報のリスト（通常1件）。user_idは各要素の 'username' キーに格納。
        """
        if not self.refresh_token:
            self.logger.error("Cannot fetch Pixiv artwork: refresh-token not configured")
            return []

        url = f"https://www.pixiv.net/artworks/{artwork_id}"
        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '-o', f'extractor.pixiv.refresh-token={self.refresh_token}',
            '-v',
            '-j',
            '--range', '1-1',
            url,
        ]

        self.logger.info(f"Fetching Pixiv artwork {artwork_id} for user resolution")

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=60, rate_limit_retries=0)

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for Pixiv artwork {artwork_id}: "
                    f"returncode={result.returncode} stderr={result.stderr[:300]}"
                )
                return []

            output = result.stdout.strip()
            if not output:
                self.logger.info(f"No output from gallery-dl for Pixiv artwork {artwork_id}")
                return []

            return self._parse_gallery_dl_output(output, f"artwork:{artwork_id}")

        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout fetching Pixiv artwork {artwork_id}")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching Pixiv artwork {artwork_id}: {e}")
            return []

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """
        Pixiv数値ユーザーIDからdisplay name（表示名）を取得する。
        gallery-dlで作品を1件だけ取得し、メタデータ中の user.name を抽出する。

        Args:
            user_id: Pixiv数値ユーザーID

        Returns:
            display name（取得できた場合）、取得失敗時はNone
        """
        if not self.refresh_token:
            self.logger.warning("Cannot resolve Pixiv display name: refresh-token not configured")
            return None

        works = self.fetch_user_works(user_id, limit=1)
        if not works:
            self.logger.warning(f"No works found for Pixiv user {user_id}, cannot resolve display name")
            return None

        display_name = works[0].get('display_name', '')
        if display_name:
            self.logger.info(f"Resolved Pixiv user {user_id} -> {display_name}")
            return display_name

        self.logger.warning(f"Display name not found in metadata for Pixiv user {user_id}")
        return None

    # exitcode 0 でも発生するPixiv退会パターン（gallery-dlはNotFoundでも0を返す）
    _PIXIV_NOT_FOUND_PATTERNS = [
        "not found",
        "does not exist",
        "user has left",
        "notfounderror",
    ]

    # レートリミット・一時エラーパターン（not-found判定を無効化する）
    _PIXIV_RATE_LIMIT_PATTERNS = [
        "429",
        "too many requests",
        "rate limit",
        "retry after",
        "403",
        "forbidden",
    ]

    def _is_pixiv_rate_limited(self, stderr: str, stdout: str) -> bool:
        """レートリミット・一時的アクセス拒否かどうか"""
        s = (stderr or "").lower()
        o = (stdout or "").lower()
        return any(p in s or p in o for p in self._PIXIV_RATE_LIMIT_PATTERNS)

    def _is_pixiv_not_found(self, stderr: str, stdout: str) -> bool:
        """stderr/stdoutいずれかにPixiv退会/存在しないパターンが含まれるか。
        レートリミット時は誤検知を防ぐため必ずFalseを返す。"""
        if self._is_pixiv_rate_limited(stderr, stdout):
            return False
        s = (stderr or "").lower()
        o = (stdout or "").lower()
        matched = [p for p in self._PIXIV_NOT_FOUND_PATTERNS if p in s or p in o]
        if matched:
            self.logger.debug(f"Pixiv not-found patterns matched: {matched}")
        return bool(matched)

    def check_account_reachable(self, user_id: str) -> bool:
        """軽量リチェック: gallery-dl --range 1-1 でアカウントの到達性を確認"""
        if not self.refresh_token:
            self.logger.warning("Cannot check Pixiv reachability: no refresh-token")
            return True  # 設定不備は一時的 → 到達可能扱い（誤フラグ防止）

        url = f"https://www.pixiv.net/users/{user_id}/artworks"
        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '-o', f'extractor.pixiv.refresh-token={self.refresh_token}',
            '-q', '-j',
            '--range', '1-1',
            url,
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )

            # exitcode 0/1 に関係なく、退会・存在しないパターンを優先チェック
            # （gallery-dlはNotFoundでも exitcode=0 を返すことがある）
            if self._is_pixiv_not_found(result.stderr, result.stdout):
                self.logger.info(
                    f"Pixiv user {user_id} appears unreachable: {result.stderr[:200]}"
                )
                return False

            if result.returncode == 0:
                return True

            # returncode != 0 かつ明確な削除シグナルなし → 一時的エラーとして到達可能扱い
            self.logger.debug(
                f"Pixiv user {user_id}: gallery-dl returned {result.returncode}, "
                f"no clear deletion signal"
            )
            return True

        except subprocess.TimeoutExpired:
            return True  # タイムアウトは一時的
        except Exception as e:
            self.logger.error(f"Error checking Pixiv reachability for user {user_id}: {e}")
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(self, user_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        指定PixivユーザーIDの作品メタデータを取得

        Args:
            user_id: Pixiv数値ユーザーID
            limit: 取得件数制限

        Returns:
            作品情報のリスト
        """
        if not self.refresh_token:
            self.logger.error("Cannot fetch Pixiv works: refresh-token not configured")
            return []

        url = f"https://www.pixiv.net/users/{user_id}/artworks"

        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '-o', f'extractor.pixiv.refresh-token={self.refresh_token}',
            '-v',
            '-j',
        ]

        if limit:
            cmd.extend(['--range', f'1-{limit}'])

        cmd.append(url)

        self.logger.info(f"Fetching Pixiv works for user {user_id} (limit: {limit or 'all'})")

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(f"gallery-dl stderr for Pixiv user {user_id}: {result.stderr[:2000]}")

            # exitcode 0/1 に関係なく、退会・存在しないパターンを優先チェック
            if self._is_pixiv_not_found(result.stderr, result.stdout):
                self.logger.error(
                    f"Pixiv user {user_id} appears unreachable (not found): {result.stderr[:200]}"
                )
                self._account_reachable[user_id] = False
                return []

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for Pixiv user {user_id}: returncode={result.returncode}"
                    f" stderr={result.stderr[:300]}"
                )
                return []

            output = result.stdout.strip()
            if not output:
                self.logger.info(f"No output from gallery-dl for Pixiv user {user_id}")
                self._account_reachable[user_id] = True  # returncode==0 → 到達可能
                return []

            self._account_reachable[user_id] = True
            return self._parse_gallery_dl_output(output, user_id)

        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout fetching Pixiv works for user {user_id}")
            # タイムアウトは一時的 → reachable を設定しない
            return []
        except Exception as e:
            self.logger.error(f"Error fetching Pixiv works: {e}")
            return []

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定の作品IDのメディアをダウンロード

        Args:
            user_id: PixivユーザーID
            work_ids: ダウンロード対象の作品IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            作品IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        if not self.refresh_token:
            self.logger.error("Cannot download Pixiv media: refresh-token not configured")
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)
        existing_files = self._snapshot_existing_files()
        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []

        self.logger.info(f"Downloading media for {len(work_ids)} Pixiv works in a single run")

        url_file_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as url_file:
                for wid in work_ids:
                    url_file.write(f"https://www.pixiv.net/artworks/{wid}\n")
                url_file_path = url_file.name

            cmd = [
                sys.executable,
                str(self.wrapper_path),
                '-o', f'extractor.pixiv.refresh-token={self.refresh_token}',
                '-d', str(self.media_dir),
                '-v',
                '--input-file', url_file_path,
            ]

            result = run_with_idle_timeout(cmd, idle_timeout=180, rate_limit_retries=0)
            if result.returncode != 0:
                self.logger.warning(f"gallery-dl issues: {result.stderr[:200]}")

            all_media_paths = self._collect_downloaded_files(
                work_ids,
                self.media_dir,
                existing_files,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning(f"Timeout downloading Pixiv media for user {user_id}")
            all_media_paths = self._collect_downloaded_files(
                work_ids,
                self.media_dir,
                existing_files,
            )
        except Exception as exc:
            self.logger.error(f"Error downloading Pixiv media for {user_id}: {exc}")
        finally:
            if url_file_path:
                try:
                    os.unlink(url_file_path)
                except Exception:
                    pass

        for files in all_media_paths.values():
            all_downloaded.extend(files)

        missing = [wid for wid in work_ids if wid not in all_media_paths]
        if missing:
            self.logger.error(f"Failed to download media for {len(missing)} Pixiv works")

        # ファイルをimagesディレクトリに移動
        final_paths: Dict[str, List[str]] = {}
        if move_to_images and all_downloaded:
            moved = self._move_to_images_dir_with_mapping(all_downloaded, user_id)
            for wid, orig_files in all_media_paths.items():
                paths = []
                for orig in orig_files:
                    if orig in moved:
                        abs_path = str(moved[orig]).replace('\\', '/')
                        if '/images/' in abs_path:
                            rel = 'images/' + abs_path.split('/images/')[1]
                        elif '/videos/' in abs_path:
                            rel = 'videos/' + abs_path.split('/videos/')[1]
                        else:
                            rel = abs_path
                        paths.append(rel)
                if paths:
                    final_paths[wid] = paths
        else:
            final_paths = {
                wid: [str(f) for f in fs]
                for wid, fs in all_media_paths.items()
            }

        self._cleanup_media_dir()
        return final_paths

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _snapshot_existing_files(self) -> set:
        existing_files = set()
        if self.media_dir.exists():
            for file_path in self.media_dir.rglob('*'):
                if file_path.is_file():
                    existing_files.add(file_path)
        return existing_files

    def _parse_gallery_dl_output(self, output: str, user_id: str) -> List[Dict[str, Any]]:
        """gallery-dlのJSON出力をパースして作品情報リストを返す"""
        works: Dict[str, Dict[str, Any]] = {}

        try:
            if not output.startswith('['):
                self.logger.error("Unexpected gallery-dl output format")
                return []

            all_items = json.loads(output)

            for item in all_items:
                if not isinstance(item, list) or len(item) < 2:
                    continue

                item_type = item[0]

                if item_type == 2 and isinstance(item[1], dict):
                    # Type 2: 作品メタデータ [2, metadata_dict]
                    work_info = self._extract_work_info(item[1])
                    if work_info:
                        wid = work_info['id']
                        if wid not in works:
                            works[wid] = work_info

                elif item_type == 3 and len(item) >= 3 and isinstance(item[2], dict):
                    # Type 3: ファイルダウンロード [3, media_url, metadata_dict]
                    media_url = item[1]
                    media_meta = item[2]
                    wid = str(media_meta.get('id', ''))
                    if wid:
                        if wid not in works:
                            work_info = self._extract_work_info(media_meta)
                            if work_info:
                                works[wid] = work_info
                        if wid in works and media_url not in works[wid]['media']:
                            works[wid]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse gallery-dl Pixiv output: {e}")
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} Pixiv works for user {user_id}")
        return result

    def _extract_work_info(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータから作品情報を抽出"""
        try:
            work_id = data.get('id')
            if not work_id:
                return None

            work_id = str(work_id)

            # 日付パース
            date_str = data.get('date', '')
            if date_str:
                if isinstance(date_str, str):
                    # "2025-01-15 12:00:00" or ISO format
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            dt = datetime.strptime(date_str, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        dt = datetime.now()
                else:
                    dt = datetime.now()
                date_iso = dt.isoformat() if dt.tzinfo else dt.isoformat() + 'Z'
            else:
                date_iso = datetime.now().isoformat() + 'Z'

            # ユーザー情報
            user_info = data.get('user', {})
            user_id = str(user_info.get('id', data.get('user_id', '')))
            display_name = user_info.get('name', '') or user_info.get('account', '')

            # タグ
            tags = []
            raw_tags = data.get('tags', [])
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    if isinstance(tag, str):
                        tags.append(tag)
                    elif isinstance(tag, dict):
                        tags.append(tag.get('name', ''))

            # メディアURL
            media_url = data.get('url', '')
            media_list = [media_url] if media_url else []

            # 作品タイプ判定
            work_type = data.get('type', 'illust')
            if isinstance(work_type, int):
                work_type = {0: 'illust', 1: 'manga', 2: 'ugoira'}.get(work_type, 'illust')

            return {
                'id': work_id,
                'username': user_id,
                'display_name': display_name,
                'text': data.get('title', ''),
                'date': date_iso,
                'url': f"https://www.pixiv.net/artworks/{work_id}",
                'media': media_list,
                'tags': tags,
                'work_type': work_type,
                'page_count': data.get('page_count', 1),
                'bookmark_count': data.get('bookmark_count', 0),
                'x_restrict': data.get('x_restrict', 0),
                'sensitive': data.get('x_restrict', 0) >= 1,
                'source': 'pixiv',
                'platform': 'pixiv',
            }

        except Exception as e:
            self.logger.error(f"Error extracting Pixiv work info: {e}")
            return None

    def _collect_downloaded_files(
        self,
        work_ids: List[str],
        output_dir: Path,
        existing_files: set,
    ) -> Dict[str, List[Path]]:
        """ダウンロード済みファイルを作品IDごとに収集"""
        new_files_by_work: Dict[str, List[Path]] = {}
        if not output_dir.exists():
            return new_files_by_work

        for f in output_dir.rglob('*'):
            if not f.is_file() or f in existing_files:
                continue

            filename = f.name
            # gallery-dlのPixivファイル名パターン: {work_id}_p{page}.ext
            # 例: 140326050_p0.jpg
            for wid in work_ids:
                if filename.startswith(f"{wid}_") or filename.startswith(f"{wid}."):
                    if wid not in new_files_by_work:
                        new_files_by_work[wid] = []
                    new_files_by_work[wid].append(f)
                    break

        return new_files_by_work

    def _move_to_images_dir_with_mapping(
        self, files: List[Path], user_id: str
    ) -> Dict[Path, Path]:
        """ダウンロードしたファイルをimages/videosディレクトリに移動"""
        mapping: Dict[Path, Path] = {}
        try:
            images_base, videos_base = get_media_base_paths(self.config)

            images_dir = images_base / user_id
            videos_dir = videos_base / user_id
            images_dir.mkdir(parents=True, exist_ok=True)
            videos_dir.mkdir(parents=True, exist_ok=True)

            video_extensions = {
                '.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv',
                '.m4v', '.mpg', '.mpeg', '.3gp', '.3g2', '.ts', '.vob',
                '.ogv', '.f4v', '.asf', '.rm', '.rmvb', '.m2ts', '.mts',
                '.m3u8', '.m3u',
                '.gif', '.gifv',
                '.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus',
                '.wma', '.aiff', '.alac', '.oga',
            }

            for src_file in files:
                filename = src_file.name
                is_video = src_file.suffix.lower() in video_extensions

                if is_video:
                    dest_file = videos_dir / filename
                else:
                    dest_file = images_dir / filename

                if dest_file.exists():
                    mapping[src_file] = dest_file
                else:
                    shutil.copy2(src_file, dest_file)
                    mapping[src_file] = dest_file

            self.logger.info(f"Moved {len(mapping)} Pixiv media files for user {user_id}")

        except Exception as e:
            self.logger.error(f"Failed to move Pixiv files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
