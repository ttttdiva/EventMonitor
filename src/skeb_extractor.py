#!/usr/bin/env python3
"""
Skeb作品取得・メディアダウンロード
gallery-dlを使用してSkeb.jpの作品メタデータとメディアを取得
Cookie認証を使用
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
from typing import List, Dict, Any, Optional, Tuple

from .path_utils import get_media_base_paths
from .subprocess_utils import run_with_idle_timeout


class SkebExtractor:
    """gallery-dlを使用してSkeb作品を取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.SkebExtractor")
        self.skeb_config = config.get('skeb', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'skeb'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # Cookieファイル
        self.cookie_file = Path('cookies/skeb.jp_cookies.txt')

        # バッチサイズ
        batch_cfg = self.skeb_config.get('max_batch_size', 50)
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

        # post_id → post_num マッピング（download_media_for_worksでURL構築に使用）
        self._post_id_to_num: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """
        表示名を取得（1作品だけ取得してメタデータから抽出）

        Args:
            user_id: Skebスクリーンネーム

        Returns:
            display name（取得できた場合）、取得失敗時はNone
        """
        works = self.fetch_user_works(user_id, limit=1)
        if not works:
            self.logger.warning(
                f"No works found for skeb user {user_id}, cannot resolve display name"
            )
            return None

        display_name = works[0].get('display_name', '')
        if display_name:
            self.logger.info(f"Resolved skeb user {user_id} -> {display_name}")
            return display_name

        self.logger.warning(
            f"Display name not found in metadata for skeb user {user_id}"
        )
        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """軽量リチェック: gallery-dl --range 1-1 でアカウントの到達性を確認"""
        # /works サフィックスが必要（SkebUserExtractorのdispatch型はDataJobで機能しないため）
        url = f"https://skeb.jp/@{user_id}/works"
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
                    f"Skeb user {user_id} appears unreachable: "
                    f"{result.stderr[:200]}"
                )
                return False

            # 明確な削除シグナルなし → 一時的エラーとして到達可能扱い
            return True

        except subprocess.TimeoutExpired:
            return True  # タイムアウトは一時的
        except Exception as e:
            self.logger.error(
                f"Error checking skeb reachability for {user_id}: {e}"
            )
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()
        self._post_id_to_num.clear()

    def fetch_user_works(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        指定Skebユーザーの作品メタデータを取得

        Args:
            user_id: Skebスクリーンネーム
            limit: 取得件数制限

        Returns:
            作品情報のリスト
        """
        # /works サフィックスが必要（SkebUserExtractorのdispatch型はDataJobで機能しないため）
        url = f"https://skeb.jp/@{user_id}/works"

        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '--cookies', str(self.cookie_file),
            '-v',
            '-j',
        ]

        if limit:
            cmd.extend(['--range', f'1-{limit}'])

        cmd.append(url)

        self.logger.info(
            f"Fetching skeb works for user {user_id} (limit: {limit or 'all'})"
        )

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(
                    f"gallery-dl stderr for skeb {user_id}: "
                    f"{result.stderr[:500]}"
                )

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for skeb {user_id}: "
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
                    f"No output from gallery-dl for skeb {user_id}"
                )
                self._account_reachable[user_id] = True
                return []

            self._account_reachable[user_id] = True
            return self._parse_gallery_dl_output(output)

        except subprocess.TimeoutExpired:
            self.logger.error(
                f"Timeout fetching skeb works for user {user_id}"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error fetching skeb works: {e}")
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
            user_id: Skebスクリーンネーム
            work_ids: ダウンロード対象のpost_idリスト
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
                f"{len(remaining)} remaining skeb works"
            )

            url_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False
                ) as url_file:
                    for post_id in current_batch:
                        # post_id → post_num マッピングからURL構築
                        post_num = self._post_id_to_num.get(post_id, post_id)
                        url_file.write(
                            f"https://skeb.jp/@{user_id}/works/{post_num}\n"
                        )
                    url_file_path = url_file.name

                cmd = [
                    sys.executable,
                    str(self.wrapper_path),
                    '--cookies', str(self.cookie_file),
                    '-d', str(self.media_dir),
                    '-o', 'filename={post_id}_{file_id}.{extension}',
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
                f"Failed to download media for {len(remaining)} skeb works"
            )

        # ファイルをimages/videosディレクトリに移動
        dir_name = f"skeb_{user_id}"
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
                    # Type 2: ディレクトリメタデータ [2, metadata_dict]
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
                    post_id = str(media_meta.get('post_id', ''))
                    if post_id:
                        if post_id not in works:
                            work_info = self._extract_work_info(media_meta)
                            if work_info:
                                works[post_id] = work_info
                        if post_id in works and media_url not in works[post_id]['media']:
                            works[post_id]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(
                f"Failed to parse gallery-dl skeb output: {e}"
            )
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} skeb works")
        return result

    def _extract_work_info(
        self, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータから作品情報を抽出"""
        try:
            post_id = data.get('post_id')
            if not post_id:
                return None

            post_id = str(post_id)
            post_num = str(data.get('post_num', ''))

            # post_id → post_num マッピングを保存（download URLの構築に使用）
            if post_num:
                self._post_id_to_num[post_id] = post_num

            # Skeb APIは投稿日時を返さないため、datetime.now()をフォールバックに使用
            date_iso = datetime.now().isoformat() + 'Z'

            # クリエイター情報
            creator = data.get('creator', {})
            display_name = creator.get('name', '') if isinstance(creator, dict) else ''

            # タグ
            tags = data.get('tags', [])
            if isinstance(tags, list):
                tags = [t if isinstance(t, str) else str(t) for t in tags if t]
            else:
                tags = []

            # センシティブ判定（nsfw == True）
            sensitive = bool(data.get('nsfw', False))

            return {
                'id': post_id,
                'post_num': post_num,
                'display_name': display_name,
                'text': data.get('body', ''),
                'date': date_iso,
                'url': data.get('post_url', f"https://skeb.jp/works/{post_num}"),
                'media': [],  # Type 3エントリで追加される
                'tags': tags,
                'sensitive': sensitive,
                'genre': data.get('genre', ''),
                'source': 'skeb',
                'platform': 'skeb',
            }

        except Exception as e:
            self.logger.error(f"Error extracting skeb work info: {e}")
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
            # ファイル名パターン: {post_id}_{file_id}.ext
            for post_id in work_ids:
                if (filename.startswith(f"{post_id}_")
                        or filename.startswith(f"{post_id}.")):
                    if post_id not in new_files_by_work:
                        new_files_by_work[post_id] = []
                    new_files_by_work[post_id].append(f)
                    break

        # ファイル名順でソート
        for wid in new_files_by_work:
            new_files_by_work[wid].sort(key=lambda f: f.name)

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
                f"Moved {len(mapping)} skeb media files for {dir_name}"
            )

        except Exception as e:
            self.logger.error(f"Failed to move skeb files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
