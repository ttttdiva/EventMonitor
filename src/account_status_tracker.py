"""
アカウント到達性フラグ管理

到達不能と判定されたアカウントを data/flagged_accounts.json で管理し、
30日間の猶予期間後に deleted_accounts.csv へ自動アーカイブする。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


class AccountStatusTracker:

    DEFAULT_PATH = "data/flagged_accounts.json"
    DEFAULT_EXPIRY_DAYS = 30

    def __init__(
        self,
        path: str = DEFAULT_PATH,
        expiry_days: int = DEFAULT_EXPIRY_DAYS,
    ) -> None:
        self.path = Path(path)
        self.expiry_days = expiry_days
        self.logger = logging.getLogger("EventMonitor.AccountStatusTracker")
        self._data: Dict[str, Any] = {"version": 1, "flagged": {}}
        self._load()

    # ---- Persistence ----

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self._data = json.load(f)
                count = len(self._data.get("flagged", {}))
                if count:
                    self.logger.info(f"Loaded {count} flagged account(s)")
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load flagged accounts: {e}")
                self._data = {"version": 1, "flagged": {}}
        else:
            self._data = {"version": 1, "flagged": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.logger.error(f"Failed to save flagged accounts: {e}")

    # ---- Core API ----

    def is_flagged(self, username: str) -> bool:
        """フラグ済みか判定"""
        return username in self._data.get("flagged", {})

    def flag_account(
        self,
        username: str,
        platform: str,
        account_type: str = "",
        display_name: str = "",
        error_msg: str = "",
    ) -> None:
        """アカウントを即時フラグ。既にフラグ済みの場合は last_checked を更新。"""
        now = datetime.now().isoformat()
        entry = self._data["flagged"].get(username)

        if entry is None:
            entry = {
                "platform": platform,
                "account_type": account_type,
                "display_name": display_name,
                "first_flagged": now,
                "last_checked": now,
                "last_error": error_msg,
            }
            self._data["flagged"][username] = entry
            self.logger.warning(
                f"Account {username} ({platform}) flagged as unreachable: {error_msg}"
            )
        else:
            entry["last_checked"] = now
            entry["last_error"] = error_msg

    def record_recovery(self, username: str) -> None:
        """アカウント復活 — フラグ解除"""
        if username in self._data["flagged"]:
            entry = self._data["flagged"].pop(username)
            self.logger.info(
                f"Account {username} recovered "
                f"(was flagged since {entry.get('first_flagged', 'N/A')})"
            )

    def get_expired_accounts(self) -> List[Dict[str, Any]]:
        """expiry_days を超過したフラグ済みアカウントを返す"""
        expired: List[Dict[str, Any]] = []
        cutoff = datetime.now() - timedelta(days=self.expiry_days)

        for username, entry in self._data.get("flagged", {}).items():
            first_flagged = entry.get("first_flagged")
            if not first_flagged:
                continue
            try:
                flagged_dt = datetime.fromisoformat(first_flagged)
            except (ValueError, TypeError):
                continue
            if flagged_dt < cutoff:
                expired.append({"username": username, **entry})

        return expired

    def remove_account(self, username: str) -> None:
        """アーカイブ後にエントリを削除"""
        self._data["flagged"].pop(username, None)

    def get_all_flagged(self) -> Dict[str, Any]:
        """デバッグ・ログ用: 全フラグ済みエントリ"""
        return dict(self._data.get("flagged", {}))
