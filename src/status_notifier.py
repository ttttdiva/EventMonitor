"""
EventMonitor ステータス通知モジュール

DiscordダッシュボードとAoiTalkへのステータス配信を一元管理する。
"""
import os
import time
import logging
from typing import Optional, Dict, Any
from datetime import datetime
import requests
import urllib3

# 自己署名証明書の警告を抑制
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DiscordDashboardNotifier:
    """Discordダッシュボードへのステータス通知を管理するクラス"""
    
    # ステータスに応じた色定義
    STATUS_COLORS = {
        "running": 0x00FF00,  # 緑
        "idle": 0x808080,     # グレー
        "error": 0xFF0000,    # 赤
        "starting": 0xFFFF00, # 黄色
        "stopped": 0x0066FF   # 青
    }
    
    def __init__(
        self,
        bot_token: str,
        dashboard_channel_id: str,
        error_log_channel_id: str,
        min_update_interval: int = 60,
        enabled: bool = True
    ):
        """
        Args:
            bot_token: Discord Bot Token
            dashboard_channel_id: ダッシュボード用チャンネルID
            error_log_channel_id: エラーログ用チャンネルID
            min_update_interval: 最小更新間隔（秒）
            enabled: 通知が有効かどうか
        """
        self.bot_token = bot_token
        self.dashboard_channel_id = dashboard_channel_id
        self.error_log_channel_id = error_log_channel_id
        self.min_update_interval = min_update_interval
        self.enabled = enabled
        
        self.dashboard_message_id: Optional[str] = None
        self.last_update_time: float = 0
        self.last_status: Optional[str] = None
        
        self.base_url = "https://discordapp.com/api"
        self.headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json"
        }
        
        self.logger = logging.getLogger("EventMonitor.StatusNotifier.Discord")
        
        if self.enabled:
            self._initialize_dashboard()
    
    def _initialize_dashboard(self):
        """ダッシュボードメッセージを初期化（既存を探すか新規作成）"""
        try:
            # 過去のメッセージを取得
            url = f"{self.base_url}/channels/{self.dashboard_channel_id}/messages?limit=50"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                messages = response.json()
                # 自分が送信したメッセージでEmbedを含むものを探す
                for message in messages:
                    if (message.get("author", {}).get("bot") and 
                        len(message.get("embeds", [])) > 0 and
                        message["embeds"][0].get("title") == "🐦 EventMonitor Status"):
                        self.dashboard_message_id = message["id"]
                        self.logger.info(f"既存のダッシュボードメッセージを発見: {self.dashboard_message_id}")
                        return
                
                # 既存メッセージが見つからない場合は新規作成
                self._create_dashboard_message()
            else:
                self.logger.warning(f"メッセージ取得失敗: {response.status_code}")
                self._create_dashboard_message()
                
        except Exception as e:
            self.logger.error(f"ダッシュボード初期化エラー: {e}")
    
    def _create_dashboard_message(self):
        """新規ダッシュボードメッセージを作成"""
        try:
            embed = self._build_embed("starting", {
                "processed_accounts": 0,
                "new_tweets": 0,
                "event_tweets": 0,
                "error_count": 0
            })
            
            url = f"{self.base_url}/channels/{self.dashboard_channel_id}/messages"
            payload = {"embeds": [embed]}
            
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                message = response.json()
                self.dashboard_message_id = message["id"]
                self.logger.info(f"新規ダッシュボードメッセージを作成: {self.dashboard_message_id}")
            else:
                self.logger.error(f"メッセージ作成失敗: {response.status_code}, {response.text}")
                
        except Exception as e:
            self.logger.error(f"ダッシュボード作成エラー: {e}")
    
    def _build_embed(self, status: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Embedを構築"""
        status_text = {
            "running": "🟢 実行中",
            "idle": "⚪ 待機中",
            "error": "🔴 エラー",
            "starting": "🟡 起動中",
            "stopped": "🔵 停止"
        }.get(status, "❓ 不明")
        
        fields = [
            {
                "name": "ステータス",
                "value": status_text,
                "inline": True
            },
            {
                "name": "処理済みアカウント",
                "value": str(data.get("processed_accounts", 0)),
                "inline": True
            },
            {
                "name": "新規ツイート",
                "value": str(data.get("new_tweets", 0)),
                "inline": True
            }
        ]

        total_targets = data.get("total_targets", 0)
        completed_targets = data.get("completed_targets", 0)
        progress_percent = data.get("progress_percent", 0.0)
        if total_targets > 0:
            fields.append({
                "name": "進捗",
                "value": f"{progress_percent:.1f}% ({completed_targets}/{total_targets})",
                "inline": True
            })
        
        if data.get("event_tweets", 0) > 0:
            fields.append({
                "name": "イベントツイート",
                "value": str(data["event_tweets"]),
                "inline": True
            })
        
        if data.get("error_count", 0) > 0:
            fields.append({
                "name": "エラー数",
                "value": str(data["error_count"]),
                "inline": True
            })
        
        if data.get("last_check_time"):
            fields.append({
                "name": "最終チェック時刻",
                "value": data["last_check_time"],
                "inline": False
            })
        
        if data.get("current_account"):
            fields.append({
                "name": "処理中アカウント",
                "value": f"@{data['current_account']}",
                "inline": False
            })
        
        # Discord Crawler統計
        discord_servers = data.get("processed_discord_servers", 0)
        discord_channels = data.get("processed_discord_channels", 0)
        current_discord = data.get("current_discord_server")
        if discord_servers > 0 or discord_channels > 0 or current_discord:
            fields.append({
                "name": "Discord Crawler",
                "value": f"サーバー: {discord_servers} / チャンネル: {discord_channels}",
                "inline": True
            })
        if current_discord:
            fields.append({
                "name": "処理中Discordサーバー",
                "value": current_discord,
                "inline": True
            })
        
        return {
            "title": "🐦 EventMonitor Status",
            "color": self.STATUS_COLORS.get(status, 0x808080),
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {
                "text": "EventMonitor - Twitter Event Monitor"
            }
        }
    
    def update_status(self, status: str, data: Dict[str, Any], force: bool = False):
        """
        ステータスを更新
        
        Args:
            status: ステータス ("running", "idle", "error", "starting")
            data: 表示するデータ
            force: 強制更新（レート制限を無視）
        """
        if not self.enabled:
            return
        
        current_time = time.time()
        
        # 状態遷移時は即座に更新
        status_changed = status != self.last_status
        
        # レート制限チェック（強制更新または状態遷移時はスキップ）
        if not force and not status_changed:
            if current_time - self.last_update_time < self.min_update_interval:
                return
        
        if not self.dashboard_message_id:
            self._create_dashboard_message()
            if not self.dashboard_message_id:
                return
        
        try:
            embed = self._build_embed(status, data)
            
            url = f"{self.base_url}/channels/{self.dashboard_channel_id}/messages/{self.dashboard_message_id}"
            payload = {"embeds": [embed]}
            
            response = requests.patch(url, json=payload, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                self.last_update_time = current_time
                self.last_status = status
                self.logger.debug(f"ダッシュボード更新成功: status={status}")
            else:
                self.logger.warning(f"メッセージ更新失敗: {response.status_code}, {response.text}")
                # メッセージが削除された可能性があるため再作成を試みる
                if response.status_code == 404:
                    self.dashboard_message_id = None
                    self._create_dashboard_message()
                    
        except Exception as e:
            self.logger.error(f"ステータス更新エラー: {e}")
    
    def post_error_log(self, error_message: str, error_details: Optional[str] = None):
        """
        エラーログチャンネルにエラーを投稿
        
        Args:
            error_message: エラーメッセージ
            error_details: エラー詳細（スタックトレースなど）
        """
        if not self.enabled:
            return
        
        try:
            embed = {
                "title": "❌ EventMonitor エラー",
                "description": error_message,
                "color": 0xFF0000,
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {
                    "text": "EventMonitor Error Log"
                }
            }
            
            if error_details:
                # 詳細が長すぎる場合は切り詰める
                if len(error_details) > 1024:
                    error_details = error_details[:1021] + "..."
                embed["fields"] = [{
                    "name": "詳細",
                    "value": f"```\n{error_details}\n```",
                    "inline": False
                }]
            
            url = f"{self.base_url}/channels/{self.error_log_channel_id}/messages"
            payload = {"embeds": [embed]}
            
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                self.logger.info("エラーログ投稿成功")
            else:
                self.logger.warning(f"エラーログ投稿失敗: {response.status_code}, {response.text}")
                
        except Exception as e:
            self.logger.error(f"エラーログ投稿エラー: {e}")


class AoiTalkNotifier:
    """AoiTalkへのステータス通知を管理するクラス"""
    
    def __init__(
        self,
        api_url: str,
        api_key: str,
        enabled: bool = True
    ):
        """
        Args:
            api_url: AoiTalk API URL
            api_key: API認証キー
            enabled: 通知が有効かどうか
        """
        self.api_url = api_url
        self.api_key = api_key
        self.enabled = enabled
        
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        self.logger = logging.getLogger("EventMonitor.StatusNotifier.AoiTalk")
    
    def push_status(self, status: str, data: Dict[str, Any]):
        """
        ステータスをAoiTalkにPush
        
        Args:
            status: ステータス ("running", "idle", "error", "starting", "stopped")
            data: ステータスデータ
        """
        if not self.enabled:
            return
        
        try:
            payload = {
                "name": "EventMonitor",
                "status": status,
                "processed_accounts": data.get("processed_accounts", 0),
                "completed_accounts": data.get("completed_accounts", 0),
                "total_accounts": data.get("total_accounts", 0),
                "new_tweets": data.get("new_tweets", 0),
                "event_tweets": data.get("event_tweets", 0),
                "error_count": data.get("error_count", 0),
                "processed_discord_servers": data.get("processed_discord_servers", 0),
                "completed_discord_servers": data.get("completed_discord_servers", 0),
                "total_discord_servers": data.get("total_discord_servers", 0),
                "processed_discord_channels": data.get("processed_discord_channels", 0),
                "completed_targets": data.get("completed_targets", 0),
                "total_targets": data.get("total_targets", 0),
                "progress_percent": data.get("progress_percent", 0.0),
                "last_check_time": data.get("last_check_time", "")
            }
            
            # 追加情報があれば含める
            if "current_account" in data:
                payload["current_account"] = data["current_account"]
            
            response = requests.post(
                self.api_url,
                json=payload,
                headers=self.headers,
                timeout=10,
                verify=False  # 自己署名証明書のためSSL検証を無効化
            )
            
            if response.status_code == 200:
                self.logger.debug(f"ステータス送信成功: status={status}")
            else:
                self.logger.warning(f"ステータス送信失敗: {response.status_code}, {response.text}")
                
        except requests.exceptions.Timeout:
            self.logger.error("ステータス送信タイムアウト")
        except requests.exceptions.ConnectionError:
            self.logger.debug("AoiTalkへの接続に失敗しました（サーバー停止中の可能性）")
        except Exception as e:
            self.logger.error(f"ステータス送信エラー: {e}")


class StatusNotifier:
    """統合ステータス通知クラス（Discord + AoiTalk）"""
    
    def __init__(self, config: dict):
        """
        Args:
            config: 設定辞書
        """
        self.config = config
        self.logger = logging.getLogger("EventMonitor.StatusNotifier")
        
        self.discord_notifier: Optional[DiscordDashboardNotifier] = None
        self.aoitalk_notifier: Optional[AoiTalkNotifier] = None
        
        # 統計情報
        self.processed_accounts = 0
        self.completed_accounts = 0
        self.total_accounts = 0
        self.new_tweets = 0
        self.event_tweets = 0
        self.error_count = 0
        self.current_account: Optional[str] = None
        # Discord Crawler統計
        self.processed_discord_servers = 0
        self.completed_discord_servers = 0
        self.total_discord_servers = 0
        self.processed_discord_channels = 0
        self.current_discord_server: Optional[str] = None
        
        self._initialize_notifiers()
    
    def _initialize_notifiers(self):
        """通知機能を初期化"""
        notification_config = self.config.get('status_notification', {})
        
        # Discord通知の初期化
        discord_config = notification_config.get('discord', {})
        if discord_config.get('enabled', False):
            bot_token = os.getenv('DISCORD_BOT_TOKEN')
            dashboard_channel_id = os.getenv('DISCORD_DASHBOARD_CHANNEL_ID')
            error_log_channel_id = os.getenv('DISCORD_ERROR_LOG_CHANNEL_ID')
            
            if bot_token and dashboard_channel_id and error_log_channel_id:
                self.discord_notifier = DiscordDashboardNotifier(
                    bot_token=bot_token,
                    dashboard_channel_id=dashboard_channel_id,
                    error_log_channel_id=error_log_channel_id,
                    min_update_interval=discord_config.get('min_update_interval_seconds', 60)
                )
                self.logger.info("Discordステータス通知を有効化しました")
            else:
                missing = []
                if not bot_token:
                    missing.append("DISCORD_BOT_TOKEN")
                if not dashboard_channel_id:
                    missing.append("DISCORD_DASHBOARD_CHANNEL_ID")
                if not error_log_channel_id:
                    missing.append("DISCORD_ERROR_LOG_CHANNEL_ID")
                self.logger.warning(f"Discord環境変数が不足しているため、Discord通知は無効です: {', '.join(missing)}")
        
        # AoiTalk通知の初期化
        aoitalk_config = notification_config.get('aoitalk', {})
        if aoitalk_config.get('enabled', False):
            api_key = os.getenv('CRAWLER_API_KEY')
            api_url = os.getenv('AOITALK_API_URL', 'https://127.0.0.1:3000/api/crawler/report')
            
            if api_key:
                self.aoitalk_notifier = AoiTalkNotifier(
                    api_url=api_url,
                    api_key=api_key
                )
                self.logger.info("AoiTalkステータス通知を有効化しました")
            else:
                self.logger.warning("CRAWLER_API_KEYが設定されていないため、AoiTalk通知は無効です")
    
    def _get_status_data(self) -> Dict[str, Any]:
        """現在のステータスデータを取得"""
        total_targets = self.total_accounts + self.total_discord_servers
        completed_targets = self.completed_accounts + self.completed_discord_servers
        progress_percent = 0.0
        if total_targets > 0:
            progress_percent = round((completed_targets / total_targets) * 100, 1)

        return {
            "processed_accounts": self.processed_accounts,
            "completed_accounts": self.completed_accounts,
            "total_accounts": self.total_accounts,
            "new_tweets": self.new_tweets,
            "event_tweets": self.event_tweets,
            "error_count": self.error_count,
            "current_account": self.current_account,
            "processed_discord_servers": self.processed_discord_servers,
            "completed_discord_servers": self.completed_discord_servers,
            "total_discord_servers": self.total_discord_servers,
            "processed_discord_channels": self.processed_discord_channels,
            "current_discord_server": self.current_discord_server,
            "completed_targets": completed_targets,
            "total_targets": total_targets,
            "progress_percent": progress_percent,
            "last_check_time": datetime.now().isoformat()
        }
    
    def notify_starting(self):
        """開始ステータスを通知"""
        self._reset_stats()
        self._notify("starting", force=True)
    
    def notify_running(self, current_account: Optional[str] = None):
        """実行中ステータスを通知"""
        if current_account:
            self.current_account = current_account
        self._notify("running")
    
    def notify_idle(self):
        """待機中ステータスを通知（デーモンモードで次のサイクルを待機中）"""
        self.current_account = None
        self._notify("idle", force=True)
    
    def notify_stopped(self):
        """停止ステータスを通知（プロセスが正常終了）"""
        self.current_account = None
        self._notify("stopped", force=True)
    
    def notify_error(self, error_message: str, error_details: Optional[str] = None):
        """エラーステータスを通知"""
        self.error_count += 1
        self._notify("error", force=True)
        
        # エラーログを投稿
        if self.discord_notifier:
            self.discord_notifier.post_error_log(error_message, error_details)
    
    def increment_processed_accounts(self):
        """処理済みアカウント数をインクリメント"""
        self.processed_accounts += 1

    def increment_completed_accounts(self):
        """完了扱いのアカウント数をインクリメント（成功/スキップ/失敗を含む）"""
        self.completed_accounts += 1
    
    def add_new_tweets(self, count: int):
        """新規ツイート数を追加"""
        self.new_tweets += count
    
    def add_event_tweets(self, count: int):
        """イベントツイート数を追加"""
        self.event_tweets += count
    
    def increment_processed_discord_servers(self):
        """処理済みDiscordサーバー数をインクリメント"""
        self.processed_discord_servers += 1

    def increment_completed_discord_servers(self):
        """完了扱いのDiscordサーバー数をインクリメント（成功/スキップ/失敗を含む）"""
        self.completed_discord_servers += 1
    
    def add_discord_channels(self, count: int):
        """処理済みDiscordチャンネル数を追加"""
        self.processed_discord_channels += count

    def set_target_counts(self, total_accounts: int, total_discord_servers: int = 0):
        """今回のサイクルで処理対象となる件数を設定"""
        self.total_accounts = max(0, total_accounts)
        self.total_discord_servers = max(0, total_discord_servers)
    
    def _reset_stats(self):
        """統計情報をリセット"""
        self.processed_accounts = 0
        self.completed_accounts = 0
        self.total_accounts = 0
        self.new_tweets = 0
        self.event_tweets = 0
        self.error_count = 0
        self.current_account = None
        self.processed_discord_servers = 0
        self.completed_discord_servers = 0
        self.total_discord_servers = 0
        self.processed_discord_channels = 0
        self.current_discord_server = None
    
    def _notify(self, status: str, force: bool = False):
        """ステータスを通知"""
        data = self._get_status_data()
        
        if self.discord_notifier:
            try:
                self.discord_notifier.update_status(status, data, force=force)
            except Exception as e:
                self.logger.error(f"Discord通知中にエラー: {e}")
        
        if self.aoitalk_notifier:
            try:
                self.aoitalk_notifier.push_status(status, data)
            except Exception as e:
                self.logger.error(f"AoiTalk通知中にエラー: {e}")
