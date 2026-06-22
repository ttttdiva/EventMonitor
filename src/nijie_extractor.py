#!/usr/bin/env python3
"""
ニジエ作品取得・メディアダウンロード
gallery-dlを使用してnijie.infoの作品メタデータとメディアを取得
Cookie認証（nijie_tok）を使用
"""

import sys
import json
import re
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


class NijieExtractor:
    """gallery-dlを使用してニジエ作品を取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.NijieExtractor")
        self.nijie_config = config.get('nijie', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'nijie'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # Cookieファイル
        self.cookie_file = Path('cookies/nijie.info_cookies.txt')

        # バッチサイズ
        batch_cfg = self.nijie_config.get('max_batch_size', 50)
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """
        表示名を取得（1作品だけ取得してメタデータから抽出）
        """
        works = self.fetch_user_works(user_id, limit=1)
        if not works:
            self.logger.warning(
                f"No works found for nijie user {user_id}, cannot resolve display name"
            )
            return None

        display_name = works[0].get('display_name', '')
        if display_name:
            self.logger.info(f"Resolved nijie user {user_id} -> {display_name}")
            return display_name

        self.logger.warning(
            f"Display name not found in metadata for nijie user {user_id}"
        )
        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """軽量リチェック: gallery-dl --range 1-1 でアカウントの到達性を確認"""
        url = f"https://nijie.info/members_illust.php?id={user_id}"
        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '--cookies', str(self.cookie_file),
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
                    f"Nijie user {user_id} appears unreachable: "
                    f"{result.stderr[:200]}"
                )
                return False

            return True

        except subprocess.TimeoutExpired:
            return True
        except Exception as e:
            self.logger.error(
                f"Error checking nijie reachability for {user_id}: {e}"
            )
            return True

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        指定ニジエユーザーの作品メタデータを取得

        Args:
            user_id: ニジエユーザーID
            limit: 取得件数制限

        Returns:
            作品情報のリスト
        """
        # members_illust（イラスト）を取得
        url = f"https://nijie.info/members_illust.php?id={user_id}"

        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '--cookies', str(self.cookie_file),
            '-o', 'sleep-request=6',
            '-v',
            '-j',
        ]

        if limit:
            cmd.extend(['--range', f'1-{limit}'])

        cmd.append(url)

        self.logger.info(
            f"Fetching nijie works for user {user_id} (limit: {limit or 'all'})"
        )

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(
                    f"gallery-dl stderr for nijie {user_id}: "
                    f"{result.stderr[:500]}"
                )

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for nijie {user_id}: "
                    f"returncode={result.returncode} stderr={result.stderr[:300]}"
                )
                stderr_lower = (result.stderr or "").lower()
                not_found_patterns = [
                    "404", "not found", "no results", "does not exist"
                ]
                if any(p in stderr_lower for p in not_found_patterns):
                    self._account_reachable[user_id] = False
                return []

            output = result.stdout.strip()
            if not output:
                self.logger.info(
                    f"No output from gallery-dl for nijie {user_id}"
                )
                self._account_reachable[user_id] = True
                return []

            self._account_reachable[user_id] = True
            return self._parse_gallery_dl_output(output)

        except subprocess.TimeoutExpired:
            self.logger.error(
                f"Timeout fetching nijie works for user {user_id}"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error fetching nijie works: {e}")
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
            user_id: ニジエユーザーID
            work_ids: ダウンロード対象のimage_idリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            作品IDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)

        # ダウンロード前のファイルを記録
        existing_files = set()
        if self.media_dir.exists():
            for f in self.media_dir.rglob('*'):
                if f.is_file():
                    existing_files.add(f)

        remaining = list(work_ids)
        all_media_paths: Dict[str, List[Path]] = {}
        all_downloaded: List[Path] = []
        consecutive_no_progress = 0
        max_no_progress = 5
        attempt = 0

        while remaining and consecutive_no_progress < max_no_progress:
            attempt += 1
            current_batch = remaining[:self.batch_size]

            self.logger.info(
                f"Attempt {attempt}: Downloading media for {len(current_batch)} of "
                f"{len(remaining)} remaining nijie works"
            )

            url_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False
                ) as url_file:
                    for image_id in current_batch:
                        url_file.write(
                            f"https://nijie.info/view.php?id={image_id}\n"
                        )
                    url_file_path = url_file.name

                cmd = [
                    sys.executable,
                    str(self.wrapper_path),
                    '--cookies', str(self.cookie_file),
                    '-d', str(self.media_dir),
                    '-o', 'filename={image_id}_p{num}.{extension}',
                    '-o', 'sleep-request=6',
                    '-v',
                    '--input-file', url_file_path,
                ]

                result = run_with_idle_timeout(cmd, idle_timeout=180, rate_limit_retries=0)

                if result.returncode != 0:
                    self.logger.warning(
                        f"gallery-dl issues: {result.stderr[:200]}"
                    )

                new_files_by_work = self._collect_downloaded_files(
                    current_batch, self.media_dir, existing_files
                )

                if new_files_by_work:
                    total_files = sum(
                        len(fs) for fs in new_files_by_work.values()
                    )
                    self.logger.info(
                        f"Downloaded {total_files} files for "
                        f"{len(new_files_by_work)} works"
                    )

                    all_media_paths.update(new_files_by_work)
                    for fs in new_files_by_work.values():
                        all_downloaded.extend(fs)

                    remaining = [
                        wid for wid in remaining
                        if wid not in new_files_by_work
                    ]
                    for fs in new_files_by_work.values():
                        existing_files.update(fs)

                    consecutive_no_progress = 0
                else:
                    self.logger.warning(f"No new files in attempt {attempt}")
                    consecutive_no_progress += 1

            except subprocess.TimeoutExpired:
                self.logger.warning(f"Timeout in attempt {attempt}")
                partial = self._collect_downloaded_files(
                    current_batch, self.media_dir, existing_files
                )
                if partial:
                    all_media_paths.update(partial)
                    for fs in partial.values():
                        all_downloaded.extend(fs)
                    remaining = [
                        wid for wid in remaining if wid not in partial
                    ]
                    for fs in partial.values():
                        existing_files.update(fs)
                    consecutive_no_progress = 0
                else:
                    consecutive_no_progress += 1

            except Exception as e:
                self.logger.error(f"Error in attempt {attempt}: {e}")
                consecutive_no_progress += 1

            finally:
                if url_file_path:
                    try:
                        os.unlink(url_file_path)
                    except Exception:
                        pass

        if remaining:
            self.logger.error(
                f"Failed to download media for {len(remaining)} nijie works"
            )

        # ファイルをimages/videosディレクトリに移動
        dir_name = f"nijie_{user_id}"
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

    def _parse_gallery_dl_output(
        self, output: str
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
                    # Type 2: ディレクトリメタデータ
                    work_info = self._extract_work_info(item[1])
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
                    image_id = str(media_meta.get('image_id', ''))
                    if image_id:
                        if image_id not in works:
                            work_info = self._extract_work_info(media_meta)
                            if work_info:
                                works[image_id] = work_info
                        if image_id in works and media_url not in works[image_id]['media']:
                            works[image_id]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(
                f"Failed to parse gallery-dl nijie output: {e}"
            )
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} nijie works")
        return result

    def _extract_work_info(
        self, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータから作品情報を抽出"""
        try:
            image_id = data.get('image_id')
            if not image_id:
                return None

            image_id = str(image_id)

            # 日付パース
            date_val = data.get('date')
            if date_val:
                if isinstance(date_val, str):
                    for fmt in (
                        "%a %b %d %H:%M:%S %Y",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S%z",
                    ):
                        try:
                            dt = datetime.strptime(date_val, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        dt = datetime.now()
                elif isinstance(date_val, (int, float)):
                    dt = datetime.fromtimestamp(date_val)
                else:
                    dt = datetime.now()
                date_iso = dt.isoformat() if dt.tzinfo else dt.isoformat() + 'Z'
            else:
                date_iso = datetime.now().isoformat() + 'Z'

            # ユーザー情報
            display_name = (
                data.get('artist_name', '')
                or data.get('user_name', '')
            )

            # タグ
            tags = data.get('tags', [])
            if isinstance(tags, list):
                tags = [t.strip() if isinstance(t, str) else str(t) for t in tags if t]
            else:
                tags = []

            # ニジエは全コンテンツがR-18のため常にTrue
            sensitive = True

            return {
                'id': image_id,
                'display_name': display_name,
                'text': data.get('title', ''),
                'description': data.get('description', ''),
                'date': date_iso,
                'url': f"https://nijie.info/view.php?id={image_id}",
                'media': [],  # Type 3エントリで追加される
                'tags': tags,
                'sensitive': sensitive,
                'artist_id': str(data.get('artist_id', '')),
                'source': 'nijie',
                'platform': 'nijie',
            }

        except Exception as e:
            self.logger.error(f"Error extracting nijie work info: {e}")
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
            # ファイル名パターン: {image_id}_p{num}.ext
            for image_id in work_ids:
                if (filename.startswith(f"{image_id}_p")
                        or filename.startswith(f"{image_id}.")):
                    if image_id not in new_files_by_work:
                        new_files_by_work[image_id] = []
                    new_files_by_work[image_id].append(f)
                    break

        # ページ番号順でソート（_p{num}パターンを数値として比較）
        for wid in new_files_by_work:
            new_files_by_work[wid].sort(
                key=lambda f: int(m.group(1)) if (m := re.search(r'_p(\d+)', f.name)) else 0
            )

        return new_files_by_work

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
                f"Moved {len(mapping)} nijie media files for {dir_name}"
            )

        except Exception as e:
            self.logger.error(f"Failed to move nijie files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
