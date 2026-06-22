#!/usr/bin/env python3
"""
bilibili動態(opus)取得・メディアダウンロード

取得は2段構え:
1. web-dynamic feed API (opus/feed/space) で opus_id 一覧を高速取得（Cookie不要）
   - 新着ID判定（check_new_post_ids）はこのAPIだけで完結する
2. gallery-dl で各 opus 詳細（投稿日時・全画像・作者名）を取得・DL

bilibiliにはNSFWフラグが存在しない（プラットフォーム側で規制済み）ため、
sensitive は常に False とする。
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

import requests

from .path_utils import get_media_base_paths
from .subprocess_utils import run_with_idle_timeout


class BilibiliExtractor:
    """feed API + gallery-dl で bilibili 動態(opus)を取得"""

    FEED_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.BilibiliExtractor")
        self.bilibili_config = config.get('bilibili', {})

        # メディア保存先（一時）
        self.media_dir = Path(config.get('media', {}).get('save_dir', 'data/media')) / 'bilibili'

        # gallery-dl ラッパースクリプト
        self.wrapper_path = Path(__file__).parent / 'gallery_dl_wrapper.py'

        # Cookie（任意。リスクコントロール緩和用。存在すれば gallery-dl / feed API に渡す）
        self.cookie_file = Path('cookies/bilibili.com_cookies.txt')

        # バッチサイズ
        batch_cfg = self.bilibili_config.get('max_batch_size', 50)
        try:
            self.batch_size = max(1, int(batch_cfg))
        except (TypeError, ValueError):
            self.batch_size = 50

        # check_new_post_ids の最大ページ数（1ページ20件）
        pages_cfg = self.bilibili_config.get('feed_max_pages', 20)
        try:
            self.feed_max_pages = max(1, int(pages_cfg))
        except (TypeError, ValueError):
            self.feed_max_pages = 20

        # アカウント到達性キャッシュ（サイクルごとにクリア）
        self._account_reachable: Dict[str, bool] = {}

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        })
        self._cookie_header = self._load_cookie_header()
        if self._cookie_header:
            self._session.headers.update({"Cookie": self._cookie_header})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_display_name(self, user_id: str) -> Optional[str]:
        """表示名を取得（最新opusの詳細から作者名を抽出）"""
        opus_ids = self._list_opus_ids(user_id, limit=1)
        if not opus_ids:
            self.logger.warning(
                f"No opus found for bilibili user {user_id}, cannot resolve display name"
            )
            return None

        works = self.fetch_works_metadata_by_ids(user_id, opus_ids[:1])
        if works and works[0].get('display_name'):
            display_name = works[0]['display_name']
            self.logger.info(f"Resolved bilibili user {user_id} -> {display_name}")
            return display_name

        self.logger.warning(
            f"Display name not found for bilibili user {user_id}"
        )
        return None

    def fetch_user_id_by_opus(self, opus_id: str) -> Optional[str]:
        """opus_id から投稿者の数値ユーザーID(mid)を解決（Discord ingest用）"""
        works = self._detail_fetch_works([str(opus_id)])
        if works:
            mid = works[0].get('username')
            if mid:
                return str(mid)
        return None

    def check_account_reachable(self, user_id: str) -> bool:
        """feed API でアカウントの到達性を確認"""
        try:
            data = self._call_feed(user_id, offset=None)
            if data is None:
                # 通信失敗は一時的エラーとして到達可能扱い
                return True

            code = data.get("code")
            if code == 0:
                self._account_reachable[user_id] = True
                return True

            # アカウント非公開/存在しない/凍結等の明確なエラー
            # -352: 風控, -404/-509等。404相当は到達不可とみなす
            if code in (-404, 4_100_000) or "不存在" in str(data.get("message", "")):
                self.logger.info(
                    f"bilibili user {user_id} appears unreachable: {data.get('message')}"
                )
                self._account_reachable[user_id] = False
                return False

            # それ以外（風控等）は一時的扱い
            return True
        except Exception as e:
            self.logger.error(f"Error checking bilibili reachability for {user_id}: {e}")
            return True

    def clear_reachability_cache(self) -> None:
        """サイクル間のキャッシュをクリア"""
        self._account_reachable.clear()

    def check_new_post_ids(
        self, user_id: str, existing_ids: set
    ) -> Optional[List[str]]:
        """feed APIだけで新着 opus_id を高速特定する（FANBOX同様の浅いチェック）。

        Returns:
            新着 opus_id のリスト（新→古順）。
            既知IDに到達できず max_pages を使い切った場合や通信失敗時は None
            （呼び出し側が従来の --range ベース取得へフォールバックする）。
        """
        new_ids: List[str] = []
        offset: Optional[str] = None

        for page in range(self.feed_max_pages):
            data = self._call_feed(user_id, offset=offset)
            if data is None or data.get("code") != 0:
                return None

            payload = data.get("data", {}) or {}
            items = payload.get("items", []) or []
            if not items:
                # 投稿が無い/全て取得済み
                self._account_reachable[user_id] = True
                return new_ids

            self._account_reachable[user_id] = True
            hit_known = False
            for item in items:
                opus_id = str(item.get("opus_id") or "")
                if not opus_id:
                    continue
                if opus_id in existing_ids:
                    hit_known = True
                    break
                new_ids.append(opus_id)
                offset = opus_id

            if hit_known:
                return new_ids

            if not payload.get("has_more"):
                # 最後まで見たが既知IDに当たらなかった → 全件新規
                return new_ids

        # max_pages を使い切っても既知IDに当たらなかった → フォールバック
        self.logger.info(
            f"bilibili:{user_id} shallow check exceeded {self.feed_max_pages} pages"
        )
        return None

    def fetch_works_metadata_by_ids(
        self, user_id: str, opus_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """指定 opus_id 群の詳細メタデータを gallery-dl で取得"""
        if not opus_ids:
            return []
        works = self._detail_fetch_works(opus_ids)
        # 念のため identity を CSV mid に揃える
        for work in works:
            work['username'] = str(user_id)
        return works

    def fetch_user_works(
        self, user_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """指定ユーザーの opus を取得（feed で一覧 → gallery-dl で詳細）"""
        opus_ids = self._list_opus_ids(user_id, limit=limit)
        if not opus_ids:
            return []

        self.logger.info(
            f"Fetching bilibili details for {len(opus_ids)} opus of user {user_id}"
        )
        return self.fetch_works_metadata_by_ids(user_id, opus_ids)

    def download_media_for_works(
        self,
        user_id: str,
        work_ids: List[str],
        move_to_images: bool = True,
    ) -> Dict[str, List[str]]:
        """特定 opus のメディアを gallery-dl でダウンロード"""
        if not work_ids:
            return {}

        self.media_dir.mkdir(parents=True, exist_ok=True)

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
                f"{len(remaining)} remaining bilibili opus"
            )

            url_file_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False, encoding='utf-8'
                ) as url_file:
                    for opus_id in current_batch:
                        url_file.write(
                            f"https://www.bilibili.com/opus/{opus_id}\n"
                        )
                    url_file_path = url_file.name

                cmd = [
                    sys.executable,
                    str(self.wrapper_path),
                ]
                if self.cookie_file.exists():
                    cmd.extend(['--cookies', str(self.cookie_file)])
                cmd.extend([
                    '-d', str(self.media_dir),
                    '-o', 'filename={id}_{num}.{extension}',
                    '-v',
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
                    total_files = sum(len(fs) for fs in new_files_by_work.values())
                    self.logger.info(
                        f"Downloaded {total_files} files for "
                        f"{len(new_files_by_work)} opus"
                    )
                    all_media_paths.update(new_files_by_work)
                    for fs in new_files_by_work.values():
                        all_downloaded.extend(fs)
                    remaining = [
                        wid for wid in remaining if wid not in new_files_by_work
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
                    remaining = [wid for wid in remaining if wid not in partial]
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
                f"Failed to download media for {len(remaining)} bilibili opus"
            )

        dir_name = f"bilibili_{user_id}"
        final_paths: Dict[str, List[str]] = {}
        if move_to_images and all_downloaded:
            moved = self._move_to_images_dir_with_mapping(all_downloaded, dir_name)
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

    def _load_cookie_header(self) -> str:
        """Netscape cookie ファイルから bilibili.com の Cookie ヘッダを構築（任意）"""
        if not self.cookie_file.exists():
            return ""
        pairs = []
        try:
            with self.cookie_file.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        pairs.append(f"{parts[5]}={parts[6]}")
        except OSError as exc:
            self.logger.warning(f"Failed to read bilibili cookie file: {exc}")
            return ""
        return "; ".join(pairs)

    def _call_feed(self, host_mid: str, offset: Optional[str]) -> Optional[dict]:
        """feed/space API を1ページ呼び出す。失敗時は None"""
        params = {"host_mid": str(host_mid)}
        if offset:
            params["offset"] = str(offset)
        try:
            resp = self._session.get(self.FEED_API, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.logger.warning(
                f"bilibili feed API failed for {host_mid} (offset={offset}): {exc}"
            )
            return None

    def _list_opus_ids(self, user_id: str, limit: Optional[int]) -> List[str]:
        """feed/space API を辿って opus_id 一覧（新→古）を返す"""
        opus_ids: List[str] = []
        seen = set()
        offset: Optional[str] = None

        for _page in range(self.feed_max_pages):
            data = self._call_feed(user_id, offset=offset)
            if data is None or data.get("code") != 0:
                break

            payload = data.get("data", {}) or {}
            items = payload.get("items", []) or []
            if not items:
                break

            self._account_reachable[user_id] = True
            for item in items:
                opus_id = str(item.get("opus_id") or "")
                if not opus_id or opus_id in seen:
                    continue
                seen.add(opus_id)
                opus_ids.append(opus_id)
                offset = opus_id
                if limit and len(opus_ids) >= limit:
                    return opus_ids

            if not payload.get("has_more"):
                break

        return opus_ids

    def _detail_fetch_works(self, opus_ids: List[str]) -> List[Dict[str, Any]]:
        """gallery-dl -j で opus 群の詳細を取得しパース"""
        if not opus_ids:
            return []

        url_file_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', delete=False, encoding='utf-8'
            ) as url_file:
                for opus_id in opus_ids:
                    url_file.write(f"https://www.bilibili.com/opus/{opus_id}\n")
                url_file_path = url_file.name

            cmd = [sys.executable, str(self.wrapper_path)]
            if self.cookie_file.exists():
                cmd.extend(['--cookies', str(self.cookie_file)])
            cmd.extend(['-q', '-j', '--input-file', url_file_path])

            result = run_with_idle_timeout(cmd, idle_timeout=180, rate_limit_retries=0)

            if result.returncode != 0:
                self.logger.warning(
                    f"gallery-dl -j returncode={result.returncode} "
                    f"stderr={result.stderr[:300]}"
                )

            output = (result.stdout or "").strip()
            if not output:
                return []
            return self._parse_gallery_dl_output(output)

        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout fetching bilibili opus details")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching bilibili opus details: {e}")
            return []
        finally:
            if url_file_path:
                try:
                    os.unlink(url_file_path)
                except Exception:
                    pass

    def _parse_gallery_dl_output(self, output: str) -> List[Dict[str, Any]]:
        """gallery-dl の JSON 出力をパースして作品情報リストを返す"""
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
                        works.setdefault(wid, work_info)

                elif item_type == 3 and len(item) >= 3 and isinstance(item[2], dict):
                    # Type 3: ファイルダウンロード [3, media_url, metadata]
                    media_url = item[1]
                    media_meta = item[2]
                    work_info = self._extract_work_info(media_meta)
                    if not work_info:
                        continue
                    wid = work_info['id']
                    works.setdefault(wid, work_info)
                    if media_url and media_url not in works[wid]['media']:
                        works[wid]['media'].append(media_url)

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse gallery-dl bilibili output: {e}")
            return []

        result = list(works.values())
        self.logger.info(f"Parsed {len(result)} bilibili opus")
        return result

    def _extract_work_info(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """gallery-dl のメタデータから作品情報を抽出"""
        try:
            detail = data.get('detail', {}) or {}
            opus_id = str(detail.get('id_str') or data.get('id') or '')
            if not opus_id:
                return None

            modules = detail.get('modules', {}) or {}
            author = modules.get('module_author', {}) or {}

            # 投稿者の数値ユーザーID（mid）。CSV username / bilibili_user タグに使用
            mid = str(author.get('mid') or detail.get('basic', {}).get('uid') or '')
            display_name = author.get('name') or data.get('username') or ''

            # 投稿日時: pub_ts(UNIX秒) を ISO へ。取れない場合は空文字
            date_iso = ''
            pub_ts = author.get('pub_ts')
            if pub_ts:
                try:
                    date_iso = datetime.fromtimestamp(int(pub_ts)).isoformat()
                except (TypeError, ValueError, OSError):
                    date_iso = ''

            # 本文テキスト: module_content の段落から抽出。
            # basic.title は "{name}的动态 - 哔哩哔哩" 形式の汎用ページタイトルなので
            # 本文が取れない場合のみフォールバックに使う。
            title = self._extract_content_text(modules) or detail.get('basic', {}).get('title') or ''

            return {
                'id': opus_id,
                'username': mid,
                'display_name': display_name,
                'title': title,
                'text': title,
                'date': date_iso,
                'url': f"https://www.bilibili.com/opus/{opus_id}",
                'media': [],  # Type 3 で追加される
                'tags': [],
                'sensitive': False,  # bilibili に NSFW フラグは存在しない
                'source': 'bilibili',
                'platform': 'bilibili',
            }
        except Exception as e:
            self.logger.error(f"Error extracting bilibili work info: {e}")
            return None

    @staticmethod
    def _extract_content_text(modules: Dict[str, Any]) -> str:
        """module_content の段落テキストを連結して本文を組み立てる"""
        content = modules.get('module_content') or {}
        parts: List[str] = []
        for paragraph in content.get('paragraphs', []) or []:
            text_block = paragraph.get('text')
            if not text_block:
                continue
            for node in text_block.get('nodes', []) or []:
                rich = node.get('rich') or {}
                word = node.get('word') or {}
                chunk = rich.get('text') or word.get('words') or ''
                if chunk:
                    parts.append(chunk)
        return ''.join(parts).strip()

    def _collect_downloaded_files(
        self,
        work_ids: List[str],
        output_dir: Path,
        existing_files: set,
    ) -> Dict[str, List[Path]]:
        """ダウンロード済みファイルを opus_id ごとに収集"""
        new_files_by_work: Dict[str, List[Path]] = {}
        if not output_dir.exists():
            return new_files_by_work

        for f in output_dir.rglob('*'):
            if not f.is_file() or f in existing_files:
                continue
            filename = f.name
            # ファイル名パターン: {opus_id}_{num}.ext
            for opus_id in work_ids:
                if filename.startswith(f"{opus_id}_") or filename.startswith(f"{opus_id}."):
                    new_files_by_work.setdefault(opus_id, []).append(f)
                    break

        for wid in new_files_by_work:
            new_files_by_work[wid].sort(key=lambda f: f.name)

        return new_files_by_work

    def _move_to_images_dir_with_mapping(
        self, files: List[Path], dir_name: str
    ) -> Dict[Path, Path]:
        """ダウンロードしたファイルを images/videos ディレクトリに移動"""
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
                dest_file = (videos_dir if is_video else images_dir) / filename
                if not dest_file.exists():
                    shutil.copy2(src_file, dest_file)
                mapping[src_file] = dest_file

            self.logger.info(
                f"Moved {len(mapping)} bilibili media files for {dir_name}"
            )
        except Exception as e:
            self.logger.error(f"Failed to move bilibili files: {e}")

        return mapping

    def _cleanup_media_dir(self):
        """一時メディアディレクトリを削除"""
        try:
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir)
        except Exception as e:
            self.logger.error(f"Failed to cleanup media dir: {e}")
