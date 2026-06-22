#!/usr/bin/env python3
"""
Gelbooru投稿取得・メディアダウンロード
gallery-dlを使用してGelbooruのタグ検索結果からメタデータとメディアを取得
APIキー認証（任意）
"""

import sys
import json
import subprocess
import logging
import os
import shutil
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from .path_utils import get_media_base_paths
from .subprocess_utils import run_with_idle_timeout


class GelbooruExtractor:
    """gallery-dlを使用してGelbooruタグ検索結果を取得"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.GelbooruExtractor")
        self.gelbooru_config = config.get('gelbooru', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'gelbooru'

        # ラッパースクリプトのパス
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # APIキー（環境変数優先、なければconfig.yaml）
        self.api_key = os.environ.get('GELBOORU_API_KEY') or self.gelbooru_config.get('api_key', '')
        self.api_user_id = os.environ.get('GELBOORU_USER_ID') or self.gelbooru_config.get('user_id', '')

        # バッチサイズ
        batch_cfg = self.gelbooru_config.get('max_batch_size', 50)
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # 最大取得件数
        limit_cfg = self.gelbooru_config.get('max_fetch_limit', 200)
        try:
            self.max_fetch_limit = max(1, int(limit_cfg))
        except (TypeError, ValueError):
            self.max_fetch_limit = 200

        # 検索クエリ到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """
        検索型なのでクエリそのものを返す

        Args:
            user_id: 検索クエリ文字列

        Returns:
            クエリ文字列をそのまま返す
        """
        return user_id

    def check_account_reachable(self, user_id: str) -> bool:
        """軽量チェック: gallery-dl --range 1-1 で検索クエリの有効性を確認"""
        url = self._build_search_url(user_id)
        cmd = self._build_base_cmd()
        cmd.extend(['-j', '--range', '1-1', url])

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
                    f"Gelbooru search '{user_id}' appears unreachable: "
                    f"{result.stderr[:200]}"
                )
                return False

            # 明確なエラーシグナルなし → 一時的エラーとして到達可能扱い
            return True

        except subprocess.TimeoutExpired:
            return True  # タイムアウトは一時的
        except Exception as e:
            self.logger.error(
                f"Error checking gelbooru reachability for '{user_id}': {e}"
            )
            return True  # エラーは一時的

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def fetch_user_works(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        指定タグクエリでGelbooru投稿メタデータを取得

        Args:
            user_id: Gelbooruタグ検索クエリ
            limit: 取得件数制限（Noneの場合はmax_fetch_limitを使用）

        Returns:
            投稿情報のリスト
        """
        effective_limit = limit or self.max_fetch_limit
        url = self._build_search_url(user_id)

        cmd = self._build_base_cmd()
        cmd.extend([
            '-j',
            # カテゴリ別タグ取得を有効化（tags_artist, tags_character等）
            '-o', 'extractor.gelbooru.tags=true',
        ])

        if effective_limit:
            cmd.extend(['--range', f'1-{effective_limit}'])

        cmd.append(url)

        self.logger.info(
            f"Fetching gelbooru posts for query '{user_id}' (limit: {effective_limit})"
        )

        try:
            result = run_with_idle_timeout(cmd, idle_timeout=120, rate_limit_retries=0)

            if result.stderr:
                self.logger.debug(
                    f"gallery-dl stderr for gelbooru '{user_id}': "
                    f"{result.stderr[:500]}"
                )

            if result.returncode != 0:
                self.logger.error(
                    f"gallery-dl error for gelbooru '{user_id}': "
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
                    f"No output from gallery-dl for gelbooru '{user_id}'"
                )
                self._account_reachable[user_id] = True
                return []

            self._account_reachable[user_id] = True
            return self._parse_gallery_dl_output(output)

        except subprocess.TimeoutExpired:
            self.logger.error(
                f"Timeout fetching gelbooru posts for query '{user_id}'"
            )
            return []
        except Exception as e:
            self.logger.error(f"Error fetching gelbooru posts: {e}")
            return []

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """
        特定の投稿IDのメディアをダウンロード

        Args:
            user_id: 検索クエリ（ディレクトリ名生成用）
            work_ids: ダウンロード対象のpost IDリスト
            move_to_images: imagesディレクトリに移動するか

        Returns:
            投稿IDごとのメディアファイルパスの辞書
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
                f"{len(remaining)} remaining gelbooru posts"
            )

            url_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False
                ) as url_file:
                    for post_id in current_batch:
                        # 個別投稿URL
                        url_file.write(
                            f"https://gelbooru.com/index.php?page=post&s=view&id={post_id}\n"
                        )
                    url_file_path = url_file.name

                cmd = self._build_base_cmd()
                cmd.extend([
                    '-d', str(self.media_dir),
                    '-o', 'filename={id}_{num}.{extension}',
                    '--input-file', url_file_path,
                ])

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
                        f"{len(new_files_by_work)} posts"
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
                f"Failed to download media for {len(remaining)} gelbooru posts"
            )

        # ファイルをimages/videosディレクトリに移動
        # 検索クエリからディレクトリ名を生成（安全な文字列に変換）
        safe_query = "".join(
            c if c.isalnum() or c in ('-', '_') else '_'
            for c in user_id
        )[:100]
        dir_name = f"gelbooru_{safe_query}"
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

    def _build_search_url(self, query: str) -> str:
        """検索クエリからGelbooru検索URLを構築"""
        encoded_tags = urllib.parse.quote(query, safe='')
        return f"https://gelbooru.com/index.php?page=post&s=list&tags={encoded_tags}"

    def _build_base_cmd(self) -> list:
        """gallery-dlの基本コマンドを構築"""
        cmd = [
            sys.executable,
            str(self.wrapper_path),
            '-v',
        ]
        # APIキー認証（任意）
        if self.api_key and self.api_user_id:
            cmd.extend(['-o', f'extractor.gelbooru.api-key={self.api_key}'])
            cmd.extend(['-o', f'extractor.gelbooru.user-id={self.api_user_id}'])
        return cmd

    def _parse_gallery_dl_output(
        self, output: str
    ) -> List[Dict[str, Any]]:
        """gallery-dlのJSON出力をパースして投稿情報リストを返す"""
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

                # Type -1: gallery-dlエラー [-1, {"error": ..., "message": ...}]
                if item_type == -1 and isinstance(item[1], dict):
                    err = item[1]
                    self.logger.error(
                        f"gallery-dl error: {err.get('error', 'Unknown')}: "
                        f"{err.get('message', '')}"
                    )
                    return []

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
                    post_id = str(media_meta.get('id', ''))
                    if post_id:
                        if post_id not in works:
                            work_info = self._extract_work_info(media_meta)
                            if work_info:
                                works[post_id] = work_info
                        if post_id in works and media_url not in works[post_id]['media']:
                            works[post_id]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(
                f"Failed to parse gallery-dl gelbooru output: {e}"
            )
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} gelbooru posts")
        return result

    def _extract_work_info(
        self, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """gallery-dlのデータから投稿情報を抽出"""
        try:
            post_id = data.get('id')
            if not post_id:
                return None

            post_id = str(post_id)

            # 投稿日時
            created_at = data.get('created_at', '')
            if created_at:
                # Gelbooruの日時フォーマット: "Fri Feb 28 10:30:45 +0000 2026" など
                try:
                    dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                    date_iso = dt.isoformat()
                except (ValueError, TypeError):
                    date_iso = str(created_at)
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

            # タグ（フラット文字列 → リスト）
            tags_str = data.get('tags', '')
            if isinstance(tags_str, str):
                tags = [t for t in tags_str.split() if t]
            elif isinstance(tags_str, list):
                tags = tags_str
            else:
                tags = []

            # カテゴリ別タグ（tags=true有効時のみ利用可能）
            tags_artist = self._parse_space_separated_tags(data.get('tags_artist', ''))
            tags_character = self._parse_space_separated_tags(data.get('tags_character', ''))
            tags_copyright = self._parse_space_separated_tags(data.get('tags_copyright', ''))
            tags_general = self._parse_space_separated_tags(data.get('tags_general', ''))
            tags_metadata = self._parse_space_separated_tags(data.get('tags_metadata', ''))

            # Rating → Sensitive判定
            rating = str(data.get('rating', 'general')).lower()
            sensitive = rating in ('questionable', 'explicit')

            # ソースURL（元投稿: Pixiv等へのリンク）
            source_url = data.get('source', '')

            # スコア
            score = 0
            try:
                score = int(data.get('score', 0))
            except (TypeError, ValueError):
                pass

            return {
                'id': post_id,
                'display_name': '',  # 検索型なので空
                'text': '',  # Gelbooruには本文がない
                'date': date_iso,
                'url': f"https://gelbooru.com/index.php?page=post&s=view&id={post_id}",
                'media': [],  # Type 3エントリで追加される
                'tags': tags,
                'tags_artist': tags_artist,
                'tags_character': tags_character,
                'tags_copyright': tags_copyright,
                'tags_general': tags_general,
                'tags_metadata': tags_metadata,
                'source_url': source_url,
                'score': score,
                'rating': rating,
                'sensitive': sensitive,
                'source': 'gelbooru',
                'platform': 'gelbooru',
            }

        except Exception as e:
            self.logger.error(f"Error extracting gelbooru post info: {e}")
            return None

    def _parse_space_separated_tags(self, value) -> List[str]:
        """スペース区切りのタグ文字列をリストに変換"""
        if isinstance(value, str) and value.strip():
            return [t for t in value.split() if t]
        elif isinstance(value, list):
            return [str(t) for t in value if t]
        return []

    def _collect_downloaded_files(
        self,
        work_ids: List[str],
        output_dir: Path,
        existing_files: set,
    ) -> Dict[str, List[Path]]:
        """ダウンロード済みファイルを投稿IDごとに収集"""
        new_files_by_work: Dict[str, List[Path]] = {}
        if not output_dir.exists():
            return new_files_by_work

        for f in output_dir.rglob('*'):
            if not f.is_file() or f in existing_files:
                continue

            filename = f.name
            # ファイル名パターン: {id}_{num}.ext
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
                f"Moved {len(mapping)} gelbooru media files for {dir_name}"
            )

        except Exception as e:
            self.logger.error(f"Failed to move gelbooru files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
