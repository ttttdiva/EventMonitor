#!/usr/bin/env python3
"""
Misskey投稿取得・メディアダウンロード
gallery-dlを使用してMisskey系インスタンスのノートメタデータとメディアを取得
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


class MisskeyExtractor:
    """gallery-dlを使用してMisskeyノートを取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.MisskeyExtractor")
        self.misskey_config = config.get('misskey', {})
        self.default_instance = (
            str(self.misskey_config.get('default_instance', 'misskey.io')).strip().lower()
            or 'misskey.io'
        )

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'misskey'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # Cookieファイル
        configured_cookie = self.misskey_config.get('cookies_file')
        if configured_cookie:
            self.default_cookie_file = Path(configured_cookie)
        else:
            self.default_cookie_file = Path('cookies') / f'{self.default_instance}_cookies.txt'
        self.cookie_dir = Path(self.misskey_config.get('cookie_dir', 'cookies'))

        # バッチサイズ
        batch_cfg = self.misskey_config.get('max_batch_size', 50)
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
        表示名を取得（1ノートだけ取得してメタデータから抽出）

        Args:
            user_id: Misskeyユーザー名

        Returns:
            display name（取得できた場合）、取得失敗時はNone
        """
        works = self.fetch_user_works(user_id, limit=1)
        if not works:
            self.logger.warning(
                f"No notes found for misskey user {user_id}, cannot resolve display name"
            )
            return None

        display_name = works[0].get('display_name', '')
        if display_name:
            self.logger.info(f"Resolved misskey user {user_id} -> {display_name}")
            return display_name

        self.logger.warning(
            f"Display name not found in metadata for misskey user {user_id}"
        )
        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """軽量リチェック: gallery-dl --range 1-1 でアカウントの到達性を確認"""
        # /notes サフィックスが必要（MisskeyUserExtractorのdispatch型はDataJobで機能しないため）
        _, instance_host = self._split_account_identifier(user_id)
        url = self._build_profile_url(user_id)
        cmd = self._build_gallery_dl_cmd(
            instance_host, '-q', '-j', '--range', '1-1', url
        )

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
                    f"Misskey user {user_id} appears unreachable: "
                    f"{result.stderr[:200]}"
                )
                return False

            # 明確な削除シグナルなし → 一時的エラーとして到達可能扱い
            return True

        except subprocess.TimeoutExpired:
            return True  # タイムアウトは一時的
        except Exception as e:
            self.logger.error(
                f"Error checking misskey reachability for {user_id}: {e}"
            )
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def _split_account_identifier(self, user_id: str) -> Tuple[str, str]:
        """`username` または `username@host` を `(username, host)` に分解"""
        raw = (user_id or '').strip()
        if raw.startswith('@'):
            raw = raw[1:]
        if not raw:
            return '', self.default_instance

        if '@' in raw:
            username, host = raw.rsplit('@', 1)
            username = username.strip().lstrip('@')
            host = host.strip().lower()
            if username and host:
                return username, host

        return raw, self.default_instance

    def _build_profile_url(self, user_id: str) -> str:
        username, instance_host = self._split_account_identifier(user_id)
        return f"https://{instance_host}/@{username}/notes"

    def _build_note_url(self, note_id: str, instance_host: Optional[str]) -> str:
        host = (instance_host or self.default_instance).strip().lower() or self.default_instance
        return f"https://{host}/notes/{note_id}"

    def _build_work_id(self, note_id: str, instance_host: Optional[str]) -> str:
        note_id = str(note_id)
        host = (instance_host or self.default_instance).strip().lower() or self.default_instance
        if host == self.default_instance:
            return note_id
        return f"{host}:{note_id}"

    def _extract_note_id(self, work_id: str, instance_host: Optional[str]) -> str:
        raw = str(work_id)
        host = (instance_host or self.default_instance).strip().lower() or self.default_instance
        prefix = f"{host}:"
        if host != self.default_instance and raw.startswith(prefix):
            return raw[len(prefix):]
        return raw

    def _resolve_cookie_file(self, instance_host: Optional[str]) -> Optional[Path]:
        host = (instance_host or self.default_instance).strip().lower() or self.default_instance
        candidates: List[Path] = []
        if host == self.default_instance:
            candidates.append(self.default_cookie_file)
        host_cookie = self.cookie_dir / f'{host}_cookies.txt'
        if host_cookie not in candidates:
            candidates.append(host_cookie)

        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _build_gallery_dl_cmd(self, instance_host: Optional[str], *args: str) -> List[str]:
        cmd = [sys.executable, str(self.wrapper_path)]
        cookie_file = self._resolve_cookie_file(instance_host)
        if cookie_file:
            cmd.extend(['--cookies', str(cookie_file)])
        return cmd + list(args)

    def fetch_user_works(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        指定Misskeyユーザーのノートメタデータを取得

        Args:
            user_id: Misskeyユーザー名
            limit: 取得件数制限

        Returns:
            ノート情報のリスト
        """
        # /notes サフィックスが必要（MisskeyUserExtractorのdispatch型はDataJobで機能しないため）
        _, instance_host = self._split_account_identifier(user_id)
        url = self._build_profile_url(user_id)

        cmd = self._build_gallery_dl_cmd(instance_host, '-v', '-j')

        if limit:
            cmd.extend(['--range', f'1-{limit}'])

        cmd.append(url)

        self.logger.info(
            f"Fetching misskey notes for user {user_id} (limit: {limit or 'all'})"
        )

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(
                    f"gallery-dl stderr for misskey {user_id}: "
                    f"{result.stderr[:500]}"
                )

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for misskey {user_id}: "
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
                    f"No output from gallery-dl for misskey {user_id}"
                )
                self._account_reachable[user_id] = True
                return []

            self._account_reachable[user_id] = True
            return self._parse_gallery_dl_output(output, instance_host)

        except subprocess.TimeoutExpired:
            self.logger.error(
                f"Timeout fetching misskey notes for user {user_id}"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error fetching misskey notes: {e}")
            return []

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定のノートIDのメディアをダウンロード

        Args:
            user_id: Misskeyユーザー名
            work_ids: ダウンロード対象のnote IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            ノートIDごとのメディアファイルパスの辞書
        """
        if not work_ids:
            return {}

        _, instance_host = self._split_account_identifier(user_id)
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
            current_batch_map = {
                work_id: self._extract_note_id(work_id, instance_host)
                for work_id in current_batch
            }

            self.logger.info(
                f"Attempt {attempt}: Downloading media for {len(current_batch)} of "
                f"{len(remaining)} remaining misskey notes"
            )

            url_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False
                ) as url_file:
                    for note_id in current_batch_map.values():
                        url_file.write(f"{self._build_note_url(note_id, instance_host)}\n")
                    url_file_path = url_file.name

                cmd = self._build_gallery_dl_cmd(
                    instance_host,
                    '-d', str(self.media_dir),
                    '-o', 'filename={id}_{num}.{extension}',
                    '-v',
                    '--input-file', url_file_path,
                )

                result = run_with_idle_timeout(cmd, idle_timeout=180, rate_limit_retries=0)

                if result.returncode != 0:
                    self.logger.warning(
                        f"gallery-dl issues: {result.stderr[:200]}"
                    )

                new_files_by_work = self._collect_downloaded_files(
                    current_batch_map, self.media_dir, existing_files
                )

                if new_files_by_work:
                    total_files = sum(
                        len(fs) for fs in new_files_by_work.values()
                    )
                    self.logger.info(
                        f"Downloaded {total_files} files for "
                        f"{len(new_files_by_work)} notes"
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
                    current_batch_map, self.media_dir, existing_files
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
                f"Failed to download media for {len(remaining)} misskey notes"
            )

        # ファイルをimages/videosディレクトリに移動
        dir_name = f"misskey_{user_id}"
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
        self, output: str, instance_host: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """gallery-dlのJSON出力をパースしてノート情報リストを返す"""
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
                    work_info = self._extract_work_info(item[1], instance_host)
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
                    note_id = str(media_meta.get('id', ''))
                    if note_id:
                        work_id = self._build_work_id(note_id, instance_host)
                        if work_id not in works:
                            work_info = self._extract_work_info(media_meta, instance_host)
                            if work_info:
                                works[work_id] = work_info
                        if work_id in works and media_url not in works[work_id]['media']:
                            works[work_id]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(
                f"Failed to parse gallery-dl misskey output: {e}"
            )
            return []

        # リノートをフィルタリング（renoteId が設定されているノートは除外）
        filtered = {
            wid: w for wid, w in works.items()
            if not w.get('_is_renote', False)
        }

        result = list(filtered.values())
        self.logger.info(
            f"Parsed {len(result)} misskey notes "
            f"(filtered {len(works) - len(filtered)} renotes)"
        )
        return result

    def _extract_work_info(
        self, data: Dict[str, Any], instance_host: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータからノート情報を抽出"""
        try:
            note_id = data.get('id')
            if not note_id:
                return None

            note_id = str(note_id)

            # リノート判定
            is_renote = data.get('renoteId') is not None

            # 投稿日時（MisskeyはcreatedAtを返す）
            created_at = data.get('createdAt', '')
            if created_at:
                date_iso = created_at
            else:
                # フォールバック: dateフィールド（gallery-dlがフォーマットしたもの）
                date_obj = data.get('date')
                if date_obj:
                    if isinstance(date_obj, (int, float)):
                        date_iso = datetime.utcfromtimestamp(date_obj).isoformat() + 'Z'
                    else:
                        date_iso = str(date_obj)
                else:
                    date_iso = datetime.now().isoformat() + 'Z'

            # ユーザー情報
            user = data.get('user', {})
            display_name = ''
            misskey_username = ''
            if isinstance(user, dict):
                display_name = user.get('name', '') or user.get('username', '')
                misskey_username = user.get('username', '')

            # テキスト
            text = data.get('text', '') or ''

            # センシティブ判定:
            # 1. cw (content warning) フィールドが設定されている → sensitive
            # 2. ファイルレベルの isSensitive は Type 3 エントリで確認
            cw = data.get('cw')
            sensitive = cw is not None and cw != ''

            # タグ（ハッシュタグ）
            tags = data.get('tags', [])
            if isinstance(tags, list):
                tags = [t if isinstance(t, str) else str(t) for t in tags if t]
            else:
                tags = []

            return {
                'id': self._build_work_id(note_id, instance_host),
                'note_id': note_id,
                'instance_host': (instance_host or self.default_instance),
                'username': misskey_username,
                'display_name': display_name,
                'text': text,
                'date': date_iso,
                'url': self._build_note_url(note_id, instance_host),
                'media': [],  # Type 3エントリで追加される
                'tags': tags,
                'sensitive': sensitive,
                'source': 'misskey',
                'platform': 'misskey',
                '_is_renote': is_renote,
            }

        except Exception as e:
            self.logger.error(f"Error extracting misskey note info: {e}")
            return None

    def _collect_downloaded_files(
        self,
        work_id_to_note_id: Dict[str, str],
        output_dir: Path,
        existing_files: set,
    ) -> Dict[str, List[Path]]:
        """ダウンロード済みファイルをノートIDごとに収集"""
        new_files_by_work: Dict[str, List[Path]] = {}
        if not output_dir.exists():
            return new_files_by_work

        for f in output_dir.rglob('*'):
            if not f.is_file() or f in existing_files:
                continue

            filename = f.name
            # ファイル名パターン: {id}_{num}.ext
            for work_id, note_id in work_id_to_note_id.items():
                if (filename.startswith(f"{note_id}_")
                        or filename.startswith(f"{note_id}.")):
                    if work_id not in new_files_by_work:
                        new_files_by_work[work_id] = []
                    new_files_by_work[work_id].append(f)
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
                f"Moved {len(mapping)} misskey media files for {dir_name}"
            )

        except Exception as e:
            self.logger.error(f"Failed to move misskey files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
