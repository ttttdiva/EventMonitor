#!/usr/bin/env python3
"""
Poipiku投稿取得・メディアダウンロード
requests + BeautifulSoup によるカスタムスクレイパー
（gallery-dlがCloudFront署名URL変更で動作不安定のため）
"""

import http.cookiejar
import json
import logging
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


class PoipikuExtractor:
    """Poipiku投稿をHTTPスクレイピングで取得"""

    BASE_URL = "https://poipiku.com"
    REQUEST_MAX_RETRIES = 5
    ITEMS_PER_PAGE = 48  # gallery-dlソースより: 1ページ48件
    REQUEST_INTERVAL = 3  # リクエスト間の秒数

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.PoipikuExtractor")
        self.poipiku_config = config.get('poipiku', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'poipiku'

        # バッチサイズ
        batch_cfg = self.poipiku_config.get('max_batch_size',
                    self.poipiku_config.get('batch_size', 50))
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
            'Accept-Language': 'ja,en;q=0.9',
        })

        # Poipiku用Cookie（R-18コンテンツ表示モード）
        self._session.cookies.set('POIPIKU_CONTENTS_VIEW_MODE', '1', domain='poipiku.com')
        self._session.cookies.set('LANG', 'ja', domain='poipiku.com')

        # 認証Cookie読み込み - Netscapeクッキーファイルから
        cookie_loaded = False
        cookie_path = Path("cookies/poipiku.com_cookies.txt")
        if cookie_path.exists():
            try:
                cj = http.cookiejar.MozillaCookieJar()
                cj.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
                self._session.cookies.update(cj)
                cookie_loaded = True
                self.logger.info(f"Poipiku cookies loaded from {cookie_path}")
            except Exception as e:
                self.logger.warning(f"Failed to load Poipiku cookie file: {e}")

        if not cookie_loaded:
            self.logger.warning(
                "Poipiku cookie file not found (cookies/poipiku.com_cookies.txt). "
                "R-18 and login-only content will be inaccessible."
            )

        # 認証状態チェック
        self._is_authenticated = self._check_authenticated()

        # APIリクエスト用ヘッダー
        self._api_headers = {
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': self.BASE_URL,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }

        self._last_request_time = 0.0

    def _check_authenticated(self) -> bool:
        """POIPIKU_LK cookieの有無で認証状態を判定"""
        for cookie in self._session.cookies:
            if cookie.name == 'POIPIKU_LK':
                return True
        return False

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

    def _api_post(self, url: str, data: dict, referer: str) -> Optional[dict]:
        """API用POSTリクエスト（JSON応答）"""
        headers = dict(self._api_headers)
        headers['Referer'] = referer
        resp = request_with_rate_limit_retry(
            self._session,
            "post",
            url,
            logger=self.logger,
            throttle=self._throttle,
            max_retries=self.REQUEST_MAX_RETRIES,
            timeout=30,
            data=data,
            headers=headers,
        )
        if not resp:
            return None

        try:
            return resp.json()
        except json.JSONDecodeError as e:
            self.logger.error(f"API POST failed for {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """ユーザーページからdisplay nameを取得"""
        url = f"{self.BASE_URL}/{user_id}/"
        resp = self._get(url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # パターン1: <h2 class="UserInfoUserName">内のテキスト
        name_tag = soup.find('h2', class_='UserInfoUserName')
        if name_tag:
            # リンク内のテキストを取得
            a_tag = name_tag.find('a')
            if a_tag:
                name = a_tag.get_text().strip()
                if name:
                    return name
            name = name_tag.get_text().strip()
            if name:
                return name

        # パターン2: og:titleから取得
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title:
            content = og_title.get('content', '').strip()
            if content:
                return content

        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """ユーザーページへのGETでアカウント存在確認"""
        url = f"{self.BASE_URL}/{user_id}/"
        self._throttle()
        try:
            resp = self._session.get(url, timeout=30, allow_redirects=False)
            if resp.status_code == 200:
                return True
            if resp.status_code in (301, 302, 404):
                self.logger.info(f"Poipiku user_id {user_id} unreachable: HTTP {resp.status_code}")
                return False
            return True  # その他のステータスは一時的エラーとして到達可能扱い
        except requests.RequestException as e:
            self.logger.error(f"Error checking Poipiku reachability for user_id {user_id}: {e}")
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(self, user_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        指定ユーザーの投稿メタデータを取得

        Args:
            user_id: PoipikuユーザーID
            limit: 取得件数制限

        Returns:
            投稿情報のリスト
        """
        self.logger.info(f"Fetching Poipiku works for user_id {user_id} (limit: {limit or 'all'})")

        # 1. 投稿リストから投稿IDを収集
        post_ids = self._collect_post_ids(user_id, limit)
        if not post_ids:
            self.logger.info(f"No Poipiku posts found for user_id {user_id}")
            self._account_reachable[user_id] = True
            return []

        self._account_reachable[user_id] = True
        self.logger.info(f"Found {len(post_ids)} post IDs for user_id {user_id}")

        # 2. 各投稿ページからメタデータ取得
        works = []
        for post_id in post_ids:
            work_info = self._fetch_post_detail(user_id, post_id)
            if work_info:
                works.append(work_info)

        self.logger.info(f"Parsed {len(works)} Poipiku works for user_id {user_id}")
        return works

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定の投稿IDのメディアをダウンロード
        署名URL対策: ダウンロード時に投稿ページを再フェッチして新鮮なURLを取得

        Args:
            user_id: PoipikuユーザーID
            work_ids: ダウンロード対象の投稿IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            投稿IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)

        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []

        for work_id in work_ids:
            downloaded = self._download_work_images(user_id, work_id)
            if downloaded:
                all_media_paths[work_id] = downloaded
                all_downloaded.extend(downloaded)
                self.logger.info(f"Downloaded {len(downloaded)} files for Poipiku post {work_id}")

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
    # Internal: 投稿リスト収集
    # ------------------------------------------------------------------

    def _collect_post_ids(self, user_id: str, limit: Optional[int] = None) -> List[str]:
        """投稿リストページを巡回して投稿IDを収集"""
        post_ids = []
        page_num = 0

        while True:
            url = f"{self.BASE_URL}/IllustListPcV.jsp"
            params = {'PG': page_num, 'ID': user_id, 'KWD': ''}
            resp = self._get(url, params=params)
            if not resp:
                if page_num == 0:
                    self._account_reachable[user_id] = False
                break

            # IllustInfo リンクから投稿IDを抽出
            page_ids = []
            for match in re.finditer(r'class="IllustInfo"\s+href="(/\d+/(\d+)\.html)"', resp.text):
                pid = match.group(2)
                if pid not in post_ids and pid not in page_ids:
                    page_ids.append(pid)

            if not page_ids:
                break

            post_ids.extend(page_ids)

            if limit and len(post_ids) >= limit:
                post_ids = post_ids[:limit]
                break

            # 48件未満なら最終ページ
            if len(page_ids) < self.ITEMS_PER_PAGE:
                break

            page_num += 1

        return post_ids

    # ------------------------------------------------------------------
    # Internal: 投稿詳細取得
    # ------------------------------------------------------------------

    def _fetch_post_detail(self, user_id: str, post_id: str) -> Optional[Dict[str, Any]]:
        """個別投稿ページからメタデータを取得"""
        url = f"{self.BASE_URL}/{user_id}/{post_id}.html"
        resp = self._get(url)
        if not resp:
            return None

        return self._parse_post_page(resp.text, user_id, post_id)

    def _parse_post_page(self, html: str, user_id: str, post_id: str) -> Optional[Dict[str, Any]]:
        """投稿ページHTMLからメタデータを抽出"""
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # センシティブ判定
            sensitive = self._detect_sensitive(soup, html)

            # カテゴリ（タイトルタグの [...] 部分）
            category = ''
            title_tag = soup.find('title')
            if title_tag:
                title_text = title_tag.get_text()
                m = re.match(r'\[(.+?)\]', title_text)
                if m:
                    category = m.group(1)

            # 投稿テキスト（説明文）
            description = ''
            desc_elem = soup.find(class_='IllustItemDesc')
            if desc_elem:
                description = desc_elem.get_text().strip()

            # クリエイター名
            display_name = ''
            name_tag = soup.find('h2', class_='UserInfoUserName')
            if name_tag:
                a_tag = name_tag.find('a')
                if a_tag:
                    display_name = a_tag.get_text().strip()
                else:
                    display_name = name_tag.get_text().strip()

            # 投稿日時 - Poipikuは明示的な日時表示がないため空文字
            # datetime.now() を入れると取得順(新→旧)がそのまま時系列になり
            # _sort_artworks_oldest_first の date ソートで逆順になるバグの原因になる
            date_iso = ''

            # 画像URL取得（メタデータ用 - カウント確認のため）
            media_urls = self._extract_image_urls(soup, user_id, post_id, html)

            # タグ
            tags = []
            if category:
                tags.append(category)

            return {
                'id': post_id,
                'username': user_id,
                'display_name': display_name,
                'text': description if description else category,
                'date': date_iso,
                'url': f"{self.BASE_URL}/{user_id}/{post_id}.html",
                'media': media_urls,
                'tags': tags,
                'sensitive': sensitive,
                'source': 'poipiku',
                'platform': 'poipiku',
            }

        except Exception as e:
            self.logger.error(f"Error parsing Poipiku post page {post_id}: {e}")
            return None

    def _detect_sensitive(self, soup: BeautifulSoup, html: str) -> bool:
        """センシティブコンテンツ（R-18/Warning）の検出"""
        # パターン1: IllustItem div の CSS クラスで判定（最も信頼性が高い）
        # R-18投稿: class="IllustItem R18 Upload"
        # Warning投稿: class="IllustItem R15 Upload"
        illust_item = soup.find('div', id=lambda x: x and x.startswith('IllustItem_'))
        if illust_item:
            classes = illust_item.get('class', [])
            if 'R18' in classes or 'R15' in classes:
                return True

        # パターン2: サムネイル画像URLに warning パスが含まれる
        # （cdn.poipiku.com/img/ および img.poipiku.com/img/ の両ドメインに対応）
        for img in soup.find_all('img', class_='IllustItemThumbImg'):
            src = img.get('src', '')
            if ('cdn.poipiku.com/img/' in src or 'img.poipiku.com/img/' in src) and '/warning' in src:
                return True

        # パターン3: 投稿本文内のR-18表記（おすすめ欄等を除外）
        post_texts = []
        desc_elem = soup.find(class_='IllustItemDesc')
        if desc_elem:
            post_texts.append(desc_elem.get_text())
        title_tag = soup.find('title')
        if title_tag:
            post_texts.append(title_tag.get_text())
        post_text = ' '.join(post_texts)
        if 'R-18' in post_text or 'R18' in post_text or 'NSFW' in post_text:
            return True

        return False

    def _extract_image_urls(
        self, soup: BeautifulSoup, user_id: str, post_id: str, html: str
    ) -> List[str]:
        """投稿ページから画像URLを抽出"""
        urls = []

        # サムネイル画像を抽出（実画像のみ、警告アイコンはスキップ）
        for img in soup.find_all('img', class_='IllustItemThumbImg'):
            src = img.get('src', '')
            if not src:
                continue
            # 警告/アクセス制限アイコンはスキップ（cdn/img 両ドメイン対応）
            if 'cdn.poipiku.com/img/' in src or 'img.poipiku.com/img/' in src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = self.BASE_URL + src
            if src not in urls:
                urls.append(src)

        # ShowAppendFile で追加画像を取得
        if 'ShowAppendFile' in html or not urls:
            append_urls = self._fetch_append_files(user_id, post_id)
            for u in append_urls:
                if u not in urls:
                    urls.append(u)

        return urls

    def _fetch_append_files(self, user_id: str, post_id: str) -> List[str]:
        """ShowAppendFileF.jsp APIで追加画像URLを取得"""
        url = f"{self.BASE_URL}/f/ShowAppendFileF.jsp"
        referer = f"{self.BASE_URL}/{user_id}/{post_id}.html"
        data = {
            'UID': user_id,
            'IID': post_id,
            'PAS': '',
            'MD': '0',
            'TWF': '-1',
        }

        result = self._api_post(url, data, referer)
        if not result:
            return []

        html = result.get('html', '')
        if not html:
            return []

        # エラーチェック
        result_num = result.get('result_num', 0)
        if result_num is not None and int(result_num) < 0:
            self.logger.warning(f"Poipiku {post_id}: API error: {html.replace('<br/>', ' ')}")
            return []

        # HTML内の画像URLを抽出
        urls = []
        for match in re.finditer(r'class="IllustItemThumbImg"\s+src="([^"]+)"', html):
            src = match.group(1)
            if 'cdn.poipiku.com/img/' in src or 'img.poipiku.com/img/' in src:
                continue  # アイコン画像はスキップ
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = self.BASE_URL + src
            if src not in urls:
                urls.append(src)

        return urls

    def _fetch_detail_files(self, user_id: str, post_id: str) -> List[str]:
        """ShowIllustDetailF.jsp APIで元画像URLを取得（認証済みユーザー用）"""
        url = f"{self.BASE_URL}/f/ShowIllustDetailF.jsp"
        referer = f"{self.BASE_URL}/{user_id}/{post_id}.html"
        data = {
            'ID': user_id,
            'TD': post_id,
            'AD': '-1',
            'PAS': '',
        }

        result = self._api_post(url, data, referer)
        if not result:
            return []

        if result.get('error_code'):
            return []

        html = result.get('html', '')
        if not html:
            return []

        urls = []
        for match in re.finditer(r'src="([^"]+)"', html):
            src = match.group(1)
            if 'cdn.poipiku.com/img/' in src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = self.BASE_URL + src
            if src not in urls:
                urls.append(src)

        return urls

    # ------------------------------------------------------------------
    # Internal: メディアダウンロード
    # ------------------------------------------------------------------

    def _download_work_images(self, user_id: str, work_id: str) -> List[Path]:
        """投稿の画像をダウンロード（署名URL対策で再フェッチ）"""
        # 認証済みの場合はShowIllustDetailF.jspで元画像URL取得
        if self._is_authenticated:
            image_urls = self._fetch_detail_files(user_id, work_id)
            if not image_urls:
                # フォールバック: ShowAppendFileF.jsp
                image_urls = self._fetch_append_files_for_download(user_id, work_id)
        else:
            image_urls = self._fetch_append_files_for_download(user_id, work_id)

        if not image_urls:
            self.logger.warning(f"No image URLs found for Poipiku post {work_id}")
            return []

        downloaded = []
        for idx, img_url in enumerate(image_urls):
            file_path = self._download_single_image(img_url, work_id, idx, user_id)
            if file_path:
                downloaded.append(file_path)

        return downloaded

    def _fetch_append_files_for_download(self, user_id: str, work_id: str) -> List[str]:
        """ダウンロード用: ページを再フェッチしてサムネイル + ShowAppendFile の画像URLを取得"""
        post_url = f"{self.BASE_URL}/{user_id}/{work_id}.html"
        resp = self._get(post_url)
        if not resp:
            return []

        urls = []

        # ページ内のサムネイル画像を取得
        for match in re.finditer(r'class="IllustItemThumbImg"\s+src="([^"]+)"', resp.text):
            src = match.group(1)
            if 'cdn.poipiku.com/img/' in src or 'img.poipiku.com/img/' in src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = self.BASE_URL + src
            if src not in urls:
                urls.append(src)

        # ShowAppendFileで追加画像を取得
        append_urls = self._fetch_append_files(user_id, work_id)
        for u in append_urls:
            if u not in urls:
                urls.append(u)

        return urls

    def _download_single_image(
        self, img_url: str, work_id: str, index: int, user_id: str
    ) -> Optional[Path]:
        """単一画像をダウンロード"""
        try:
            # Poipiku CDNにはRefererヘッダーが必要
            headers = {'Referer': f"{self.BASE_URL}/{user_id}/{work_id}.html"}
            resp = request_with_rate_limit_retry(
                self._session,
                "get",
                img_url,
                logger=self.logger,
                throttle=self._throttle,
                max_retries=self.REQUEST_MAX_RETRIES,
                timeout=60,
                stream=True,
                headers=headers,
            )
            if not resp:
                return None

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
            self.logger.error(f"Failed to download Poipiku image {img_url}: {e}")
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

        # URLの拡張子（署名URLのクエリパラメータを除去して判定）
        # Poipiku CDN URL例: xxx.png_640.jpg → .jpg
        clean_url = url.split('?')[0]
        m = re.search(r'\.(\w{3,4})$', clean_url)
        if m:
            return '.' + m.group(1).lower()

        # Poipiku特有: .png_640.jpg パターン → 元は .png
        m = re.search(r'\.(\w{3,4})_\d+\.jpg', clean_url)
        if m:
            return '.' + m.group(1).lower()

        return '.jpg'  # デフォルト

    # ------------------------------------------------------------------
    # Internal: ファイル移動
    # ------------------------------------------------------------------

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

            self.logger.info(f"Moved {len(mapping)} Poipiku media files for user_id {user_id}")

        except Exception as e:
            self.logger.error(f"Failed to move Poipiku files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
