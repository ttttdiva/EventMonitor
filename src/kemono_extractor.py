#!/usr/bin/env python3
"""
Kemono.cr作品取得・メディアダウンロード
gallery-dlを使用してKemono.crの作品メタデータとメディアを取得
fanbox, fantia等の複数サービスに対応
"""

import sys
import json
import subprocess
import logging
import os
import re
import hashlib
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests

from .path_utils import get_media_base_paths
from .subprocess_utils import run_with_idle_timeout

_KEMONO_HASH_RE = re.compile(r'/([0-9a-f]{64})\.', re.IGNORECASE)
_DEFAULT_CDN_FALLBACK_HOSTS = (
    'n2.kemono.cr',
    'n3.kemono.cr',
    'n4.kemono.cr',
    'n1.kemono.cr',
)
_DEFAULT_PREVIEW_FALLBACK_HOSTS = (
    'img.kemono.cr',
    'kemono.cr',
)


class KemonoExtractor:
    """gallery-dlを使用してKemono.cr作品を取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.KemonoExtractor")
        self.kemono_config = config.get('kemono', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'kemono'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}
        self._cdn_session = requests.Session()
        self._cdn_bad_until: Dict[str, float] = {}
        self._cdn_outage_preview_only = False

    # ------------------------------------------------------------------
    # Username parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_kemono_username(kemono_username: str) -> Tuple[str, str]:
        """
        kemono_username ('fanbox/3316400') を service, user_id に分解

        Args:
            kemono_username: 'service/user_id' 形式の文字列

        Returns:
            (service, user_id) タプル

        Raises:
            ValueError: 不正な形式の場合
        """
        parts = kemono_username.split('/', 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"Invalid kemono username format: '{kemono_username}' "
                f"(expected 'service/user_id', e.g. 'fanbox/3316400')"
            )
        return parts[0], parts[1]

    @staticmethod
    def _extract_hash_from_url(url: str) -> Optional[str]:
        """Kemono CDN URL からSHA256ハッシュを抽出

        URL format: https://kemono.cr/data/XX/YY/<64-char-sha256>.<ext>
        """
        match = _KEMONO_HASH_RE.search(url)
        if match:
            return match.group(1).lower()
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, kemono_username: str) -> Optional[str]:
        """
        表示名を取得（1作品だけ取得してメタデータから抽出）

        Args:
            kemono_username: 'service/user_id' 形式

        Returns:
            display name（取得できた場合）、取得失敗時はNone
        """
        works = self.fetch_user_works(kemono_username, limit=1)
        if not works:
            self.logger.warning(
                f"No works found for kemono user {kemono_username}, cannot resolve display name"
            )
            return None

        display_name = works[0].get('display_name', '')
        if display_name:
            self.logger.info(f"Resolved kemono user {kemono_username} -> {display_name}")
            return display_name

        self.logger.warning(
            f"Display name not found in metadata for kemono user {kemono_username}"
        )
        return None

    def check_account_reachable(self, kemono_username: str) -> bool:
        """軽量リチェック: gallery-dl --range 1-1 でアカウントの到達性を確認"""
        try:
            service, user_id = self._parse_kemono_username(kemono_username)
        except ValueError:
            return False

        url = f"https://kemono.cr/{service}/user/{user_id}"
        cmd = [
            sys.executable,
            str(self.wrapper_path),
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

            if result.returncode == 0:
                return True

            stderr_lower = (result.stderr or "").lower()
            not_found_patterns = ["404", "not found", "no results", "does not exist"]
            if any(p in stderr_lower for p in not_found_patterns):
                self.logger.info(
                    f"Kemono user {kemono_username} appears unreachable: "
                    f"{result.stderr[:200]}"
                )
                return False

            # 明確な削除シグナルなし → 一時的エラーとして到達可能扱い
            return True

        except subprocess.TimeoutExpired:
            return True  # タイムアウトは一時的
        except Exception as e:
            self.logger.error(
                f"Error checking kemono reachability for {kemono_username}: {e}"
            )
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()
        self._cdn_bad_until.clear()
        self._cdn_outage_preview_only = False

    def fetch_user_works(
        self, kemono_username: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        指定Kemonoユーザーの作品メタデータを取得

        Args:
            kemono_username: 'service/user_id' 形式
            limit: 取得件数制限

        Returns:
            作品情報のリスト
        """
        try:
            service, user_id = self._parse_kemono_username(kemono_username)
        except ValueError as e:
            self.logger.error(str(e))
            return []

        url = f"https://kemono.cr/{service}/user/{user_id}"

        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '-v',
            '-j',
        ]

        if limit:
            cmd.extend(['--range', f'1-{limit}'])

        cmd.append(url)

        self.logger.info(
            f"Fetching kemono works for {kemono_username} (limit: {limit or 'all'})"
        )

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(
                    f"gallery-dl stderr for kemono {kemono_username}: "
                    f"{result.stderr[:500]}"
                )

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for kemono {kemono_username}: "
                    f"returncode={result.returncode} stderr={result.stderr[:300]}"
                )
                stderr_lower = (result.stderr or "").lower()
                not_found_patterns = [
                    "404", "not found", "no results", "does not exist"
                ]
                if any(p in stderr_lower for p in not_found_patterns):
                    self._account_reachable[kemono_username] = False
                return []

            output = result.stdout.strip()
            if not output:
                self.logger.info(
                    f"No output from gallery-dl for kemono {kemono_username}"
                )
                self._account_reachable[kemono_username] = True
                return []

            self._account_reachable[kemono_username] = True
            return self._parse_gallery_dl_output(output, service)

        except subprocess.TimeoutExpired:
            self.logger.error(
                f"Timeout fetching kemono works for {kemono_username}"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error fetching kemono works: {e}")
            return []

    def download_media_for_works(
        self,
        kemono_username: str,
        work_ids: List[str],
        move_to_images: bool = True,
        hash_map: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> Dict[str, List[str]]:
        """
        特定の作品IDのメディアをダウンロード

        Args:
            kemono_username: 'service/user_id' 形式
            work_ids: ダウンロード対象の作品IDリスト ({service}_{post_id} 形式)
            move_to_images: imagesディレクトリに移動するか

        Returns:
            作品IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        try:
            service, user_id = self._parse_kemono_username(kemono_username)
        except ValueError as e:
            self.logger.error(str(e))
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)
        existing_files = self._snapshot_existing_files()
        work_id_to_post_id = self._build_work_id_to_post_id(work_ids)
        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []

        self.logger.info(f"Downloading media for {len(work_ids)} kemono works in a single run")

        skip_full_download = bool(hash_map and self._cdn_outage_preview_only)
        url_file_path = None
        try:
            if skip_full_download:
                self.logger.warning(
                    "Kemono CDN outage mode active; skipping gallery-dl full "
                    "media download and using previews only"
                )
            else:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False
                ) as url_file:
                    for work_id, post_id in work_id_to_post_id.items():
                        url_file.write(
                            f"https://kemono.cr/{service}/user/{user_id}/post/{post_id}\n"
                        )
                    url_file_path = url_file.name

                cmd = [
                    sys.executable,
                    str(self.wrapper_path),
                    '-d', str(self.media_dir),
                    '-o', 'filename={id}_{num:>02}.{extension}',
                    '-v',
                    '--input-file', url_file_path,
                ]

                result = run_with_idle_timeout(cmd, idle_timeout=180, rate_limit_retries=0)
                if result.returncode != 0:
                    self.logger.warning(
                        f"gallery-dl issues: {self._stderr_snippet(result.stderr)}"
                    )

                all_media_paths = self._collect_downloaded_files(
                    work_ids,
                    work_id_to_post_id,
                    self.media_dir,
                    existing_files,
                )

                if hash_map:
                    fallback_paths = self._download_missing_media_via_cdn_fallback(
                        work_ids,
                        work_id_to_post_id,
                        hash_map,
                        all_media_paths,
                    )
                    for wid, paths in fallback_paths.items():
                        all_media_paths.setdefault(wid, []).extend(paths)
        except subprocess.TimeoutExpired:
            self.logger.warning(f"Timeout downloading kemono media for {kemono_username}")
            all_media_paths = self._collect_downloaded_files(
                work_ids,
                work_id_to_post_id,
                self.media_dir,
                existing_files,
            )
        except Exception as exc:
            self.logger.error(f"Error downloading kemono media for {kemono_username}: {exc}")
        finally:
            if url_file_path:
                try:
                    os.unlink(url_file_path)
                except Exception:
                    pass

        if hash_map and all_media_paths:
            validated, failed_wids = self._validate_downloaded_files(
                all_media_paths,
                hash_map,
            )
            if failed_wids:
                self.logger.warning(
                    f"Hash validation failed for {len(failed_wids)} kemono works: {failed_wids[:5]}"
                )
            all_media_paths = validated

        if hash_map:
            preview_paths = self._download_missing_media_via_preview_fallback(
                work_ids,
                work_id_to_post_id,
                hash_map,
                all_media_paths,
            )
            for wid, paths in preview_paths.items():
                all_media_paths.setdefault(wid, []).extend(paths)
                all_media_paths[wid] = self._order_paths_with_preview_fallbacks(
                    all_media_paths[wid],
                    hash_map.get(wid, {}),
                    work_id_to_post_id.get(wid, wid),
                )

        if all_media_paths:
            all_media_paths = self._extract_zip_files(all_media_paths)

        for files in all_media_paths.values():
            all_downloaded.extend(files)

        missing = [wid for wid in work_ids if wid not in all_media_paths]
        if missing:
            self.logger.error(
                f"Failed to download media for {len(missing)} kemono works"
            )

        # ファイルをimages/videosディレクトリに移動
        dir_name = f"{service}_{user_id}"
        final_paths: Dict[str, List[str]] = {}
        if move_to_images and all_downloaded:
            moved = self._move_to_images_dir_with_mapping(
                all_downloaded, dir_name
            )
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

    @staticmethod
    def _stderr_snippet(stderr: str, limit: int = 2000) -> str:
        """長いstderrは末尾を優先して残す。"""
        if not stderr:
            return ""
        text = stderr.strip()
        if len(text) <= limit:
            return text
        return f"...{text[-limit:]}"

    def _snapshot_existing_files(self) -> set:
        existing_files = set()
        if self.media_dir.exists():
            for file_path in self.media_dir.rglob('*'):
                if file_path.is_file():
                    existing_files.add(file_path)
        return existing_files

    @staticmethod
    def _build_work_id_to_post_id(work_ids: List[str]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for work_id in work_ids:
            parts = work_id.split('_', 1)
            mapping[work_id] = parts[1] if len(parts) == 2 else work_id
        return mapping

    def _parse_gallery_dl_output(
        self, output: str, service: str
    ) -> List[Dict[str, Any]]:
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
                    work_info = self._extract_work_info(item[1], service)
                    if work_info:
                        wid = work_info['id']
                        if wid not in works:
                            works[wid] = work_info

                elif (item_type == 3
                      and len(item) >= 3
                      and isinstance(item[2], dict)):
                    # Type 3: ファイルダウンロード [3, media_url, metadata_dict]
                    media_url = item[1]
                    media_meta = item[2]
                    post_id = str(media_meta.get('id', ''))
                    item_service = media_meta.get('service', service)
                    wid = f"{item_service}_{post_id}"
                    if post_id:
                        if wid not in works:
                            work_info = self._extract_work_info(
                                media_meta, item_service
                            )
                            if work_info:
                                works[wid] = work_info
                        if wid in works and media_url not in works[wid]['media']:
                            works[wid]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(
                f"Failed to parse gallery-dl kemono output: {e}"
            )
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} kemono works")
        return result

    def _extract_work_info(
        self, data: Dict[str, Any], service: str
    ) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータから作品情報を抽出"""
        try:
            post_id = data.get('id')
            if not post_id:
                return None

            post_id = str(post_id)
            work_id = f"{service}_{post_id}"

            # 日付パース（publishedを優先、なければdate）
            date_str = data.get('published') or data.get('date', '')
            if date_str:
                if isinstance(date_str, str):
                    for fmt in (
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S%z",
                    ):
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
            user_id = str(data.get('user', ''))
            user_profile = data.get('user_profile', {})
            display_name = (
                data.get('username', '')
                or user_profile.get('name', '')
            )

            # メディアURL収集（元プラットフォームの表示順: file → attachments）
            # file = カバー/1枚目、attachments = 2枚目以降
            # gallery-dlは attachments→file の順で{num}を振るが、
            # ハッシュ検証時にここの順序で並べ替えるため、正しい表示順を定義する
            media_urls = []
            # file first (カバー/1枚目)
            file_info = data.get('file')
            if isinstance(file_info, dict) and file_info.get('url'):
                media_urls.append(file_info['url'])
            # attachments after (2枚目以降)
            for att in data.get('attachments', []):
                if isinstance(att, dict) and att.get('url'):
                    media_urls.append(att['url'])

            # メディアURL→SHA256ハッシュのマッピング構築
            media_hashes: Dict[str, str] = {}
            for url in media_urls:
                h = KemonoExtractor._extract_hash_from_url(url)
                if h:
                    media_hashes[url] = h

            # コンテンツ
            title = data.get('title', '')
            content = data.get('substring', '') or data.get('content', '')

            return {
                'id': work_id,
                'username': f"{service}/{user_id}",  # CSV形式と一致
                'display_name': display_name,
                'text': title,
                'content': content,
                'date': date_iso,
                'url': (
                    f"https://kemono.cr/{service}/user/{user_id}"
                    f"/post/{post_id}"
                ),
                'media': media_urls,
                'media_hashes': media_hashes,
                'tags': [],  # Kemonoにはタグフィールドなし
                'service': service,
                'post_id': post_id,
                'file_count': data.get('count', len(media_urls)),
                'sensitive': False,  # Kemonoにはセンシティブフラグなし
                'source': 'kemono',
                'platform': 'kemono',
            }

        except Exception as e:
            self.logger.error(f"Error extracting kemono work info: {e}")
            return None

    def _collect_downloaded_files(
        self,
        work_ids: List[str],
        work_id_to_post_id: Dict[str, str],
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
            # ファイル名パターン: {post_id}_{num}.ext
            for wid in work_ids:
                post_id = work_id_to_post_id.get(wid, wid)
                if (filename.startswith(f"{post_id}_")
                        or filename.startswith(f"{post_id}.")):
                    if wid not in new_files_by_work:
                        new_files_by_work[wid] = []
                    new_files_by_work[wid].append(f)
                    break

        # ファイル名順でソート（hash検証なし時のフォールバック）
        for wid in new_files_by_work:
            new_files_by_work[wid].sort(key=lambda f: f.name)

        return new_files_by_work

    def _download_missing_media_via_cdn_fallback(
        self,
        work_ids: List[str],
        work_id_to_post_id: Dict[str, str],
        hash_map: Dict[str, Dict[str, str]],
        current_paths: Dict[str, List[Path]],
    ) -> Dict[str, List[Path]]:
        """gallery-dlで不足したKemonoメディアをCDN別ホストから取得する。"""
        fallback_paths: Dict[str, List[Path]] = {}

        for work_id in work_ids:
            url_hashes = hash_map.get(work_id) or {}
            if not url_hashes:
                continue

            existing_hashes = self._hash_existing_files(current_paths.get(work_id, []))
            missing_urls = [
                (index, url, expected_hash)
                for index, (url, expected_hash) in enumerate(url_hashes.items(), start=1)
                if not expected_hash or expected_hash.lower() not in existing_hashes
            ]
            if not missing_urls:
                continue

            post_id = work_id_to_post_id.get(work_id, work_id)
            self.logger.info(
                f"Trying Kemono CDN fallback for {work_id}: "
                f"{len(missing_urls)} missing files"
            )

            for index, url, expected_hash in missing_urls:
                dest = self.media_dir / self._fallback_filename(post_id, index, url)
                downloaded = self._download_single_media_via_cdn_fallback(
                    url,
                    dest,
                    expected_hash,
                )
                if downloaded:
                    fallback_paths.setdefault(work_id, []).append(downloaded)

        return fallback_paths

    def _download_missing_media_via_preview_fallback(
        self,
        work_ids: List[str],
        work_id_to_post_id: Dict[str, str],
        hash_map: Dict[str, Dict[str, str]],
        current_paths: Dict[str, List[Path]],
    ) -> Dict[str, List[Path]]:
        """原寸CDNが落ちている場合にKemonoのthumbnail/previewを取得する。"""
        fallback_paths: Dict[str, List[Path]] = {}

        for work_id in work_ids:
            url_hashes = hash_map.get(work_id) or {}
            if not url_hashes:
                continue

            existing_hashes = self._hash_existing_files(current_paths.get(work_id, []))
            missing_urls = [
                (index, url, expected_hash)
                for index, (url, expected_hash) in enumerate(url_hashes.items(), start=1)
                if not expected_hash or expected_hash.lower() not in existing_hashes
            ]
            if not missing_urls:
                continue

            post_id = work_id_to_post_id.get(work_id, work_id)
            self.logger.info(
                f"Trying Kemono preview fallback for {work_id}: "
                f"{len(missing_urls)} missing files"
            )

            for index, url, _expected_hash in missing_urls:
                dest = self.media_dir / self._fallback_filename(
                    post_id, index, url, marker='preview'
                )
                downloaded = self._download_single_media_via_preview_fallback(
                    url,
                    dest,
                )
                if downloaded:
                    fallback_paths.setdefault(work_id, []).append(downloaded)

        return fallback_paths

    @staticmethod
    def _hash_existing_files(files: List[Path]) -> set:
        hashes = set()
        for file_path in files:
            try:
                if file_path.is_file():
                    hashes.add(KemonoExtractor._sha256_file(file_path))
            except OSError:
                continue
        return hashes

    @staticmethod
    def _sha256_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fallback_filename(
        post_id: str,
        index: int,
        url: str,
        marker: Optional[str] = None,
    ) -> str:
        suffix = Path(urlparse(url).path).suffix or '.bin'
        marker_part = f"_{marker}" if marker else ""
        return f"{post_id}_{index:02}{marker_part}{suffix}"

    def _download_single_media_via_cdn_fallback(
        self,
        original_url: str,
        dest: Path,
        expected_hash: Optional[str],
    ) -> Optional[Path]:
        for candidate_url in self._cdn_fallback_urls(original_url):
            host = urlparse(candidate_url).netloc
            if not self._cdn_host_available(host):
                continue

            try:
                downloaded = self._download_candidate_url(candidate_url, dest)
            except requests.RequestException as exc:
                if self._is_cdn_host_failure(exc):
                    self._mark_cdn_host_failure(host, exc)
                else:
                    self.logger.warning(
                        f"Kemono CDN fallback failed via {host}: {exc}"
                    )
                continue
            except OSError as exc:
                self.logger.warning(
                    f"Failed to write Kemono fallback file {dest}: {exc}"
                )
                return None

            if expected_hash:
                actual_hash = self._sha256_file(downloaded)
                if actual_hash.lower() != expected_hash.lower():
                    self.logger.warning(
                        f"Kemono CDN fallback hash mismatch for {downloaded.name}: "
                        f"expected={expected_hash} actual={actual_hash}"
                    )
                    try:
                        downloaded.unlink()
                    except OSError:
                        pass
                    continue

            self.logger.info(
                f"Kemono CDN fallback downloaded {downloaded.name} via {host}"
            )
            return downloaded

        self.logger.warning(
            f"Kemono CDN fallback failed for {Path(urlparse(original_url).path).name}"
        )
        return None

    def _download_single_media_via_preview_fallback(
        self,
        original_url: str,
        dest: Path,
    ) -> Optional[Path]:
        for candidate_url in self._preview_fallback_urls(original_url):
            host = urlparse(candidate_url).netloc

            try:
                downloaded = self._download_preview_candidate_url(candidate_url, dest)
            except requests.RequestException as exc:
                self.logger.debug(
                    f"Kemono preview fallback failed via {host}: {exc}"
                )
                continue
            except OSError as exc:
                self.logger.warning(
                    f"Failed to write Kemono preview fallback file {dest}: {exc}"
                )
                return None

            if not self._validate_files_with_pillow([downloaded]):
                continue

            self.logger.info(
                f"Kemono preview fallback downloaded {downloaded.name} via {host}"
            )
            return downloaded

        self.logger.warning(
            f"Kemono preview fallback failed for {Path(urlparse(original_url).path).name}"
        )
        return None

    def _cdn_fallback_urls(self, original_url: str) -> List[str]:
        parsed = urlparse(original_url)
        if not parsed.path.startswith('/data/'):
            return [original_url]

        urls = []
        for host in self._cdn_fallback_hosts():
            urls.append(urlunparse(parsed._replace(netloc=host)))
        return urls

    def _cdn_fallback_hosts(self) -> List[str]:
        configured_hosts = self.kemono_config.get('cdn_fallback_hosts')
        hosts = configured_hosts or list(_DEFAULT_CDN_FALLBACK_HOSTS)
        fallback_hosts = []
        seen = set()
        for host in hosts:
            if not host or host in seen:
                continue
            seen.add(host)
            fallback_hosts.append(host)
        return fallback_hosts

    def _preview_fallback_urls(self, original_url: str) -> List[str]:
        parsed = urlparse(original_url)
        path = parsed.path
        if path.startswith('/data/'):
            thumbnail_path = f"/thumbnail{path}"
        elif _KEMONO_HASH_RE.search(path):
            thumbnail_path = f"/thumbnail/data{path}"
        else:
            return []

        configured_hosts = self.kemono_config.get('preview_fallback_hosts')
        hosts = configured_hosts or list(_DEFAULT_PREVIEW_FALLBACK_HOSTS)
        urls = []
        seen = set()
        for host in hosts:
            if not host or host in seen:
                continue
            seen.add(host)
            urls.append(urlunparse(parsed._replace(netloc=host, path=thumbnail_path)))
        return urls

    def _cdn_host_available(self, host: str) -> bool:
        return time.monotonic() >= self._cdn_bad_until.get(host, 0.0)

    def _mark_cdn_host_failure(self, host: str, exc: requests.RequestException) -> None:
        cooldown = int(self.kemono_config.get('cdn_fallback_cooldown_seconds', 600))
        self._cdn_bad_until[host] = time.monotonic() + cooldown
        self.logger.warning(
            f"Kemono CDN host {host} failed; suppressing retries for "
            f"{cooldown}s: {exc}"
        )
        if self._all_cdn_fallback_hosts_unavailable():
            self._activate_cdn_outage_preview_only()

    def _all_cdn_fallback_hosts_unavailable(self) -> bool:
        hosts = self._cdn_fallback_hosts()
        return bool(hosts) and all(not self._cdn_host_available(host) for host in hosts)

    def _activate_cdn_outage_preview_only(self) -> None:
        if self._cdn_outage_preview_only:
            return
        self._cdn_outage_preview_only = True
        self.logger.warning(
            "Kemono CDN outage detected; using preview-only downloads for "
            "the rest of this crawl cycle"
        )

    @staticmethod
    def _is_cdn_host_failure(exc: requests.RequestException) -> bool:
        if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError)):
            return True
        response = getattr(exc, 'response', None)
        return response is not None and response.status_code >= 500

    def _download_candidate_url(self, url: str, dest: Path) -> Path:
        connect_timeout = float(self.kemono_config.get('cdn_fallback_connect_timeout', 8))
        read_timeout = float(self.kemono_config.get('cdn_fallback_read_timeout', 60))
        response = self._cdn_session.get(
            url,
            timeout=(connect_timeout, read_timeout),
            stream=True,
        )
        try:
            response.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_name(dest.name + '.part')
            with tmp_dest.open('wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            tmp_dest.replace(dest)
            return dest
        finally:
            response.close()

    def _download_preview_candidate_url(self, url: str, dest: Path) -> Path:
        connect_timeout = float(self.kemono_config.get('preview_fallback_connect_timeout', 3))
        read_timeout = float(self.kemono_config.get('preview_fallback_read_timeout', 15))
        response = self._cdn_session.get(
            url,
            timeout=(connect_timeout, read_timeout),
            stream=True,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        try:
            response.raise_for_status()
            content_type = (response.headers.get('content-type') or '').split(';', 1)[0].lower()
            if content_type and not content_type.startswith('image/'):
                raise requests.HTTPError(
                    f"Unexpected preview content-type: {content_type}",
                    response=response,
                )

            dest = self._with_content_type_suffix(dest, content_type)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_name(dest.name + '.part')
            with tmp_dest.open('wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            tmp_dest.replace(dest)
            return dest
        finally:
            response.close()

    @staticmethod
    def _with_content_type_suffix(dest: Path, content_type: str) -> Path:
        suffix_by_type = {
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'image/webp': '.webp',
            'image/gif': '.gif',
            'image/bmp': '.bmp',
            'image/tiff': '.tiff',
        }
        suffix = suffix_by_type.get(content_type)
        if not suffix or dest.suffix.lower() == suffix:
            return dest
        return dest.with_suffix(suffix)

    def _order_paths_with_preview_fallbacks(
        self,
        file_paths: List[Path],
        work_hashes: Dict[str, str],
        post_id: str,
    ) -> List[Path]:
        expected_order = list(work_hashes.values())
        hash_to_index = {h: index for index, h in enumerate(expected_order)}
        ordered_slots: Dict[int, Path] = {}
        extras: List[Path] = []

        for file_path in file_paths:
            try:
                actual_hash = self._sha256_file(file_path)
            except OSError:
                extras.append(file_path)
                continue

            if actual_hash in hash_to_index:
                ordered_slots[hash_to_index[actual_hash]] = file_path
                continue

            preview_index = self._preview_fallback_index(file_path, post_id)
            if preview_index is not None:
                ordered_slots.setdefault(preview_index, file_path)
            else:
                extras.append(file_path)

        ordered = [
            ordered_slots[index]
            for index in range(len(expected_order))
            if index in ordered_slots
        ]
        ordered.extend(extras)
        return ordered

    @staticmethod
    def _preview_fallback_index(file_path: Path, post_id: str) -> Optional[int]:
        match = re.match(
            rf"^{re.escape(post_id)}_(\d+)_preview\.",
            file_path.name,
            re.IGNORECASE,
        )
        if not match:
            return None
        return max(int(match.group(1)) - 1, 0)

    def _move_to_images_dir_with_mapping(
        self, files: List[Path], dir_name: str
    ) -> Dict[Path, Path]:
        """ダウンロードしたファイルをimages/videosディレクトリに移動"""
        mapping: Dict[Path, Path] = {}
        try:
            images_base, videos_base = get_media_base_paths(self.config)

            images_dir = images_base / dir_name
            videos_dir = videos_base / dir_name
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

            self.logger.info(
                f"Moved {len(mapping)} kemono media files for {dir_name}"
            )

        except Exception as e:
            self.logger.error(f"Failed to move kemono files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")

    # ------------------------------------------------------------------
    # ZIP extraction
    # ------------------------------------------------------------------

    _ZIP_IMAGE_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.webp',
        '.bmp', '.tiff', '.tif', '.avif', '.jfif',
    }

    @staticmethod
    def _natural_sort_key(s: str):
        """自然順ソート用キー（数値部分を数値として比較）"""
        return [
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', s)
        ]

    def _extract_zip_files(
        self, files_by_work: Dict[str, List[Path]]
    ) -> Dict[str, List[Path]]:
        """
        files_by_work 内の ZIP ファイルを検出し、画像を展開して置換する。

        ZIP 内の画像ファイルのみ展開し、展開後に ZIP を削除する。
        展開に失敗した場合は ZIP パスをそのまま残す（graceful degradation）。
        """
        result: Dict[str, List[Path]] = {}
        for wid, file_paths in files_by_work.items():
            new_paths: List[Path] = []
            for fp in file_paths:
                if fp.suffix.lower() != '.zip':
                    new_paths.append(fp)
                    continue

                try:
                    extracted = self._extract_single_zip(fp)
                    if extracted:
                        new_paths.extend(extracted)
                        try:
                            fp.unlink()
                        except Exception:
                            pass
                    else:
                        self.logger.warning(
                            f"ZIP contains no image files: {fp.name}"
                        )
                        new_paths.append(fp)
                except Exception as e:
                    self.logger.warning(
                        f"ZIP extraction failed for {fp.name}: {e}"
                    )
                    new_paths.append(fp)

            result[wid] = new_paths
        return result

    def _extract_single_zip(self, zip_path: Path) -> List[Path]:
        """
        単一 ZIP から画像ファイルを展開する。

        展開先は ZIP と同じディレクトリ。
        命名: {zip_stem}_{inner_num:03d}.{ext}
        例: 1119929_01.zip → 1119929_01_001.jpg, 1119929_01_002.png, ...
        """
        extract_dir = zip_path.parent
        zip_prefix = zip_path.stem  # e.g. "1119929_01"

        extracted_paths: List[Path] = []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            image_members = sorted(
                (
                    m for m in zf.namelist()
                    if not m.endswith('/')
                    and Path(m).suffix.lower() in self._ZIP_IMAGE_EXTENSIONS
                ),
                key=self._natural_sort_key,
            )
            if not image_members:
                return []

            self.logger.info(
                f"Extracting {len(image_members)} images from {zip_path.name}"
            )

            for idx, member in enumerate(image_members, 1):
                ext = Path(member).suffix.lower()
                new_name = f"{zip_prefix}_{idx:03d}{ext}"
                target = extract_dir / new_name

                if target.exists():
                    counter = 1
                    while target.exists():
                        new_name = f"{zip_prefix}_{idx:03d}_{counter}{ext}"
                        target = extract_dir / new_name
                        counter += 1

                target.write_bytes(zf.read(member))
                extracted_paths.append(target)

        return extracted_paths

    # ------------------------------------------------------------------
    # Hash / integrity validation
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_sha256(file_path: Path) -> str:
        """ファイルのSHA256ハッシュを計算"""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _validate_downloaded_files(
        self,
        files_by_work: Dict[str, List[Path]],
        hash_map: Dict[str, Dict[str, str]],
    ) -> Tuple[Dict[str, List[Path]], List[str]]:
        """
        ダウンロード済みファイルをSHA256ハッシュで検証し、メタデータ順に並べ替え

        gallery-dlのKemonoエクストラクターはattachmentsを先、メインfile(カバー)を
        最後に出力する。hash_mapの挿入順序（file→attachments = 元プラットフォーム
        の表示順）を基にファイルを正しい順序で並べ替える。

        Args:
            files_by_work: work_id -> [file_paths]
            hash_map: work_id -> {url: expected_sha256} (挿入順=正しい表示順)

        Returns:
            (検証済み・並べ替え済みfiles_by_work, 1つ以上失敗したwork_idリスト)
        """
        validated: Dict[str, List[Path]] = {}
        failed_work_ids: List[str] = []

        for wid, file_paths in files_by_work.items():
            work_hashes = hash_map.get(wid, {})
            if not work_hashes:
                # ハッシュ情報なし → Pillowフォールバック
                good_files = self._validate_files_with_pillow(file_paths)
                if good_files:
                    validated[wid] = good_files
                continue

            expected_set = set(work_hashes.values())
            # メタデータの挿入順序を保持（file先頭、attachments後続）
            expected_order = list(work_hashes.values())
            hash_to_file: Dict[str, Path] = {}
            unmatched_files: List[Tuple[Path, str]] = []

            for file_path in file_paths:
                actual_hash = self._calculate_sha256(file_path)
                if actual_hash in expected_set:
                    hash_to_file[actual_hash] = file_path
                    self.logger.debug(
                        f"Hash OK: {file_path.name} ({actual_hash[:16]}...)"
                    )
                else:
                    unmatched_files.append((file_path, actual_hash))

            # 期待ハッシュのうち未マッチのものを算出
            unmatched_expected = expected_set - set(hash_to_file.keys())
            has_failure = False
            pillow_fallback_files: List[Path] = []

            for file_path, actual_hash in unmatched_files:
                if unmatched_expected:
                    # まだマッチすべきハッシュが残っている → 真のミスマッチ
                    has_failure = True
                    self.logger.warning(
                        f"Hash MISMATCH: {file_path.name} "
                        f"actual={actual_hash[:16]}... "
                        f"expected={[h[:16] for h in expected_set]}"
                    )
                    try:
                        file_path.unlink()
                    except Exception:
                        pass
                else:
                    # 全期待ハッシュはマッチ済み → このファイルはハッシュ情報なし
                    self.logger.debug(
                        f"No expected hash for {file_path.name}, "
                        f"using Pillow fallback"
                    )
                    pillow_fallback_files.append(file_path)

            # Pillow検証（ハッシュ情報のないファイル）
            pillow_ok = self._validate_files_with_pillow(
                pillow_fallback_files
            ) if pillow_fallback_files else []

            # メタデータ順（file→attachments）に並べ替え
            good_files = []
            for h in expected_order:
                if h in hash_to_file:
                    good_files.append(hash_to_file[h])
            # expected_orderにないが検証OKだったファイルも追加
            for h, fp in hash_to_file.items():
                if fp not in good_files:
                    good_files.append(fp)
            # ハッシュ情報なし・Pillow検証OKのファイルを末尾に追加
            good_files.extend(pillow_ok)

            if good_files:
                validated[wid] = good_files
            if has_failure:
                failed_work_ids.append(wid)

        return validated, failed_work_ids

    def _validate_files_with_pillow(self, file_paths: List[Path]) -> List[Path]:
        """ハッシュ情報がないファイルをPillowで検証（フォールバック）"""
        try:
            from PIL import Image
        except ImportError:
            return file_paths

        image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff', '.tif'}
        good_files = []

        for fp in file_paths:
            if fp.suffix.lower() not in image_exts:
                good_files.append(fp)
                continue
            try:
                with Image.open(fp) as img:
                    img.load()
                good_files.append(fp)
            except Exception as e:
                self.logger.warning(f"Pillow検証失敗: {fp.name} ({e})")
                try:
                    fp.unlink()
                except Exception:
                    pass

        return good_files
