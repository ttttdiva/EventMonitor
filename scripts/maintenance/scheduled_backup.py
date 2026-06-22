#!/usr/bin/env python3
"""
定期バックアップスクリプト — 3種類のデータをHuggingFaceにアップロード

バックアップ対象:
  1. クローラー保存フォルダ (F:/48_EventMonitor_log/images/ + videos/)
  2. EventMonitor DB (data/eventmonitor.db)
  3. Hydrus DB (client.db, client.mappings.db, client.master.db)

使用方法:
    # 全てバックアップ
    python scripts/maintenance/scheduled_backup.py

    # 特定のターゲットのみ
    python scripts/maintenance/scheduled_backup.py --target crawler_media
    python scripts/maintenance/scheduled_backup.py --target eventmonitor_db
    python scripts/maintenance/scheduled_backup.py --target hydrus_db

    # ドライラン（アップロードせず対象ファイルを確認）
    python scripts/maintenance/scheduled_backup.py --dry-run

    # アップロード後にクローラーフォルダを削除
    python scripts/maintenance/scheduled_backup.py --delete-after

    # 進捗リセット（全ターゲットを再アップロード対象にする）
    python scripts/maintenance/scheduled_backup.py --reset-progress

注意:
    - Hydrus DBバックアップ時はHydrus Clientを停止してください（WALロック回避）
    - タスクスケジューラ等で日次/週次実行を推奨
    - 進捗は data/backup_progress.json に記録される
    - リポジトリが10万ファイル上限に達すると自動で _1→_2 に切り替わる
"""

import sys
import os

# xet (HuggingFace の新ファイル転送エンジン) を無効化
# Windows でファイルロック競合 (os error 183) を起こすため
os.environ["HF_HUB_DISABLE_XET"] = "1"

import re
import logging
import argparse
import shutil
import json
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import tempfile
import yaml
from dotenv import load_dotenv
from huggingface_hub import HfApi, create_repo, upload_file

load_dotenv()

# logsディレクトリ作成（logging.basicConfig より先に必要）
(Path(__file__).resolve().parent.parent / 'logs').mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).resolve().parent.parent / 'logs' / f'scheduled_backup_{datetime.now():%Y%m%d}.log',
            encoding='utf-8',
        ),
    ],
)
logger = logging.getLogger("ScheduledBackup")


