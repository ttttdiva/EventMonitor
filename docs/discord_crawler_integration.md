# DiscordCrawler統合計画書

## 概要

`C:\nk\52_DiscordCrawler` のDiscordサーバーアーカイブ機能をEventMonitorに統合する。
DiscordChatExporter.Cli.exe + 既存のPython wrapper (`discord_crawler.py`) をそのまま持ち込み、EventMonitorのメインループから呼び出す。

## 移行対象ファイル

### 持ち込むもの

| 移行元 (52_DiscordCrawler) | 移行先 (48_EventMonitor) | 備考 |
|---|---|---|
| `discord_crawler.py` | `src/discord_exporter.py` | asyncラッパー化 + base_dir調整 |
| `DiscordChatExporter.Cli.win-x64/` | `tools/DiscordChatExporter.Cli.win-x64/` | EXE + .NET DLL一式 |
| `DiscordChatExporter-incrementalBackup/` | `tools/DiscordChatExporter-incrementalBackup/` | 増分バックアップツール（現在未使用だが念のため） |
| `config/servers.csv` | `config/discord_servers.csv` | サーバー設定 |
| `config/tokens.csv` | 不要 | `.env` の `DISCORD_TOKEN` に統一 |
| `logs/export_history_*.json` | `data/discord_export_history_*.json` | 既存の履歴データを移行 |

### 持ち込まないもの

| ファイル | 理由 |
|---|---|
| `scheduler.py` | EventMonitorの `main.py` ループが代替 |
| `status_notifier.py` | EventMonitorの `src/status_notifier.py` に統合 |
| `main.py` | エントリポイントは不要 |
| `requirements.txt` | EventMonitorの requirements.txt に `pandas` を追加するだけ |
| `venv/` | EventMonitorの環境を使用 |
| `.env` | EventMonitorの `.env` にマージ |

## 実装手順

### Step 1: ファイル配置

1. `tools/DiscordChatExporter.Cli.win-x64/` にEXE一式をコピー
2. `tools/DiscordChatExporter-incrementalBackup/` に `backup.exe` 一式をコピー
3. `config/discord_servers.csv` を作成（52_DiscordCrawler の `config/servers.csv` を移行）
4. `.gitignore` に `tools/DiscordChatExporter*/` を追加（バイナリはgit管理しない）
5. 既存の `logs/export_history_*.json` を `data/discord_export_history_*.json` に移行

### Step 2: `src/discord_exporter.py` 作成

52_DiscordCrawler の `discord_crawler.py` (`DiscordChatExporter`クラス) を移植。

**変更点:**
- `base_dir` → EventMonitorのプロジェクトルートを基準に
- `cli_path` → `tools/DiscordChatExporter.Cli.win-x64/DiscordChatExporter.Cli.exe`
- `exports_dir` → config.yaml の `discord_crawler.exports_dir` から取得
- `logs_dir` → `data/` に統一（`export_history_*.json` の保存先）
- `_setup_logging()` → 削除（EventMonitorのロガーを使用）
- `load_config()` → tokens は `.env` から、servers は `config/discord_servers.csv` から
- `run_command()` → `asyncio.create_subprocess_exec` に変換
- `pandas` 依存 → 標準ライブラリ `csv` モジュールに置換（EventMonitorの他コンポーネントと統一）

**公開メソッド（そのまま維持）:**
```python
class DiscordExporter:
    async def export_guild(self, guild_id: str, force_initial: bool = False) -> bool
    async def export_guild_channels(self, guild_id: str, force_initial: bool = False) -> bool
    async def export_guild_batch(self, guild_id: str, force_initial: bool = False) -> bool
    def get_enabled_servers(self) -> List[Dict]
    def is_first_export(self, guild_id: str) -> bool
```

### Step 3: config.yaml に `discord_crawler` セクション追加

```yaml
# Discord Crawler設定（サーバーアーカイブ）
discord_crawler:
  enabled: true
  # DiscordChatExporter CLIのパス
  cli_path: "tools/DiscordChatExporter.Cli.win-x64/DiscordChatExporter.Cli.exe"
  # エクスポート先ディレクトリ
  exports_dir: "F:/Crawler/Discord"
  # サーバー設定CSVのパス
  servers_csv: "config/discord_servers.csv"
  # 出力フォーマット (HtmlDark, HtmlLight, PlainText, Json, Csv)
  output_format: "HtmlDark"
  # メディアもダウンロードするか
  download_media: true
```

### Step 4: `.env` にDiscord Crawler用トークン追加

```env
# Discord Crawler用トークン（既存のDISCORD_BOT_TOKENとは別）
# DiscordChatExporterはユーザートークンまたはBotトークンを使用
DISCORD_CRAWLER_TOKEN=MjE3NDUwMjM2ODc...
```

既に `DISCORD_BOT_TOKEN` が存在するが、こちらはBot REST API用（status_notifier, discord_ingest）。
DiscordChatExporterはサーバーのチャンネル一覧取得・メッセージエクスポートに使うため、別トークンとして管理。

### Step 5: main.py にDiscord Crawler処理を追加

`EventMonitor.__init__()` に追加:
```python
from src.discord_exporter import DiscordExporter

# Discord Crawler
self.discord_exporter = DiscordExporter(self.config) if self.config.get('discord_crawler', {}).get('enabled', False) else None
```

`run_once()` のプラットフォーム処理の後に追加:
```python
# --- Discord Crawlerのサーバーエクスポート ---
if self.discord_exporter:
    await self._process_discord_exports()
```

