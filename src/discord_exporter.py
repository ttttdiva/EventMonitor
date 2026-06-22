"""
Discord Exporter モジュール

DiscordChatExporter CLI を使用してDiscordサーバーのチャットログをエクスポートする。
52_DiscordCrawler から移植し、EventMonitorのasyncアーキテクチャに統合。
"""
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple


class DiscordExporter:
    """DiscordChatExporter CLIのasyncラッパー"""

    def __init__(self, config: dict):
        """
        初期化

        Args:
            config: EventMonitorの設定辞書（config.yaml由来）
        """
        self.config = config
        self.logger = logging.getLogger("EventMonitor.DiscordExporter")

        dc_config = config.get('discord_crawler', {})
        self.enabled = dc_config.get('enabled', False)

        # CLIパス
        cli_path_str = dc_config.get('cli_path', 'tools/DiscordChatExporter.Cli.win-x64/DiscordChatExporter.Cli.exe')
        self.cli_path = Path(cli_path_str)
        if not self.cli_path.is_absolute():
            self.cli_path = Path.cwd() / self.cli_path

        # エクスポート先ディレクトリ
        exports_dir_str = dc_config.get('exports_dir', 'exports/discord')
        self.exports_dir = Path(exports_dir_str)
        if not self.exports_dir.is_absolute():
            self.exports_dir = Path.cwd() / self.exports_dir

        # データディレクトリ（履歴ファイル保存先）
        data_dir_str = config.get('system', {}).get('data_dir', 'data')
        self.data_dir = Path(data_dir_str)
        if not self.data_dir.is_absolute():
            self.data_dir = Path.cwd() / self.data_dir

        # 設定値
        self.output_format = dc_config.get('output_format', 'HtmlDark')
        self.download_media = dc_config.get('download_media', True)
        self.include_threads = dc_config.get('include_threads', 'All')

        # トークン（.envから取得）
        self.token = os.getenv('DISCORD_CRAWLER_TOKEN')

        # ディレクトリ作成
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 初期検証
        if self.enabled:
            if not self.token:
                self.logger.warning("DISCORD_CRAWLER_TOKEN が設定されていません。Discord Crawlerは無効です。")
                self.enabled = False
            elif not self.cli_path.exists():
                self.logger.warning(f"DiscordChatExporter CLI が見つかりません: {self.cli_path}")
                self.enabled = False


    # --- 履歴管理 ---

    def _history_path(self, guild_id: str) -> Path:
        """エクスポート履歴ファイルのパス"""
        return self.data_dir / f"export_history_{guild_id}.json"

    def _legacy_path(self, guild_id: str) -> Path:
        """レガシー last_export ファイルのパス"""
        return self.data_dir / f"last_export_{guild_id}.txt"

    def _load_history(self, guild_id: str) -> Dict:
        """
        エクスポート履歴を読み込み

        Returns:
            {
                "last_run_completed": "iso-time",
                "channels": {"channel_id": "iso-time"}
            }
        """
        history_file = self._history_path(guild_id)

        if history_file.exists():
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self.logger.error(f"履歴ファイル読み込みエラー: {e}")

        return {
            "last_run_completed": None,
            "channels": {}
        }

    def _save_history(self, guild_id: str, history: Dict):
        """エクスポート履歴を保存"""
        history_file = self._history_path(guild_id)
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            self.logger.error(f"履歴ファイル保存エラー: {e}")

    def _save_last_export_time(self, guild_id: str):
        """最後のエクスポート時刻を保存（レガシー互換）"""
        try:
            timestamp_file = self._legacy_path(guild_id)
            with open(timestamp_file, 'w') as f:
                f.write(datetime.now().isoformat())
        except Exception as e:
            self.logger.warning(f"タイムスタンプ保存エラー: {e}")

    def _get_last_export_time(self, guild_id: str) -> Optional[datetime]:
        """最後のエクスポート時刻を取得（レガシーファイルから）"""
        try:
            timestamp_file = self._legacy_path(guild_id)
            if timestamp_file.exists():
                with open(timestamp_file, 'r') as f:
                    return datetime.fromisoformat(f.read().strip())
        except Exception:
            pass
        return None

    def is_first_export(self, guild_id: str) -> bool:
        """初回エクスポートかどうかを判定"""
        if self._history_path(guild_id).exists():
            return False
        if self._get_last_export_time(guild_id) is not None:
            return False
        return True

    # --- コマンド実行 ---

    async def _run_command(self, command: List[str], timeout: Optional[int] = 300) -> Tuple[bool, str, str]:
        """
        コマンドを非同期実行

        Args:
            command: 実行するコマンドのリスト
            timeout: タイムアウト時間（秒）。None の場合は無制限

        Returns:
            (成功フラグ, 標準出力, エラー出力)
        """
        try:
            self.logger.info(f"実行コマンド: {' '.join(command)}")

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.cwd())
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self.logger.error(f"コマンド実行タイムアウト ({timeout}秒)")
                return False, "", "タイムアウト"

            stdout = stdout_bytes.decode('utf-8', errors='replace').strip() if stdout_bytes else ""
            stderr = stderr_bytes.decode('utf-8', errors='replace').strip() if stderr_bytes else ""

            success = process.returncode == 0

            if success:
                self.logger.info("コマンド実行成功")
                if stdout:
                    self.logger.debug(f"標準出力: {stdout}")
            else:
                self.logger.error(f"コマンド実行失敗 (リターンコード: {process.returncode})")
                if stderr:
                    self.logger.error(f"エラー出力: {stderr}")

            return success, stdout, stderr

        except Exception as e:
            self.logger.error(f"コマンド実行エラー: {e}")
            return False, "", str(e)

    # --- チャンネル取得 ---

    async def get_guild_channels(self, guild_id: str) -> List[Dict]:
        """
        サーバーのチャンネル一覧を取得

        Args:
            guild_id: サーバーID

        Returns:
            チャンネル情報のリスト [{'id': '...', 'name_full': '...'}, ...]
        """
        command = [
            str(self.cli_path),
            "channels",
            "--token", self.token,
            "--guild", guild_id,
            "--include-threads", self.include_threads,
        ]

        success, stdout, stderr = await self._run_command(command, timeout=300)

        channels = []
        if success and stdout:
            for line in stdout.splitlines():
                # Format: ID | Category / Name
                # スレッドは "* ID | ..." のようにアスタリスクプレフィックス付き
                parts = line.split(" | ", 1)
                if len(parts) >= 1:
                    channel_id = parts[0].strip().lstrip('* ')
                    if not channel_id.isdigit():
                        continue
                    name_full = parts[1].strip() if len(parts) > 1 else ""
                    channels.append({
                        'id': channel_id,
                        'name_full': name_full
                    })
        return channels

    # --- エクスポート ---

    async def export_guild_channels(self, guild_id: str, force_initial: bool = False) -> Tuple[bool, int]:
        """
        サーバーのチャンネル単位エクスポートを実行

        Args:
            guild_id: サーバーID
            force_initial: 強制的に初回エクスポート（履歴無視）

        Returns:
            (成功フラグ, 処理チャンネル数)
        """
        try:
            # チャンネル一覧取得
            self.logger.info(f"チャンネル一覧を取得中: サーバー {guild_id}")
            channels = await self.get_guild_channels(guild_id)
            if not channels:
                self.logger.error("チャンネル一覧が取得できませんでした")
                return False, 0

            self.logger.info(f"取得チャンネル数: {len(channels)}")

            # 履歴ロード
            history = self._load_history(guild_id)

            # レガシー履歴チェック（後方互換性）
            legacy_last_export = self._get_last_export_time(guild_id)
            now = datetime.now()

            # 出力先ベース: exports/guild_{id}
            output_base = self.exports_dir / f"guild_{guild_id}"
            output_base.mkdir(parents=True, exist_ok=True)

            # チャンネルごとにループ
            success_count = 0
            fail_count = 0

            for idx, channel in enumerate(channels):
                channel_id = channel['id']
                channel_name = channel['name_full']

                # このチャンネルの最終エクスポート時刻
                last_export_str = history['channels'].get(channel_id)
                last_export = datetime.fromisoformat(last_export_str) if last_export_str else None

                # レガシー互換: 個別履歴がない＆レガシー全履歴がある場合、それを使用
                if not last_export and legacy_last_export:
                    last_export = legacy_last_export

                # エクスポート実行
                self.logger.info(f"[{idx+1}/{len(channels)}] エクスポート開始: {channel_name}")

                # コマンド構築
                output_template = str(output_base / "%T" / "%C [%c].html")

                cmd = [
                    str(self.cli_path),
                    "export",
                    "--token", self.token,
                    "--channel", channel_id,
                    "--output", output_template,
                    "--format", self.output_format,
                    "--include-threads", self.include_threads,
                ]

                if self.download_media:
                    cmd.append("--media")

                # 増分オプション
                if not force_initial and last_export:
                    cmd.extend(["--after", last_export.strftime("%Y-%m-%dT%H:%M:%S")])

                success, _, stderr = await self._run_command(cmd, timeout=None)

                if success:
                    # 履歴更新
                    history['channels'][channel_id] = now.isoformat()
                    self._save_history(guild_id, history)
                    success_count += 1
                elif "is a forum" in stderr:
                    # フォーラムチャンネル自体は直接エクスポート不可（スレッドが個別に処理される）
                    self.logger.warning(f"フォーラムチャンネルをスキップ（スレッドは個別処理）: {channel_name}")
                    success_count += 1
                else:
                    self.logger.error(f"エクスポート失敗: {channel_name}")
                    fail_count += 1

            # 全チャンネルループ完了
            self.logger.info(
                f"チャンネル単位処理完了: 全{len(channels)}件 / 成功{success_count} / 失敗{fail_count}"
            )
            return True, len(channels)

        except Exception as e:
            self.logger.error(f"チャンネルエクスポート中にエラー: {e}")
            return False, 0

    async def export_guild_batch(self, guild_id: str, force_initial: bool = False) -> bool:
        """
        サーバーの一括エクスポート（増分）を実行

        Args:
            guild_id: サーバーID
            force_initial: 強制的に初回エクスポート（履歴無視）

        Returns:
            成功フラグ
        """
        try:
            # 履歴ロード
            history = self._load_history(guild_id)
            legacy_last_export = self._get_last_export_time(guild_id)
            now = datetime.now()

            # 出力先ベース: exports/guild_{id}
            output_base = self.exports_dir / f"guild_{guild_id}"
            output_base.mkdir(parents=True, exist_ok=True)

            # サーバー一括増分エクスポート
            self.logger.info(f"サーバー一括増分エクスポート開始: {guild_id}")
            guild_cmd = [
                str(self.cli_path),
                "exportguild",
                "--token", self.token,
                "--guild", guild_id,
                "--output", str(output_base),
                "--format", self.output_format,
                "--include-threads", self.include_threads,
            ]

            if self.download_media:
                guild_cmd.append("--media")

            # 増分オプション（最終エクスポート時刻がある場合）
            if not force_initial and legacy_last_export:
                guild_cmd.extend(["--after", legacy_last_export.strftime("%Y-%m-%dT%H:%M:%S")])

            guild_success, _, _ = await self._run_command(guild_cmd, timeout=None)

            if guild_success:
                self.logger.info(f"サーバー一括増分エクスポート完了: {guild_id}")
            else:
                self.logger.error(f"サーバー一括増分エクスポート失敗: {guild_id}")

            # Guildとしての完了時刻を更新
            history['last_run_completed'] = now.isoformat()
            self._save_history(guild_id, history)
            # レガシーファイルも更新しておく
            self._save_last_export_time(guild_id)

            return guild_success

        except Exception as e:
            self.logger.error(f"サーバー一括エクスポート中にエラー: {e}")
            return False

    async def export_guild(self, guild_id: str, force_initial: bool = False) -> Tuple[bool, int]:
        """
        サーバーのエクスポートを実行（チャンネル単位 + バッチ）

        Args:
            guild_id: サーバーID
            force_initial: 強制的に初回エクスポート（履歴無視）

        Returns:
            (成功フラグ, 処理チャンネル数)
        """
        channels_success, channel_count = await self.export_guild_channels(guild_id, force_initial)
        batch_success = await self.export_guild_batch(guild_id, force_initial)

        return (channels_success or batch_success), channel_count