class ScheduledBackup:
    """定期バックアップ: 3種類のデータをHuggingFaceにアップロード"""

    # バックアップ対象の定義
    TARGETS = ['eventmonitor_db', 'hydrus_db', 'crawler_media']
    # TODO: 'discord' を復活させる場合はここに追加

    PROGRESS_FILE = Path('data/backup_progress.json')
    MANIFEST_FILE = Path('data/backup_crawler_manifest.txt')
    MAX_RETRIES = 3
    DIR_FILE_LIMIT = 9000   # HF上限 10,000/ディレクトリ、マージン込み
    STAGING_DIR_NAME = '.hf_upload_staging'
    HEARTBEAT_INTERVAL_SECONDS = 30
    MAX_RECENT_RUNS = 20
    INDIVIDUAL_UPLOAD_THRESHOLD = 500  # この件数以下なら個別アップロード

    def __init__(self, config_path: str = "config.yaml", dry_run: bool = False):
        self.config_path = config_path
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.dry_run = dry_run
        self.api_key = os.getenv('HUGGINGFACE_API_KEY')
        if not self.api_key:
            logger.error("HUGGINGFACE_API_KEY が環境変数に設定されていません")
            sys.exit(1)

        self.api = HfApi(token=self.api_key)

        hf_config = self.config.get('huggingface_backup', {})
        self.repo_name = hf_config.get('repo_name', 'disguisequence/EventMonitor_1')

        # パス設定
        media_storage = self.config.get('media_storage', {})
        self.images_path = Path(media_storage.get('images_path', 'F:/48_EventMonitor_log/images'))
        self.videos_path = Path(media_storage.get('videos_path', 'F:/48_EventMonitor_log/videos'))
        self.db_path = Path(self.config.get('database', {}).get('path', 'data/eventmonitor.db'))

        # Hydrus DBパス（C:ドライブのdb/配下）
        self.hydrus_db_dir = Path("C:/Hydrus Network/db")
        self.hydrus_db_files = [
            'client.db',
            'client.mappings.db',
            'client.master.db',
        ]

        # 進捗読み込み
        self.progress = self._load_progress()
        self.run_id = f"{os.getpid()}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        self.run_started_at = datetime.now().isoformat()
        self.run_status = "initialized"
        self.active_targets = []
        self.target_states: dict[str, dict[str, Any]] = {}

        if not dry_run:
            self._ensure_repo_exists()

        # 統計
        self.stats = {target: {'files': 0, 'size': 0, 'errors': 0, 'skipped': 0} for target in self.TARGETS}

    # ------------------------------------------------------------------
    # 進捗管理
    # ------------------------------------------------------------------
    def _load_progress(self) -> dict:
        """進捗JSONを読み込み（DB系バックアップ用）"""
        if self.PROGRESS_FILE.exists():
            try:
                with open(self.PROGRESS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"進捗ファイル読み込みエラー（初期化）: {e}")
        return {}

    def _write_progress_file(self, progress_data: dict):
        """進捗JSONを安全に保存"""
        self.PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.PROGRESS_FILE.with_name(f"{self.PROGRESS_FILE.name}.{self.run_id}.tmp")
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(progress_data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.PROGRESS_FILE)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _build_run_state(self, status: Optional[str] = None) -> dict:
        return {
            'run_id': self.run_id,
            'pid': os.getpid(),
            'started_at': self.run_started_at,
            'updated_at': datetime.now().isoformat(),
            'status': status or self.run_status,
            'repo_name': self.repo_name,
            'dry_run': self.dry_run,
            'targets': list(self.active_targets),
            'target_states': self.target_states,
        }

    def _save_progress(self, remove_active_run: bool = False, final_status: Optional[str] = None):
        """自プロセスの進捗だけをマージして保存"""
        progress_data = self._load_progress()

        active_runs = progress_data.get('active_runs')
        if not isinstance(active_runs, dict):
            active_runs = {}

        recent_runs = progress_data.get('recent_runs')
        if not isinstance(recent_runs, list):
            recent_runs = []

        run_state = self._build_run_state(status=final_status)
        if remove_active_run:
            active_runs.pop(self.run_id, None)
            run_state['ended_at'] = datetime.now().isoformat()
            recent_runs.insert(0, run_state)
            recent_runs = recent_runs[:self.MAX_RECENT_RUNS]
        else:
            active_runs[self.run_id] = run_state

        for target, state in self.target_states.items():
            progress_data[target] = state

        progress_data['active_runs'] = active_runs
        progress_data['recent_runs'] = recent_runs
        progress_data['meta'] = {
            'version': 2,
            'updated_at': datetime.now().isoformat(),
        }

        self._write_progress_file(progress_data)
        self.progress = progress_data

    def _update_target_progress(
        self,
        target: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        save: bool = True,
        **extra: Any,
    ):
        now = datetime.now().isoformat()
        state = dict(self.target_states.get(target, {}))
        state.setdefault('target', target)
        state.setdefault('started_at', now)
        state['last_updated'] = now
        state['run_id'] = self.run_id
        state['pid'] = os.getpid()
        state['current_repo'] = self.repo_name
        state['dry_run'] = self.dry_run

        if status is not None:
            state['status'] = status
        else:
            state.setdefault('status', 'running')

        if phase is not None:
            state['phase'] = phase

        for key, value in extra.items():
            state[key] = value

        if state['status'] in {'completed', 'failed', 'dry_run'}:
            state['completed_at'] = now

        self.target_states[target] = state

        if save:
            self._save_progress()

    def _mark_db_uploaded(self, target: str, **extra: Any):
        """DB系ターゲットのアップロード完了を記録"""
        self._update_target_progress(
            target,
            status='completed',
            phase='completed',
            last_uploaded=datetime.now().isoformat(),
            **extra,
        )

    @contextmanager
    def _progress_heartbeat(self, target: str, phase: str, **extra: Any):
        """長時間処理中も進捗更新時刻を進め続ける"""
        stop_event = threading.Event()

        def heartbeat():
            while not stop_event.wait(self.HEARTBEAT_INTERVAL_SECONDS):
                self._update_target_progress(
                    target,
                    status='running',
                    phase=phase,
                    save=True,
                    **extra,
                )

        thread = threading.Thread(
            target=heartbeat,
            name=f"scheduled-backup-progress-{target}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)

    def reset_progress(self):
        """進捗をリセット"""
        if self.PROGRESS_FILE.exists():
            self.PROGRESS_FILE.unlink()
            logger.info("進捗ファイルを削除しました")
        self.progress = {}
        self.target_states = {}

    # ------------------------------------------------------------------
    # リポジトリ自動切り替え
    # ------------------------------------------------------------------
    def _get_next_repo_name(self) -> str:
        """現在のリポジトリ名から次の番号のリポジトリ名を生成"""
        match = re.match(r'^(.+?)(?:_(\d+))?$', self.repo_name)
        if match:
            base_name = match.group(1)
            current_num = int(match.group(2)) if match.group(2) else 1
            return f"{base_name}_{current_num + 1}"
        return f"{self.repo_name}_2"

    def _update_config_file(self, new_repo_name: str):
        """config.yamlの repo_name を更新"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            config['huggingface_backup']['repo_name'] = new_repo_name
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            logger.info(f"config.yaml 更新: repo_name = {new_repo_name}")
        except Exception as e:
            logger.error(f"config.yaml 更新失敗: {e}")

    def _handle_upload_error(self, error: Exception) -> bool:
        """アップロードエラーを処理し、リトライ可否を返す

        Returns:
            bool: リトライすべきか
        """
        error_msg = str(error)

        # レート制限エラー (429)
        if "429" in error_msg and "Too Many Requests" in error_msg:
            logger.warning(f"レート制限エラー: {error_msg}")

            wait_time = 3600  # デフォルト1時間
            for pattern in [
                r'retry this action in about (\d+) (hour|minute)',
                r'you can retry this action in (\d+) (minutes?|hours?)',
                r'(\d+)\s+(minutes?|hours?)',
            ]:
                match = re.search(pattern, error_msg)
                if match:
                    time_value = int(match.group(1))
                    time_unit = match.group(2)
                    wait_time = time_value * 3600 if time_unit.startswith("hour") else time_value * 60
                    break

            total_wait = wait_time + 1
            logger.info(f"レート制限: {total_wait}秒待機後リトライ...")
            time.sleep(total_wait)
            return True

        # タイムアウトエラー（ReadTimeout / ConnectTimeout）
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            logger.warning(f"タイムアウト: 10秒待機後リトライ...")
            time.sleep(10)
            return True

        # 接続エラー（一時的なネットワーク障害）
        if any(kw in error_msg.lower() for kw in ["connection", "refused", "reset", "broken pipe"]):
            logger.warning(f"接続エラー: 10秒待機後リトライ...")
            time.sleep(10)
            return True

        # ファイル数上限エラー (10万件)
        if "over the limit of 100000 files" in error_msg:
            new_repo_name = self._get_next_repo_name()
            logger.warning(f"リポジトリ {self.repo_name} がファイル上限に到達 → {new_repo_name} に切り替え")

            self._update_config_file(new_repo_name)
            self.repo_name = new_repo_name

            try:
                create_repo(
                    repo_id=self.repo_name,
                    token=self.api_key,
                    repo_type="dataset",
                    private=False,
                )
                logger.info(f"新リポジトリ作成: {self.repo_name}")
                time.sleep(2)
                return True
            except Exception as create_error:
                logger.error(f"新リポジトリ作成失敗: {create_error}")
                return False

        return False

    def _upload_file_with_retry(self, path_or_fileobj: str, path_in_repo: str) -> bool:
        """upload_file のリトライラッパー"""
        for attempt in range(self.MAX_RETRIES):
            try:
                upload_file(
                    path_or_fileobj=path_or_fileobj,
                    path_in_repo=path_in_repo,
                    repo_id=self.repo_name,
                    repo_type="dataset",
                    token=self.api_key,
                )
                return True
            except Exception as e:
                if attempt < self.MAX_RETRIES - 1 and self._handle_upload_error(e):
                    continue
                raise
        return False

    # ------------------------------------------------------------------
    # リポジトリ管理
    # ------------------------------------------------------------------
    def _ensure_repo_exists(self):
        """リポジトリの存在確認・作成"""
        try:
            self.api.repo_info(repo_id=self.repo_name, repo_type="dataset")
            logger.info(f"リポジトリ確認済み: {self.repo_name}")
        except Exception:
            try:
                create_repo(
                    repo_id=self.repo_name,
                    token=self.api_key,
                    repo_type="dataset",
                    private=False,
                )
                logger.info(f"リポジトリ作成: {self.repo_name}")
            except Exception as e:
                logger.error(f"リポジトリ作成失敗: {e}")
                raise

    # ------------------------------------------------------------------
    # ステージング（HF 10,000ファイル/ディレクトリ上限対策）
    # ------------------------------------------------------------------
    def _find_large_dirs(self) -> tuple[list[str], dict[str, list[Path]]]:
        """10,000ファイル超のディレクトリを検出

        Returns:
            ignore_patterns: upload_large_folder で除外するパターン
            large_dirs: {リモートプレフィックス: [ファイルパス一覧]}
        """
        ignore_patterns = []
        large_dirs = {}

        for media_type, base_path in [('images', self.images_path), ('videos', self.videos_path)]:
            if not base_path.exists():
                continue
            for user_dir in sorted(base_path.iterdir()):
                if not user_dir.is_dir():
                    continue
                files = sorted(f for f in user_dir.iterdir() if f.is_file())
                if len(files) > self.DIR_FILE_LIMIT:
                    rel = f"{media_type}/{user_dir.name}"
                    ignore_patterns.append(f"{rel}/**")
                    large_dirs[rel] = files
                    logger.info(f"  {rel}: {len(files)} ファイル（上限超過、個別アップロード）")

        return ignore_patterns, large_dirs

    def _prepare_staging_for_large_dirs(self, large_dirs: dict[str, list[Path]]) -> Path:
        """上限超過ディレクトリのみステージング作成（サブフォルダ分割、ハードリンク）"""
        media_base = self.images_path.parent
        staging = media_base / self.STAGING_DIR_NAME

        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()

        for rel_prefix, files in large_dirs.items():
            parts = rel_prefix.split('/')
            target_base = staging / parts[0] / parts[1]
            target_base.mkdir(parents=True)

            for idx, f in enumerate(files):
                chunk_dir = target_base / f"{idx // self.DIR_FILE_LIMIT:02d}"
                chunk_dir.mkdir(exist_ok=True)
                os.link(str(f), str(chunk_dir / f.name))

            n_chunks = (len(files) - 1) // self.DIR_FILE_LIMIT + 1
            logger.info(f"  {rel_prefix}: {n_chunks} チャンクに分割")

        return staging

    def _cleanup_staging(self, staging: Path):
        """ステージングディレクトリを削除（ハードリンクなので元ファイルに影響なし）"""
        try:
            if staging.exists():
                shutil.rmtree(staging)
                logger.info("  ステージングディレクトリを削除")
        except Exception as e:
            logger.warning(f"  ステージング削除失敗（手動削除推奨: {staging}）: {e}")

    # ------------------------------------------------------------------
    # マニフェスト管理（クローラーメディア差分検出用）
    # ------------------------------------------------------------------
    def _load_manifest(self) -> set[str]:
        """アップロード済みファイルのマニフェストを読み込む"""
        if self.MANIFEST_FILE.exists():
            try:
                with open(self.MANIFEST_FILE, 'r', encoding='utf-8') as f:
                    return set(line.strip() for line in f if line.strip())
            except OSError as e:
                logger.warning(f"マニフェスト読み込みエラー: {e}")
        return set()

    def _save_manifest(self, manifest: set[str]):
        """マニフェストを保存"""
        self.MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.MANIFEST_FILE.with_suffix('.tmp')
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                for rel_path in sorted(manifest):
                    f.write(rel_path + '\n')
            os.replace(temp_path, self.MANIFEST_FILE)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _scan_local_media(self) -> dict[str, Path]:
        """ローカルメディアファイルをスキャンし、相対パス→絶対パスの辞書を返す"""
        local_files: dict[str, Path] = {}
        media_base = self.images_path.parent
        for media_type, base_path in [('images', self.images_path), ('videos', self.videos_path)]:
            if not base_path.exists():
                continue
            for user_dir in base_path.iterdir():
                if user_dir.is_dir():
                    for f in user_dir.iterdir():
                        if f.is_file():
                            rel = f.relative_to(media_base).as_posix()
                            local_files[rel] = f
        return local_files

    def _upload_individual_files(self, files: dict[str, Path]) -> tuple[int, int, int]:
        """少数のファイルを個別にアップロード

        Returns:
            (成功件数, 失敗件数, 合計サイズ)
        """
        success = 0
        failed = 0
        total_size = 0
        for i, (rel_path, abs_path) in enumerate(files.items(), 1):
            file_size = abs_path.stat().st_size
            try:
                logger.info(f"  [{i}/{len(files)}] {rel_path} ({file_size / 1024:.1f} KB)")
                self._upload_file_with_retry(
                    path_or_fileobj=str(abs_path),
                    path_in_repo=rel_path,
                )
                success += 1
                total_size += file_size
            except Exception as e:
                failed += 1
                logger.error(f"  {rel_path}: アップロード失敗 - {e}")
        return success, failed, total_size

    # ------------------------------------------------------------------
    # 1. クローラー保存フォルダ
    # ------------------------------------------------------------------
    def backup_crawler_media(self, delete_after: bool = False):
        """クローラー保存フォルダをHFにアップロード（差分検出対応）

        差分に応じて3パターンで動作:
          - 差分 0件: スキップ
          - 差分 ≤ INDIVIDUAL_UPLOAD_THRESHOLD: 個別 upload_file
          - 差分 > INDIVIDUAL_UPLOAD_THRESHOLD: upload_large_folder（一括）
        """
        logger.info("=== クローラー保存フォルダのバックアップ開始 ===")
        self._update_target_progress(
            'crawler_media',
            status='running',
            phase='initializing',
            delete_after=delete_after,
        )

        # images/ と videos/ の親ディレクトリ（F:/48_EventMonitor_log/）
        media_base = self.images_path.parent
        if not media_base.exists():
            logger.error(f"メディアベースフォルダが見つかりません: {media_base}")
            self._update_target_progress(
                'crawler_media',
                status='failed',
                phase='initializing',
                error=f"media base not found: {media_base}",
            )
            return

        # ローカルファイルをスキャン
        logger.info("  ローカルファイルをスキャン中...")
        local_files = self._scan_local_media()
        total_files = len(local_files)
        total_size = sum(f.stat().st_size for f in local_files.values())
        logger.info(f"  ローカル合計: {total_files} ファイル ({total_size / 1024 / 1024:.1f} MB)")

        # マニフェストと比較して差分を検出
        manifest = self._load_manifest()
        new_files = {rel: abs_path for rel, abs_path in local_files.items() if rel not in manifest}
        new_count = len(new_files)
        new_size = sum(f.stat().st_size for f in new_files.values()) if new_files else 0

        logger.info(
            f"  差分: {new_count} 件の新規ファイル ({new_size / 1024 / 1024:.1f} MB), "
            f"マニフェスト既知: {len(manifest)} 件"
        )
        self._update_target_progress(
            'crawler_media',
            status='running',
            phase='diff_detected',
            total_files=total_files,
            total_size_bytes=total_size,
            new_files=new_count,
            new_size_bytes=new_size,
            manifest_known=len(manifest),
        )

        # 差分なし → スキップ
        if new_count == 0:
            logger.info("  差分なし — スキップ")
            self.stats['crawler_media']['files'] = 0
            self.stats['crawler_media']['skipped'] = total_files
            self._update_target_progress(
                'crawler_media',
                status='completed',
                phase='skipped_no_diff',
                total_files=total_files,
                new_files=0,
            )
            return

        if self.dry_run:
            self.stats['crawler_media']['files'] = new_count
            self.stats['crawler_media']['size'] = new_size
            logger.info(f"  ドライラン: {new_count} 件をアップロード予定")
            self._update_target_progress(
                'crawler_media',
                status='dry_run',
                phase='completed',
                total_files=total_files,
                new_files=new_count,
                new_size_bytes=new_size,
            )
            return

        try:
            if new_count <= self.INDIVIDUAL_UPLOAD_THRESHOLD:
                # ---- 少量: 個別アップロード ----
                logger.info(f"  {new_count} 件 ≤ {self.INDIVIDUAL_UPLOAD_THRESHOLD} — 個別アップロード")
                self._update_target_progress(
                    'crawler_media',
                    status='running',
                    phase='individual_upload',
                    new_files=new_count,
                    new_size_bytes=new_size,
                )
                with self._progress_heartbeat(
                    'crawler_media',
                    'individual_upload',
                    new_files=new_count,
                    new_size_bytes=new_size,
                ):
                    success, failed, uploaded_size = self._upload_individual_files(new_files)

                self.stats['crawler_media']['files'] = success
                self.stats['crawler_media']['size'] = uploaded_size
                self.stats['crawler_media']['errors'] = failed

                # 成功したファイルをマニフェストに追加
                uploaded_rels = set()
                for rel, abs_path in new_files.items():
                    uploaded_rels.add(rel)
                # 失敗分は除外しない（リトライで再取得されるため問題ない）
                manifest.update(uploaded_rels)
                self._save_manifest(manifest)

            else:
                # ---- 大量: upload_large_folder（従来方式） ----
                logger.info(
                    f"  {new_count} 件 > {self.INDIVIDUAL_UPLOAD_THRESHOLD} "
                    f"— upload_large_folder で一括アップロード"
                )

                # 上限超過ディレクトリを検出
                ignore_patterns, large_dirs = self._find_large_dirs()

                # Phase 1: 通常ディレクトリ
                logger.info("  Phase 1: 通常ディレクトリのアップロード...")
                self._update_target_progress(
                    'crawler_media',
                    status='running',
                    phase='phase_1_upload',
                    new_files=new_count,
                    new_size_bytes=new_size,
                    ignored_pattern_count=len(ignore_patterns),
                    large_directory_count=len(large_dirs),
                )
                with self._progress_heartbeat(
                    'crawler_media',
                    'phase_1_upload',
                    new_files=new_count,
                    new_size_bytes=new_size,
                    ignored_pattern_count=len(ignore_patterns),
                    large_directory_count=len(large_dirs),
                ):
                    self.api.upload_large_folder(
                        repo_id=self.repo_name,
                        folder_path=str(media_base),
                        repo_type="dataset",
                        allow_patterns=["images/**", "videos/**"],
                        ignore_patterns=ignore_patterns or None,
                        num_workers=4,
                    )

                # Phase 2: 上限超過ディレクトリ（ステージング経由）
                if large_dirs:
                    logger.info("  Phase 2: 上限超過ディレクトリのアップロード...")
                    self._update_target_progress(
                        'crawler_media',
                        status='running',
                        phase='phase_2_prepare',
                        large_directory_count=len(large_dirs),
                    )
                    staging = self._prepare_staging_for_large_dirs(large_dirs)
                    try:
                        self._update_target_progress(
                            'crawler_media',
                            status='running',
                            phase='phase_2_upload',
                            staging_path=str(staging),
                            large_directory_count=len(large_dirs),
                        )
                        with self._progress_heartbeat(
                            'crawler_media',
                            'phase_2_upload',
                            staging_path=str(staging),
                            large_directory_count=len(large_dirs),
                        ):
                            self.api.upload_large_folder(
                                repo_id=self.repo_name,
                                folder_path=str(staging),
                                repo_type="dataset",
                                allow_patterns=["images/**", "videos/**"],
                                num_workers=4,
                            )
                    finally:
                        self._cleanup_staging(staging)

                self.stats['crawler_media']['files'] = new_count
                self.stats['crawler_media']['size'] = new_size

                # 一括アップロード成功後、全ローカルファイルをマニフェストに登録
                manifest.update(local_files.keys())
                self._save_manifest(manifest)

            logger.info("クローラー保存フォルダ: アップロード完了")

            if delete_after:
                for media_type, base_path in [('images', self.images_path), ('videos', self.videos_path)]:
                    if base_path.exists():
                        for user_dir in base_path.iterdir():
                            if user_dir.is_dir():
                                shutil.rmtree(user_dir)
                        logger.info(f"  {media_type}: ローカルフォルダ削除完了")

            self._update_target_progress(
                'crawler_media',
                status='completed',
                phase='completed',
                total_files=total_files,
                total_size_bytes=total_size,
                new_files=new_count,
                new_size_bytes=new_size,
                deleted_after_upload=delete_after,
            )

        except Exception as e:
            self.stats['crawler_media']['errors'] += 1
            logger.error(f"クローラー保存フォルダ: アップロード失敗 - {e}")
            self._update_target_progress(
                'crawler_media',
                status='failed',
                phase=self.target_states.get('crawler_media', {}).get('phase', 'upload'),
                total_files=total_files,
                total_size_bytes=total_size,
                new_files=new_count,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # 2. EventMonitor DB
    # ------------------------------------------------------------------
    def backup_eventmonitor_db(self):
        """EventMonitorのSQLite DBをHFにアップロード"""
        logger.info("=== EventMonitor DB のバックアップ開始 ===")
        self._update_target_progress(
            'eventmonitor_db',
            status='running',
            phase='initializing',
            db_path=str(self.db_path),
        )

        if not self.db_path.exists():
            logger.error(f"DB ファイルが見つかりません: {self.db_path}")
            self._update_target_progress(
                'eventmonitor_db',
                status='failed',
                phase='initializing',
                db_path=str(self.db_path),
                error=f"database not found: {self.db_path}",
            )
            return

        file_size = self.db_path.stat().st_size
        hf_path = "backup/eventmonitor_db/eventmonitor_latest.db"

        logger.info(f"  {self.db_path} ({file_size / 1024 / 1024:.1f} MB) -> {hf_path}")

        if self.dry_run:
            self.stats['eventmonitor_db']['files'] += 1
            self.stats['eventmonitor_db']['size'] += file_size
            self._update_target_progress(
                'eventmonitor_db',
                status='dry_run',
                phase='completed',
                db_path=str(self.db_path),
                file_size_bytes=file_size,
                repo_path=hf_path,
            )
            return

        try:
            self._update_target_progress(
                'eventmonitor_db',
                status='running',
                phase='upload',
                db_path=str(self.db_path),
                file_size_bytes=file_size,
                repo_path=hf_path,
            )
            # 最新版も常に同じパスに置く（復元時にわかりやすい）
            self._upload_file_with_retry(
                path_or_fileobj=str(self.db_path),
                path_in_repo="backup/eventmonitor_db/eventmonitor_latest.db",
            )
            # タイムスタンプ付きスナップショットも保存
            self._update_target_progress(
                'eventmonitor_db',
                status='running',
                phase='upload',
                db_path=str(self.db_path),
                file_size_bytes=file_size,
                repo_path=hf_path,
            )
            self.stats['eventmonitor_db']['files'] += 1
            self.stats['eventmonitor_db']['size'] += file_size
            self._mark_db_uploaded(
                'eventmonitor_db',
                db_path=str(self.db_path),
                file_size_bytes=file_size,
                repo_path=hf_path,
            )
            logger.info("  EventMonitor DB: アップロード完了")

        except Exception as e:
            self.stats['eventmonitor_db']['errors'] += 1
            logger.error(f"  EventMonitor DB: アップロード失敗 - {e}")
            self._update_target_progress(
                'eventmonitor_db',
                status='failed',
                phase=self.target_states.get('eventmonitor_db', {}).get('phase', 'upload'),
                db_path=str(self.db_path),
                file_size_bytes=file_size,
                repo_path=hf_path,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # 3. Hydrus DB
    # ------------------------------------------------------------------
    def backup_hydrus_db(self):
        """Hydrus ClientのDBファイル（メディア除く）をHFにアップロード"""
        logger.info("=== Hydrus DB のバックアップ開始 ===")
        self._update_target_progress(
            'hydrus_db',
            status='running',
            phase='initializing',
            db_dir=str(self.hydrus_db_dir),
            total_expected_files=len(self.hydrus_db_files),
        )

        if not self.hydrus_db_dir.exists():
            logger.error(f"Hydrus DB フォルダが見つかりません: {self.hydrus_db_dir}")
            self._update_target_progress(
                'hydrus_db',
                status='failed',
                phase='initializing',
                db_dir=str(self.hydrus_db_dir),
                error=f"hydrus db dir not found: {self.hydrus_db_dir}",
            )
            return

        # WALファイルの存在チェック（Hydrus稼働中の警告）
        wal_files = list(self.hydrus_db_dir.glob("client*.db-wal"))
        active_wals = [f for f in wal_files if f.stat().st_size > 0]
        if active_wals:
            logger.warning(
                "Hydrus Clientが稼働中の可能性があります（WALファイルが存在）。"
                "DB整合性のため、Hydrusを停止してからバックアップすることを推奨します。"
            )

        uploaded_files = 0

        for db_file in self.hydrus_db_files:
            src = self.hydrus_db_dir / db_file
            if not src.exists():
                logger.warning(f"  {db_file}: ファイルが見つかりません、スキップ")
                continue

            file_size = src.stat().st_size
            hf_path_latest = f"backup/hydrus_db/{db_file}"

            logger.info(f"  {db_file} ({file_size / 1024 / 1024:.1f} MB)")

            if self.dry_run:
                self.stats['hydrus_db']['files'] += 1
                self.stats['hydrus_db']['size'] += file_size
                uploaded_files += 1
                continue

            try:
                self._update_target_progress(
                    'hydrus_db',
                    status='running',
                    phase='upload',
                    current_file=db_file,
                    file_size_bytes=file_size,
                    uploaded_files=uploaded_files,
                    total_expected_files=len(self.hydrus_db_files),
                    repo_path=hf_path_latest,
                )
                # 最新版
                self._upload_file_with_retry(
                    path_or_fileobj=str(src),
                    path_in_repo=hf_path_latest,
                )
                # スナップショット
                self._update_target_progress(
                    'hydrus_db',
                    status='running',
                    phase='upload',
                    current_file=db_file,
                    file_size_bytes=file_size,
                    uploaded_files=uploaded_files,
                    total_expected_files=len(self.hydrus_db_files),
                    repo_path=hf_path_latest,
                )
                self.stats['hydrus_db']['files'] += 1
                self.stats['hydrus_db']['size'] += file_size
                uploaded_files += 1
                logger.info(f"  {db_file}: アップロード完了")

            except Exception as e:
                self.stats['hydrus_db']['errors'] += 1
                logger.error(f"  {db_file}: アップロード失敗 - {e}")
                self._update_target_progress(
                    'hydrus_db',
                    status='failed',
                    phase=self.target_states.get('hydrus_db', {}).get('phase', 'upload'),
                    current_file=db_file,
                    file_size_bytes=file_size,
                    uploaded_files=uploaded_files,
                    total_expected_files=len(self.hydrus_db_files),
                    error=str(e),
                )
                return

        if self.dry_run:
            self._update_target_progress(
                'hydrus_db',
                status='dry_run',
                phase='completed',
                db_dir=str(self.hydrus_db_dir),
                uploaded_files=uploaded_files,
                total_expected_files=len(self.hydrus_db_files),
            )
        else:
            self._mark_db_uploaded(
                'hydrus_db',
                db_dir=str(self.hydrus_db_dir),
                uploaded_files=uploaded_files,
                total_expected_files=len(self.hydrus_db_files),
            )

    # ------------------------------------------------------------------
    # creator_mapping.json 生成（タグ集約・ファイル数・sensitive数を含む）
    # ------------------------------------------------------------------
    def generate_creator_mapping(self):
        """DBからクリエイターマッピングを生成し、HFリポジトリにアップロード

        生成される JSON の形式:
        {
          "folderId": {
            "name": "表示名",
            "platform": "twitter",
            "file_count": 123,
            "sensitive_count": 45,
            "tags": ["R-18", "オリジナル", ...]
          }
        }
        """
        logger.info("=== クリエイターマッピング生成 ===")

        if not self.db_path.exists():
            logger.warning(f"DB ファイルが見つかりません: {self.db_path} — スキップ")
            return

        try:
            from src.database import DatabaseManager, KemonoWork, KemonoLogOnlyWork
            from src.backup_manager import BackupManager

            # DB接続
            db_manager = DatabaseManager(self.config)
            mapping = {}
            session = db_manager._get_session()

            try:
                for platform, (monitor_model, log_model) in DatabaseManager.PLATFORM_MODELS.items():
                    identity_field = db_manager._get_platform_identity_field(platform)
                    prefix = BackupManager.PLATFORM_FOLDER_PREFIX.get(platform, '')

                    for model in [monitor_model, log_model]:
                        id_col = getattr(model, identity_field)
                        dn_col = model.display_name

                        if platform == 'kemono' and hasattr(model, 'service'):
                            rows = session.query(
                                id_col, dn_col, model.service
                            ).distinct().all()
                            for uid, display_name, service in rows:
                                if not uid:
                                    continue
                                folder = f"{service}_{uid}" if service else f"kemono_{uid}"
                                if folder not in mapping:
                                    mapping[folder] = {
                                        "name": display_name or folder,
                                        "platform": "kemono"
                                    }
                        else:
                            rows = session.query(id_col, dn_col).distinct().all()
                            for uid, display_name in rows:
                                if not uid:
                                    continue
                                folder = f"{prefix}{uid}" if prefix else uid
                                if folder not in mapping:
                                    mapping[folder] = {
                                        "name": display_name or folder,
                                        "platform": platform
                                    }
            finally:
                session.close()

            # --- タグ集約・ファイル数・sensitive数を追加 ---
            self._enrich_creator_mapping(mapping)

            logger.info(f"  マッピングエントリ数: {len(mapping)}")

            if self.dry_run:
                logger.info("  [dry-run] creator_mapping.json のアップロードをスキップ")
                return

            # JSON生成・アップロード
            temp_file = Path(tempfile.mktemp(suffix='.json'))
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(mapping, f, ensure_ascii=False, indent=2)

                self._upload_file_with_retry(
                    path_or_fileobj=str(temp_file),
                    path_in_repo="creator_mapping.json",
                )
                file_size = temp_file.stat().st_size / 1024
                logger.info(f"  creator_mapping.json アップロード完了（{len(mapping)}件, {file_size:.1f} KB）")
            finally:
                temp_file.unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"  クリエイターマッピング生成失敗: {e}", exc_info=True)

    def _enrich_creator_mapping(self, mapping: dict):
        """マッピングにタグ集約・ファイル数・sensitive数を追加（SQLiteから直接取得）"""
        import sqlite3
        from collections import Counter

        if not self.db_path.exists():
            return

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            # --- 1. Twitter (all_tweets + log_only_tweets) ---
            for table in ['all_tweets', 'log_only_tweets']:
                try:
                    cursor.execute(f"""
                        SELECT username,
                               COUNT(*) as file_count,
                               SUM(CASE WHEN sensitive = 1 THEN 1 ELSE 0 END) as sensitive_count
                        FROM [{table}]
                        WHERE huggingface_urls IS NOT NULL
                          AND huggingface_urls != ''
                          AND huggingface_urls != '[]'
                        GROUP BY username
                    """)
                    for username, file_count, sensitive_count in cursor.fetchall():
                        folder = username
                        if folder in mapping:
                            mapping[folder].setdefault("file_count", 0)
                            mapping[folder]["file_count"] += file_count
                            mapping[folder].setdefault("sensitive_count", 0)
                            mapping[folder]["sensitive_count"] += (sensitive_count or 0)
                except Exception as e:
                    logger.warning(f"  {table} の集約に失敗: {e}")

            # --- 2. プラットフォーム別テーブル ---
            platform_configs = {
                'pixiv': ('pixiv_works', 'user_id', ''),
                'fanbox': ('fanbox_works', 'user_id', 'fanbox_'),
                'fantia': ('fantia_works', 'user_id', 'fantia_'),
                'nijie': ('nijie_works', 'user_id', 'nijie_'),
                'skeb': ('skeb_works', 'user_id', 'skeb_'),
                'misskey': ('misskey_works', 'user_id', 'misskey_'),
                'bluesky': ('bluesky_works', 'user_id', 'bluesky_'),
                'gelbooru': ('gelbooru_works', 'user_id', 'gelbooru_'),
                'poipiku': ('poipiku_works', 'user_id', ''),
                'tinami': ('tinami_works', 'user_id', ''),
                'privatter': ('privatter_works', 'user_id', ''),
            }

            for platform, (table, id_field, prefix) in platform_configs.items():
                try:
                    # カラムの存在確認
                    cursor.execute(f"PRAGMA table_info([{table}])")
                    columns = {col[1] for col in cursor.fetchall()}
                    has_tags = 'tags' in columns
                    has_sensitive = 'sensitive' in columns

                    # ファイル数・sensitive数
                    sensitive_sel = ", SUM(CASE WHEN sensitive = 1 THEN 1 ELSE 0 END)" if has_sensitive else ""
                    cursor.execute(f"""
                        SELECT {id_field}, COUNT(*) {sensitive_sel}
                        FROM [{table}]
                        GROUP BY {id_field}
                    """)
                    for row in cursor.fetchall():
                        uid = row[0]
                        count = row[1]
                        sens = row[2] if has_sensitive else 0
                        if not uid:
                            continue
                        folder = f"{prefix}{uid}" if prefix else uid
                        if folder in mapping:
                            mapping[folder].setdefault("file_count", 0)
                            mapping[folder]["file_count"] += count
                            mapping[folder].setdefault("sensitive_count", 0)
                            mapping[folder]["sensitive_count"] += (sens or 0)

                    # タグ集約（タグ列があるテーブルのみ）
                    if has_tags:
                        cursor.execute(f"""
                            SELECT {id_field}, tags
                            FROM [{table}]
                            WHERE tags IS NOT NULL AND tags != '' AND tags != '[]'
                        """)
                        # ユーザー別にタグカウント
                        user_tags: dict[str, Counter] = {}
                        for uid, tags_json in cursor.fetchall():
                            if not uid:
                                continue
                            try:
                                tags = json.loads(tags_json) if tags_json else []
                            except (json.JSONDecodeError, TypeError):
                                continue
                            folder = f"{prefix}{uid}" if prefix else uid
                            if folder not in user_tags:
                                user_tags[folder] = Counter()
                            for tag in tags:
                                if tag:
                                    user_tags[folder][tag] += 1

                        for folder, counter in user_tags.items():
                            if folder in mapping:
                                # 頻度上位30タグを保存
                                top_tags = [tag for tag, _ in counter.most_common(30)]
                                mapping[folder]["tags"] = top_tags

                except Exception as e:
                    logger.warning(f"  {table} の集約に失敗: {e}")
                    continue

            # --- 3. kemono （特殊: {service}_{user_id}） ---
            for table in ['kemono_works', 'kemono_log_only_works']:
                try:
                    cursor.execute(f"PRAGMA table_info([{table}])")
                    columns = {col[1] for col in cursor.fetchall()}
                    has_sensitive = 'sensitive' in columns
                    has_service = 'service' in columns

                    if not has_service:
                        continue

                    sensitive_sel = ", SUM(CASE WHEN sensitive = 1 THEN 1 ELSE 0 END)" if has_sensitive else ""
                    cursor.execute(f"""
                        SELECT user_id, service, COUNT(*) {sensitive_sel}
                        FROM [{table}]
                        GROUP BY user_id, service
                    """)
                    for row in cursor.fetchall():
                        uid = row[0]
                        service = row[1]
                        count = row[2]
                        sens = row[3] if has_sensitive else 0
                        if not uid:
                            continue
                        folder = f"{service}_{uid}" if service else f"kemono_{uid}"
                        if folder in mapping:
                            mapping[folder].setdefault("file_count", 0)
                            mapping[folder]["file_count"] += count
                            mapping[folder].setdefault("sensitive_count", 0)
                            mapping[folder]["sensitive_count"] += (sens or 0)
                except Exception as e:
                    logger.warning(f"  {table} の集約に失敗: {e}")

            # tags が設定されていないエントリにはデフォルト値
            for folder, entry in mapping.items():
                entry.setdefault("file_count", 0)
                entry.setdefault("sensitive_count", 0)
                entry.setdefault("tags", [])

            enriched_count = sum(1 for e in mapping.values() if e.get("file_count", 0) > 0)
            tag_count = sum(1 for e in mapping.values() if len(e.get("tags", [])) > 0)
            logger.info(f"  集約完了: {enriched_count}件にファイル数, {tag_count}件にタグ情報を追加")

        except Exception as e:
            logger.error(f"  マッピング集約に失敗: {e}", exc_info=True)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 4. Discord保存フォルダ（現在無効）
    # ------------------------------------------------------------------
    def backup_discord(self, delete_after: bool = False):
        """Discordエクスポートフォルダ全体をHFにアップロード（upload_large_folder使用）"""
        logger.info("=== Discord保存フォルダのバックアップ開始 ===")

        discord_config = self.config.get('discord_crawler', {})
        discord_exports_dir = Path(discord_config.get('exports_dir', 'F:/Crawler/Discord'))

        if not discord_exports_dir.exists():
            logger.error(f"Discord exports フォルダが見つかりません: {discord_exports_dir}")
            return

        guild_dirs = sorted([d for d in discord_exports_dir.iterdir() if d.is_dir()])
        if not guild_dirs:
            logger.info("Discord: アップロード対象なし")
            return

        # サマリー
        total_files = 0
        total_size = 0
        for guild_dir in guild_dirs:
            files = [f for f in guild_dir.rglob('*') if f.is_file()]
            total_files += len(files)
            total_size += sum(f.stat().st_size for f in files)

        logger.info(
            f"Discord: {len(guild_dirs)} ギルド, "
            f"{total_files} ファイル ({total_size / 1024 / 1024:.1f} MB)"
        )

        if self.dry_run:
            self.stats.setdefault('discord', {'files': 0, 'size': 0, 'errors': 0, 'skipped': 0})
            self.stats['discord']['files'] = total_files
            self.stats['discord']['size'] = total_size
            return

        self.stats.setdefault('discord', {'files': 0, 'size': 0, 'errors': 0, 'skipped': 0})
        try:
            self.api.upload_large_folder(
                repo_id=self.repo_name,
                folder_path=str(discord_exports_dir),
                repo_type="dataset",
                num_workers=4,
            )
            self.stats['discord']['files'] = total_files
            self.stats['discord']['size'] = total_size
            logger.info("Discord保存フォルダ: アップロード完了")

            if delete_after:
                for guild_dir in guild_dirs:
                    shutil.rmtree(guild_dir)
                logger.info("Discord: ローカルフォルダ削除完了")

        except Exception as e:
            self.stats['discord']['errors'] += 1
            logger.error(f"Discord保存フォルダ: アップロード失敗 - {e}")

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def run(self, targets: Optional[list] = None, delete_after: bool = False):
        """バックアップを実行"""
        if targets is None:
            targets = self.TARGETS

        self.active_targets = list(dict.fromkeys(targets))
        self.run_status = 'running'
        self._save_progress()

        start_time = datetime.now()
        mode = "ドライラン" if self.dry_run else "実行"
        logger.info(f"定期バックアップ {mode}開始 — 対象: {', '.join(targets)}")
        logger.info(f"リポジトリ: {self.repo_name}")
        run_failed = False

        try:
            # DB系を先に実行（メディアアップロードは長時間かかるため後回し）
            if 'eventmonitor_db' in targets:
                self.backup_eventmonitor_db()

            if 'hydrus_db' in targets:
                self.backup_hydrus_db()

            # DB バックアップ後にクリエイターマッピング生成
            if 'eventmonitor_db' in targets:
                self.generate_creator_mapping()

            if 'crawler_media' in targets:
                self.backup_crawler_media(delete_after=delete_after)

            if 'discord' in targets:
                self.backup_discord(delete_after=delete_after)
        except Exception:
            run_failed = True
            self.run_status = 'failed'
            raise
        finally:
            if not run_failed:
                self.run_status = 'completed'
            elapsed = datetime.now() - start_time
            self._print_summary(elapsed)
            self._save_progress(remove_active_run=True, final_status=self.run_status)

    def _print_summary(self, elapsed):
        """結果サマリーを出力"""
        logger.info("=" * 60)
        logger.info(f"バックアップ完了 (所要時間: {elapsed})")
        logger.info("-" * 60)

        total_files = 0
        total_size = 0
        total_errors = 0

        for target, stat in self.stats.items():
            if stat['files'] > 0 or stat['errors'] > 0 or stat.get('skipped', 0) > 0:
                size_mb = stat['size'] / 1024 / 1024
                skipped = stat.get('skipped', 0)
                status = 'errors: ' + str(stat['errors']) if stat['errors'] else 'OK'
                skip_info = f" skipped: {skipped}" if skipped else ""
                logger.info(
                    f"  {target:20s}: {stat['files']:>6d} files "
                    f"({size_mb:>8.1f} MB) "
                    f"{status}{skip_info}"
                )
                total_files += stat['files']
                total_size += stat['size']
                total_errors += stat['errors']

        logger.info("-" * 60)
        logger.info(
            f"  {'合計':20s}: {total_files:>6d} files "
            f"({total_size / 1024 / 1024:>8.1f} MB) "
            f"errors: {total_errors}"
        )
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='EventMonitor 定期バックアップ（HuggingFace）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--target',
        choices=ScheduledBackup.TARGETS,
        action='append',
        help='バックアップ対象（複数指定可、省略時は全て）',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='アップロードせず対象ファイルを確認するだけ',
    )
    parser.add_argument(
        '--delete-after',
        action='store_true',
        help='アップロード成功後にクローラー/Discordフォルダを削除',
    )
    parser.add_argument(
        '--reset-progress',
        action='store_true',
        help='進捗をリセットして全ターゲットを再アップロード対象にする',
    )
    parser.add_argument(
        '--config',
        default='config.yaml',
        help='config.yamlのパス（デフォルト: config.yaml）',
    )
    args = parser.parse_args()

    backup = ScheduledBackup(config_path=args.config, dry_run=args.dry_run)

    if args.reset_progress:
        backup.reset_progress()

    backup.run(targets=args.target, delete_after=args.delete_after)


if __name__ == '__main__':
    main()