新メソッド:
```python
async def _process_discord_exports(self):
    """Discord Crawlerのサーバーエクスポートを実行"""
    servers = self.discord_exporter.get_enabled_servers()
    if not servers:
        self.logger.info("No enabled Discord servers to export")
        return

    self.logger.info(f"Starting Discord server exports ({len(servers)} servers)")

    for server in servers:
        guild_id = str(server['server_id'])
        server_name = server.get('server_name', guild_id)

        try:
            self.logger.info(f"Exporting Discord server: {server_name} ({guild_id})")
            self.status_notifier.notify_running(current_account=f"Discord: {server_name}")

            success = await self.discord_exporter.export_guild(guild_id)

            if success:
                self.logger.info(f"Discord export completed: {server_name}")
            else:
                self.logger.error(f"Discord export failed: {server_name}")
        except Exception as e:
            self.logger.error(f"Discord export error for {server_name}: {e}", exc_info=True)
```

### Step 6: StatusNotifier にDiscord Crawler統計を追加

`src/status_notifier.py` の `StatusNotifier` クラスに追加:
```python
# 統計情報に追加
self.processed_discord_servers = 0

def increment_processed_discord_servers(self):
    self.processed_discord_servers += 1
```

ダッシュボードEmbedにもDiscord Crawlerのフィールドを追加（処理済みサーバー数など）。

### Step 7: `.gitignore` 更新

```gitignore
# Discord Chat Exporter binaries
tools/DiscordChatExporter.Cli.win-x64/
tools/DiscordChatExporter-incrementalBackup/
```

## 設定ファイル対応表

| 52_DiscordCrawler の設定 | 48_EventMonitor での対応 |
|---|---|
| `.env` `DISCORD_TOKEN` | `.env` `DISCORD_CRAWLER_TOKEN` |
| `.env` `EXPORTS_DIR` | `config.yaml` `discord_crawler.exports_dir` |
| `.env` `DEFAULT_OUTPUT_FORMAT` | `config.yaml` `discord_crawler.output_format` |
| `.env` `LOG_LEVEL` | `config.yaml` `system.log_level`（既存） |
| `.env` `DISCORD_BOT_TOKEN` | `.env` `DISCORD_BOT_TOKEN`（既存・共用） |
| `.env` `DISCORD_DASHBOARD_CHANNEL_ID` | `.env` 既存をそのまま使用 |
| `.env` `DISCORD_ERROR_LOG_CHANNEL_ID` | `.env` 既存をそのまま使用 |
| `.env` `CRAWLER_API_KEY` | `.env` 既存をそのまま使用 |
| `.env` `AOITALK_API_URL` | `.env` 既存をそのまま使用 |
| `config/tokens.csv` | 廃止。`.env` に統一 |
| `config/servers.csv` | `config/discord_servers.csv` にリネーム移行 |

## 注意事項

- **DiscordChatExporter.Cli.exe は ~100MB** のバイナリ。git管理せず、初回セットアップ時に手動配置 or セットアップスクリプトで自動DL
- **pandas依存の除去**: 52_DiscordCrawlerは `pandas` でCSV読み込みしているが、EventMonitor側は標準の `csv` モジュールを使用しているため、移植時に `csv.DictReader` に置換して依存を増やさない
- **sync → async変換**: `subprocess.run()` を `asyncio.create_subprocess_exec()` に書き換え。EventMonitorのメインループはasyncioで動作するため、同期的なsubprocess呼び出しはイベントループをブロックする
- **エクスポート履歴の互換性**: 既存の `export_history_*.json` はパス変更のみで互換。レガシーの `last_export_*.txt` もそのまま読み込み対応を維持
- **throttle_hours**: 52_DiscordCrawlerではサーバーごとに `throttle_hours` を設定できたが、EventMonitor統合後は `system.check_interval` のサイクルごとに全サーバーをエクスポートする方式に変更。サーバー個別のスロットルが必要な場合は、エクスポート履歴の `last_run_completed` を参照してスキップするロジックを追加する

## 統合後のディレクトリ構成（抜粋）

```
C:\nk\48_EventMonitor/
├── main.py
├── config.yaml                          # discord_crawler セクション追加
├── monitored_accounts.csv
├── config/
│   └── discord_servers.csv              # NEW: Discord サーバー設定
├── src/
│   ├── discord_exporter.py              # NEW: DiscordChatExporter wrapper
│   ├── status_notifier.py               # 変更: Discord Crawler統計追加
│   └── ... (既存ファイル)
├── tools/
│   ├── DiscordChatExporter.Cli.win-x64/ # NEW: CLI バイナリ
│   └── DiscordChatExporter-incrementalBackup/ # NEW: 増分ツール
├── data/
│   ├── eventmonitor.db
│   └── discord_export_history_*.json    # NEW: エクスポート履歴
└── exports/ or F:/Crawler/Discord/       # エクスポート出力先
```

## テスト計画

1. **単体**: `discord_exporter.py` のCSV読み込み・コマンド構築のテスト（CLI実行なし）
2. **結合**: `python main.py` 実行で Discord Crawler が正常にキックされるか確認
3. **増分**: 2回目実行時に `--after` 引数が正しく付与されるか確認
4. **エラー**: CLI不在時 / トークン未設定時にグレースフルに失敗するか確認
5. **既存機能**: Twitter / Pixiv / Kemono の処理に影響がないか確認
