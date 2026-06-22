#!/usr/bin/env python3
"""
TINAMI作品取得・メディアダウンロード
requests + BeautifulSoup によるカスタムスクレイパー
（gallery-dlがTINAMI非対応のため）
"""

import http.cookiejar
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

from .path_utils import get_media_base_paths
from .rate_limit_utils import request_with_rate_limit_retry


class TinamiExtractor:
    """TINAMI作品をHTTPスクレイピングで取得"""

    BASE_URL = "https://www.tinami.com"
    ITEMS_PER_PAGE = 20
    REQUEST_MAX_RETRIES = 5
    REQUEST_INTERVAL = 3  # リクエスト間の秒数

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.TinamiExtractor")
        self.tinami_config = config.get('tinami', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'tinami'

        # バッチサイズ
        batch_cfg = self.tinami_config.get('batch_size', 50)
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

        # HTTPセッション初期化
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Referer': 'https://www.tinami.com/',
            'Accept-Language': 'ja,en;q=0.9',
        })

        # 認証Cookie（R-18コンテンツ用）- Netscapeクッキーファイルから読み込み
        cookie_loaded = False
        cookie_path = Path("cookies/www.tinami.com_cookies.txt")
        if cookie_path.exists():
            try:
                cj = http.cookiejar.MozillaCookieJar()
                cj.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
                self._session.cookies.update(cj)
                cookie_loaded = True
                self.logger.info(f"TINAMI cookies loaded from {cookie_path}")
            except Exception as e:
                self.logger.warning(f"Failed to load TINAMI cookie file: {e}")

        if not cookie_loaded:
            self.logger.warning(
                "TINAMI cookie file not found (cookies/www.tinami.com_cookies.txt). "
                "R-18 content will be inaccessible."
            )

        self._last_request_time = 0.0

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """リクエスト間の待機"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_INTERVAL:
            time.sleep(self.REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """スロットル付きGETリクエスト"""
        return request_with_rate_limit_retry(
            self._session,
            "get",
            url,
            logger=self.logger,
            throttle=self._throttle,
            max_retries=self.REQUEST_MAX_RETRIES,
            timeout=30,
            **kwargs,
        )

    def _post(self, url: str, **kwargs) -> Optional[requests.Response]:
        """スロットル付きPOSTリクエスト"""
        return request_with_rate_limit_retry(
            self._session,
            "post",
            url,
            logger=self.logger,
            throttle=self._throttle,
            max_retries=self.REQUEST_MAX_RETRIES,
            timeout=30,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, prof_id: str) -> Optional[str]:
        """プロフィールページからクリエイター名を取得"""
        url = f"{self.BASE_URL}/creator/profile/{prof_id}"
        resp = self._get(url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        # プロフィールページのクリエイター名を探す
        # パターン1: <title>タグから「XXXXさんのプロフィール」を抽出
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text()
            m = re.match(r'(.+?)さんのプロフィール', title_text)
            if m:
                name = m.group(1).strip()
                if name and name != '-':
                    return name

        # パターン2: メタデータから取得
        for meta in soup.find_all('meta', attrs={'property': 'og:title'}):
            content = meta.get('content', '')
            m = re.match(r'(.+?)さんのプロフィール', content)
            if m:
                name = m.group(1).strip()
                if name and name != '-':
                    return name

        return None

    def check_account_reachable(self, prof_id: str) -> bool:
        """プロフィールページへのGETでアカウント存在確認"""
        url = f"{self.BASE_URL}/creator/profile/{prof_id}"
        self._throttle()
        try:
            resp = self._session.get(url, timeout=30, allow_redirects=False)
            if resp.status_code == 200:
                return True
            if resp.status_code in (301, 302, 404):
                self.logger.info(f"TINAMI prof_id {prof_id} unreachable: HTTP {resp.status_code}")
                return False
            # その他のステータスは一時的エラーとして到達可能扱い
            return True
        except requests.RequestException as e:
            self.logger.error(f"Error checking TINAMI reachability for prof_id {prof_id}: {e}")
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(self, prof_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        指定クリエイターの作品メタデータを取得

        Args:
            prof_id: TINAMIクリエイターのプロフィールID
            limit: 取得件数制限

        Returns:
            作品情報のリスト
        """
        self.logger.info(f"Fetching TINAMI works for prof_id {prof_id} (limit: {limit or 'all'})")

        # 1. 作品リストページから作品IDを収集
        work_ids = self._collect_work_ids(prof_id, limit)
        if not work_ids:
            self.logger.info(f"No TINAMI works found for prof_id {prof_id}")
            # 作品なしの場合もリチャビリティを記録
            self._account_reachable[prof_id] = True
            return []

        self._account_reachable[prof_id] = True
        self.logger.info(f"Found {len(work_ids)} work IDs for prof_id {prof_id}")

        # 2. 各作品ページからメタデータ取得
        works = []
        for work_id in work_ids:
            work_info = self._fetch_work_detail(work_id, prof_id)
            if work_info:
                works.append(work_info)

        self.logger.info(f"Parsed {len(works)} TINAMI works for prof_id {prof_id}")
        return works

    def download_media_for_works(
        self,
        prof_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定の作品IDのメディアをダウンロード

        Args:
            prof_id: TINAMIクリエイターID
            work_ids: ダウンロード対象の作品IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            作品IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)

        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []

        for work_id in work_ids:
            downloaded = self._download_work_images(work_id)
            if downloaded:
                all_media_paths[work_id] = downloaded
                all_downloaded.extend(downloaded)
                self.logger.info(f"Downloaded {len(downloaded)} files for TINAMI work {work_id}")

        # ファイルをimages/videosディレクトリに移動
        final_paths: Dict[str, List[str]] = {}
        if move_to_images and all_downloaded:
            moved = self._move_to_images_dir_with_mapping(all_downloaded, prof_id)
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
    # Internal: 作品リスト収集
    # ------------------------------------------------------------------

    def _collect_work_ids(self, prof_id: str, limit: Optional[int] = None) -> List[str]:
        """作品リストページを巡回して作品IDを収集"""
        work_ids = []
        offset = 0

        while True:
            url = (
                f"{self.BASE_URL}/search/list"
                f"?prof_id={prof_id}&sort=new&offset={offset}"
            )
            resp = self._get(url)
            if not resp:
                # 最初のページで失敗 → 到達不能
                if offset == 0:
                    self._account_reachable[prof_id] = False
                break

            soup = BeautifulSoup(resp.text, 'html.parser')

            # 作品リンクを抽出: <a href="/view/XXXXXXX">
            page_ids = []
            for link in soup.find_all('a', href=True):
                href = link['href']
                m = re.match(r'/view/(\d+)', href)
                if m:
                    wid = m.group(1)
                    if wid not in work_ids and wid not in page_ids:
                        page_ids.append(wid)

            if not page_ids:
                break

            work_ids.extend(page_ids)

            if limit and len(work_ids) >= limit:
                work_ids = work_ids[:limit]
                break

            # 次のページがあるか確認
            offset += self.ITEMS_PER_PAGE
            # ページ送りリンクが存在するか確認
            next_link = soup.find('a', href=re.compile(rf'offset={offset}'))
            if not next_link:
                break

        return work_ids

    # ------------------------------------------------------------------
    # Internal: 作品詳細取得
    # ------------------------------------------------------------------

    def _fetch_work_detail(self, work_id: str, prof_id: str) -> Optional[Dict[str, Any]]:
        """個別作品ページからメタデータを取得"""
        url = f"{self.BASE_URL}/view/{work_id}"
        resp = self._get(url)
        if not resp:
            return None

        return self._parse_work_page(resp.text, work_id, prof_id)

    def _parse_work_page(self, html: str, work_id: str, prof_id: str) -> Optional[Dict[str, Any]]:
        """作品ページHTMLからメタデータを抽出"""
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # センシティブ判定: 年齢認証ゲートの検出
            sensitive = False
            page_text = soup.get_text()
            if '18歳以上' in page_text or '年齢確認' in page_text or 'R-18' in page_text:
                sensitive = True

            # タイトル
            title = ''
            # og:titleから取得
            og_title = soup.find('meta', attrs={'property': 'og:title'})
            if og_title:
                title = og_title.get('content', '').strip()
            # titleタグからフォールバック
            if not title or title == '-':
                title_tag = soup.find('title')
                if title_tag:
                    title_text = title_tag.get_text().strip()
                    # 「作品名 | TINAMI」パターンから作品名を抽出
                    if '|' in title_text:
                        title = title_text.split('|')[0].strip()
                    elif ' - ' in title_text:
                        title = title_text.split(' - ')[0].strip()
                    else:
                        title = title_text

            # 投稿日時
            date_iso = datetime.now().isoformat() + 'Z'
            # ページ内の日時パターンを探す: "YYYY-MM-DD HH:MM:SS"
            date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', html)
            if date_match:
                try:
                    dt = datetime.strptime(date_match.group(1), '%Y-%m-%d %H:%M:%S')
                    date_iso = dt.isoformat() + 'Z'
                except ValueError:
                    pass

            # クリエイター名
            display_name = ''
            # プロフィールリンクのテキストから取得
            profile_link = soup.find('a', href=re.compile(r'/creator/profile/\d+'))
            if profile_link:
                name_text = profile_link.get_text().strip()
                if name_text and name_text != '-':
                    display_name = name_text

            # タグ
            tags = []
            # タグセクションを探す
            for tag_link in soup.find_all('a', href=re.compile(r'/search/list\?.*keyword=')):
                tag_text = tag_link.get_text().strip()
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)

            # 画像URL取得
            media_urls = self._extract_image_urls(soup, work_id)

            # 作品タイプ判定
            work_type = 'illustration'
            # アイコンやクラスからタイプ判定
            if soup.find('img', src=re.compile(r'manga|comic', re.I)):
                work_type = 'manga'

            return {
                'id': work_id,
                'username': prof_id,
                'display_name': display_name,
                'text': title if title != '-' else '',
                'date': date_iso,
                'url': f"{self.BASE_URL}/view/{work_id}",
                'media': media_urls,
                'tags': tags,
                'work_type': work_type,
                'sensitive': sensitive,
                'source': 'tinami',
                'platform': 'tinami',
            }

        except Exception as e:
            self.logger.error(f"Error parsing TINAMI work page {work_id}: {e}")
            return None

    def _extract_image_urls(self, soup: BeautifulSoup, work_id: str) -> List[str]:
        """作品ページから画像URLを抽出"""
        urls = []

        # パターン1: img.tinami.com の画像を探す
        for img in soup.find_all('img', src=re.compile(r'img\.tinami\.com')):
            src = img.get('src', '')
            if src:
                # プロトコル補完
                if src.startswith('//'):
                    src = 'https:' + src
                # サムネイルではなく本体画像を取得
                # L/ をフルサイズに置換（サムネイルパターン: /illust3/L/ → /illust3/img/）
                if '/L/' in src:
                    # サムネイルはスキップ（本体画像を探す）
                    continue
                if src not in urls:
                    urls.append(src)

        # パターン2: open_original_content フォームがある場合、元画像URLを取得
        original_form = soup.find('form', id='open_original_content')
        if original_form:
            # フォーム内の隠しフィールドからURLを構築
            action = original_form.get('action', '')
            if action:
                if action.startswith('/'):
                    action = self.BASE_URL + action
                # POSTで元画像ページを取得
                original_urls = self._fetch_original_image_urls(work_id)
                if original_urls:
                    # 元画像URLで置換
                    urls = original_urls

        # パターン3: og:image メタタグからフォールバック
        if not urls:
            og_image = soup.find('meta', attrs={'property': 'og:image'})
            if og_image:
                url = og_image.get('content', '')
                if url:
                    if url.startswith('//'):
                        url = 'https:' + url
                    urls.append(url)

        return urls

    def _fetch_original_image_urls(self, work_id: str) -> List[str]:
        """open_original_content フォームPOSTで元画像URLを取得"""
        url = f"{self.BASE_URL}/view/{work_id}"
        resp = self._post(url, data={'open_original_content': '1'})
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        urls = []

        # 拡大画像のURLを探す
        for img in soup.find_all('img', src=re.compile(r'img\.tinami\.com.*/(img|original)/')):
            src = img.get('src', '')
            if src:
                if src.startswith('//'):
                    src = 'https:' + src
                if src not in urls:
                    urls.append(src)

        return urls

    # ------------------------------------------------------------------
    # Internal: メディアダウンロード
    # ------------------------------------------------------------------

    def _download_work_images(self, work_id: str) -> List[Path]:
        """作品の画像をダウンロード"""
        # まず作品ページからURLを取得
        url = f"{self.BASE_URL}/view/{work_id}"
        resp = self._get(url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        image_urls = self._extract_image_urls(soup, work_id)

        if not image_urls:
            self.logger.warning(f"No image URLs found for TINAMI work {work_id}")
            return []

        downloaded = []
        for idx, img_url in enumerate(image_urls):
            file_path = self._download_single_image(img_url, work_id, idx)
            if file_path:
                downloaded.append(file_path)

        return downloaded

    def _download_single_image(self, img_url: str, work_id: str, index: int) -> Optional[Path]:
        """単一画像をダウンロード"""
        try:
            resp = request_with_rate_limit_retry(
                self._session,
                "get",
                img_url,
                logger=self.logger,
                throttle=self._throttle,
                max_retries=self.REQUEST_MAX_RETRIES,
                timeout=60,
                stream=True,
            )
            if not resp:
                return None

            # 拡張子をContent-TypeまたはURLから判定
            content_type = resp.headers.get('Content-Type', '')
            ext = self._guess_extension(content_type, img_url)

            filename = f"{work_id}_p{index}{ext}"
            file_path = self.media_dir / filename

            self.media_dir.mkdir(parents=True, exist_ok=True)

            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            self.logger.debug(f"Downloaded: {filename} ({file_path.stat().st_size} bytes)")
            return file_path

        except Exception as e:
            self.logger.error(f"Failed to download image {img_url}: {e}")
            return None
        finally:
            if 'resp' in locals() and resp is not None:
                resp.close()

    @staticmethod
    def _guess_extension(content_type: str, url: str) -> str:
        """Content-TypeまたはURLから拡張子を推測"""
        ct_map = {
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'image/bmp': '.bmp',
        }
        for ct, ext in ct_map.items():
            if ct in content_type:
                return ext

        # URLの拡張子
        m = re.search(r'\.(\w{3,4})(?:\?|$)', url)
        if m:
            return '.' + m.group(1).lower()

        return '.jpg'  # デフォルト

    # ------------------------------------------------------------------
    # Internal: ファイル移動
    # ------------------------------------------------------------------

    def _move_to_images_dir_with_mapping(
        self, files: List[Path], prof_id: str
    ) -> Dict[Path, Path]:
        """ダウンロードしたファイルをimages/videosディレクトリに移動"""
        mapping: Dict[Path, Path] = {}
        try:
            images_base, videos_base = get_media_base_paths(self.config)

            images_dir = images_base / prof_id
            videos_dir = videos_base / prof_id
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

            self.logger.info(f"Moved {len(mapping)} TINAMI media files for prof_id {prof_id}")

        except Exception as e:
            self.logger.error(f"Failed to move TINAMI files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
