#!/usr/bin/env python3
"""
Privatter投稿取得・メディアダウンロード
requests + BeautifulSoup によるカスタムスクレイパー
（gallery-dlがPrivatter非対応のため）

Privatterは画像を自前でホストしない。画像URLが判明すれば認証なしでダウンロード可能。
R-18判定: Privatterには年齢制限マーカーが存在しないため、全投稿を sensitive=True とする。
"""

import http.cookiejar
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


class PrivatterExtractor:
    """Privatter投稿をHTTPスクレイピングで取得"""

    BASE_URL = "https://privatter.net"
    REQUEST_MAX_RETRIES = 5
    REQUEST_INTERVAL = 3  # リクエスト間の秒数

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.PrivatterExtractor")
        self.privatter_config = config.get('privatter', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'privatter'

        # バッチサイズ
        batch_cfg = self.privatter_config.get('max_batch_size',
                    self.privatter_config.get('batch_size', 50))
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

        # DL時にページから抽出したURL数を記録（work_id -> count）
        self._expected_counts: Dict[str, int] = {}

        # HTTPセッション初期化
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en;q=0.9',
        })

        # 画像ダウンロード用セッション（Host/Cookieなし、外部CDN向け）
        self._dl_session = requests.Session()
        self._dl_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en;q=0.9',
        })

        # 認証Cookie読み込み - Netscapeクッキーファイルから
        cookie_loaded = False
        cookie_path = Path("cookies/privatter.net_cookies.txt")
        if cookie_path.exists():
            try:
                cj = http.cookiejar.MozillaCookieJar()
                cj.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
                self._session.cookies.update(cj)
                cookie_loaded = True
                self.logger.info(f"Privatter cookies loaded from {cookie_path}")
            except Exception as e:
                self.logger.warning(f"Failed to load Privatter cookie file: {e}")

        if not cookie_loaded:
            self.logger.warning(
                "Privatter cookie file not found (cookies/privatter.net_cookies.txt). "
                "Follower-only content will be inaccessible."
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """ユーザーページからdisplay nameを取得"""
        url = f"{self.BASE_URL}/u/{user_id}"
        resp = self._get(url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # og:title メタタグからユーザー名を取得
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title:
            content = og_title.get('content', '').strip()
            if content:
                return content

        # titleタグからフォールバック
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text().strip()
            # 「XXX - Privatter」パターンから名前を抽出
            if ' - ' in title_text:
                name = title_text.split(' - ')[0].strip()
                if name:
                    return name

        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """ユーザーページへのGETでアカウント存在確認"""
        url = f"{self.BASE_URL}/u/{user_id}"
        self._throttle()
        try:
            resp = self._session.get(url, timeout=30, allow_redirects=False)
            if resp.status_code == 200:
                return True
            if resp.status_code in (301, 302, 403, 404):
                self.logger.info(f"Privatter user_id {user_id} unreachable: HTTP {resp.status_code}")
                return False
            return True  # その他のステータスは一時的エラーとして到達可能扱い
        except requests.RequestException as e:
            self.logger.error(f"Error checking Privatter reachability for user_id {user_id}: {e}")
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(self, user_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        指定ユーザーの画像投稿メタデータを取得

        Args:
            user_id: PrivatterユーザーID（Twitterユーザー名）
            limit: 取得件数制限

        Returns:
            投稿情報のリスト
        """
        self.logger.info(f"Fetching Privatter works for user_id {user_id} (limit: {limit or 'all'})")

        # 1. ユーザーページから投稿リンクを収集
        post_entries = self._collect_post_entries(user_id, limit)
        if not post_entries:
            self.logger.info(f"No Privatter image posts found for user_id {user_id}")
            self._account_reachable[user_id] = True
            return []

        self._account_reachable[user_id] = True
        self.logger.info(f"Found {len(post_entries)} image post entries for user_id {user_id}")

        # 2. 各投稿ページからメタデータ取得
        works = []
        for entry in post_entries:
            work_info = self._fetch_post_detail(entry['id'], user_id)
            if work_info:
                works.append(work_info)

        self.logger.info(f"Parsed {len(works)} Privatter works for user_id {user_id}")
        return works

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定の投稿IDのメディアをダウンロード

        Args:
            user_id: PrivatterユーザーID
            work_ids: ダウンロード対象の投稿IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            投稿IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        self._expected_counts.clear()
        self.media_dir.mkdir(parents=True, exist_ok=True)

        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []

        for work_id in work_ids:
            downloaded = self._download_work_images(work_id, user_id)
            if downloaded:
                all_media_paths[work_id] = downloaded
                all_downloaded.extend(downloaded)
                self.logger.info(f"Downloaded {len(downloaded)} files for Privatter post {work_id}")

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

    def _collect_post_entries(self, user_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """ユーザーページから画像投稿のIDとURLを収集"""
        url = f"{self.BASE_URL}/u/{user_id}"
        resp = self._get(url)
        if not resp:
            self._account_reachable[user_id] = False
            return []

        if resp.status_code == 403:
            self.logger.warning(f"Privatter user page returned 403 for {user_id}")
            self._account_reachable[user_id] = False
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')

        # pull-left クラスの要素からリンクを抽出
        entries = []
        seen_ids = set()

        for elem in soup.find_all(class_='pull-left'):
            link = elem.find('a', href=True) if elem.name != 'a' else elem
            if not link or not link.get('href'):
                # elem自体がaタグでhrefを持つ場合
                if elem.name == 'a' and elem.get('href'):
                    href = elem['href']
                else:
                    continue
            else:
                href = link['href']

            # 画像投稿のみ対象: /i/{id}
            m = re.match(r'/i/(\d+)', href)
            if not m:
                continue

            post_id = m.group(1)
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            entries.append({
                'id': post_id,
                'url': f"{self.BASE_URL}{href}",
            })

        # ページネーション: 追加ページがある場合
        # 「次へ」「もっと見る」等のページ送りリンクを探す
        page = 2
        while True:
            next_link = soup.find('a', href=re.compile(rf'/u/{re.escape(user_id)}\?.*page='))
            if not next_link:
                # ページ番号付きリンクも試す
                next_link = soup.find('a', href=re.compile(r'\?page=\d+'))
            if not next_link:
                break

            next_url = next_link.get('href', '')
            if not next_url:
                break

            if next_url.startswith('/'):
                next_url = f"{self.BASE_URL}{next_url}"

            resp = self._get(next_url)
            if not resp:
                break

            soup = BeautifulSoup(resp.text, 'html.parser')
            page_entries = []
            for elem in soup.find_all(class_='pull-left'):
                link = elem.find('a', href=True) if elem.name != 'a' else elem
                if not link or not link.get('href'):
                    if elem.name == 'a' and elem.get('href'):
                        href = elem['href']
                    else:
                        continue
                else:
                    href = link['href']

                m = re.match(r'/i/(\d+)', href)
                if not m:
                    continue

                post_id = m.group(1)
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                page_entries.append({
                    'id': post_id,
                    'url': f"{self.BASE_URL}{href}",
                })

            if not page_entries:
                break

            entries.extend(page_entries)
            page += 1

            if limit and len(entries) >= limit:
                entries = entries[:limit]
                break

        if limit and len(entries) > limit:
            entries = entries[:limit]

        return entries

    # ------------------------------------------------------------------
    # Internal: 投稿詳細取得
    # ------------------------------------------------------------------

    def _fetch_post_detail(self, post_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """個別投稿ページからメタデータを取得"""
        url = f"{self.BASE_URL}/i/{post_id}"
        resp = self._get(url, headers={'Referer': f"{self.BASE_URL}/u/{user_id}"})
        if not resp:
            return None

        return self._parse_post_page(resp.text, post_id, user_id)

    def _parse_post_page(self, html: str, post_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """投稿ページHTMLからメタデータを抽出"""
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # Privatterには年齢制限マーカーが存在しないため全て sensitive=True
            sensitive = True

            # タイトル
            title = ''
            og_title = soup.find('meta', attrs={'property': 'og:title'})
            if og_title:
                title = og_title.get('content', '').strip()
            if not title:
                title_tag = soup.find('title')
                if title_tag:
                    title = title_tag.get_text().strip()

            # クリエイター名
            display_name = ''
            # ユーザープロフィールリンクから取得
            profile_link = soup.find('a', href=re.compile(r'/u/\w+'))
            if profile_link:
                name_text = profile_link.get_text().strip()
                if name_text and name_text != user_id:
                    display_name = name_text

            # 投稿日時 - Privatterはページ上に日時情報がない場合がある
            # datetime.now() を入れると取得順(新→旧)がそのまま時系列になり
            # _sort_artworks_oldest_first の date ソートで逆順になるバグの原因になる
            date_iso = ''

            # 画像URL取得
            media_urls = self._extract_image_urls(soup)

            return {
                'id': post_id,
                'username': user_id,
                'display_name': display_name,
                'text': title,
                'date': date_iso,
                'url': f"{self.BASE_URL}/i/{post_id}",
                'media': media_urls,
                'tags': [],
                'sensitive': sensitive,
                'source': 'privatter',
                'platform': 'privatter',
            }

        except Exception as e:
            self.logger.error(f"Error parsing Privatter post page {post_id}: {e}")
            return None

    @staticmethod
    def _is_valid_image_url(url: str) -> bool:
        """画像URLとして有効か判定（相対パス・プレースホルダーを除外）"""
        if not url:
            return False
        # 相対パスは除外
        if url.startswith('.') or url.startswith('/'):
            return False
        # blank.gif等のプレースホルダーを除外
        if 'blank.gif' in url or 'placeholder' in url.lower():
            return False
        # http/httpsスキームのみ許可
        if not url.startswith('http://') and not url.startswith('https://'):
            return False
        return True

    def _extract_image_urls(self, soup: BeautifulSoup) -> List[str]:
        """投稿ページから画像URLを抽出"""
        urls = []

        # class="image" の要素から画像URLを取得
        for elem in soup.find_all(class_='image'):
            # aタグのhref属性に画像URLがある場合
            if elem.name == 'a':
                href = elem.get('href', '')
                if href and href.startswith('//'):
                    href = 'https:' + href
                if self._is_valid_image_url(href) and href not in urls:
                    urls.append(href)
            else:
                # 子要素のaタグを探す
                a_tag = elem.find('a', href=True)
                if a_tag:
                    href = a_tag['href']
                    if href and href.startswith('//'):
                        href = 'https:' + href
                    if self._is_valid_image_url(href) and href not in urls:
                        urls.append(href)

                # imgタグのsrcを探す（aタグがない場合のみ）
                if not a_tag:
                    img_tag = elem.find('img', src=True)
                    if img_tag:
                        src = img_tag['src']
                        if src and src.startswith('//'):
                            src = 'https:' + src
                        if self._is_valid_image_url(src) and src not in urls:
                            urls.append(src)

        # フォールバック: og:imageから
        if not urls:
            og_image = soup.find('meta', attrs={'property': 'og:image'})
            if og_image:
                content = og_image.get('content', '')
                if content and content.startswith('//'):
                    content = 'https:' + content
                if self._is_valid_image_url(content):
                    urls.append(content)

        return urls

    # ------------------------------------------------------------------
    # Internal: メディアダウンロード
    # ------------------------------------------------------------------

    def _download_work_images(self, work_id: str, user_id: str) -> List[Path]:
        """投稿の画像をダウンロード（ページを再フェッチして新鮮なURLを取得）"""
        url = f"{self.BASE_URL}/i/{work_id}"
        resp = self._get(url, headers={'Referer': f"{self.BASE_URL}/u/{user_id}"})
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        image_urls = self._extract_image_urls(soup)

        # ページから実際に抽出できたURL数を記録（リトライ時の陳腐化対策）
        self._expected_counts[work_id] = len(image_urls)

        if not image_urls:
            self.logger.warning(f"No image URLs found for Privatter post {work_id}")
            return []

        downloaded = []
        for idx, img_url in enumerate(image_urls):
            file_path = self._download_single_image(img_url, work_id, idx, user_id)
            if file_path:
                downloaded.append(file_path)

        return downloaded

    def _download_single_image(
        self, img_url: str, work_id: str, index: int, user_id: str
    ) -> Optional[Path]:
        """単一画像をダウンロード（Privatterは画像を自前ホストしないため認証不要）"""
        try:
            # 外部CDN向けセッション（Host/Cookie汚染を避ける）
            session = self._dl_session
            headers = {'Referer': f"{self.BASE_URL}/i/{work_id}"}
            resp = request_with_rate_limit_retry(
                session,
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
            self.logger.error(f"Failed to download Privatter image {img_url}: {e}")
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

        # URLの拡張子（クエリパラメータを除去して判定）
        clean_url = url.split('?')[0]
        m = re.search(r'\.(\w{3,4})$', clean_url)
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

            self.logger.info(f"Moved {len(mapping)} Privatter media files for user_id {user_id}")

        except Exception as e:
            self.logger.error(f"Failed to move Privatter files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
