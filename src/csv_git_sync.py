import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class CsvGitSyncer:
    """Commit and push selected CSV files without touching unrelated worktree changes."""

    def __init__(self, config: Dict, repo_root: Optional[Path] = None) -> None:
        sync_config = config.get("csv_git_sync", {})
        system_config = config.get("system", {})

        self.enabled = bool(sync_config.get("enabled", False))
        self.paths = list(sync_config.get(
            "paths",
            ["monitored_accounts.csv", "deleted_accounts.csv"],
        ))
        self.remote = sync_config.get("remote", "origin")
        self.commit_message_prefix = sync_config.get(
            "commit_message_prefix",
            "CSVアカウント一覧を同期",
        )
        self.command_timeout = int(sync_config.get("command_timeout_seconds", 120))
        self.lock_stale_seconds = int(sync_config.get("lock_stale_seconds", 1800))
        self.repo_root = (repo_root or Path.cwd()).resolve()
        data_dir = Path(system_config.get("data_dir", "data"))
        if not data_dir.is_absolute():
            data_dir = self.repo_root / data_dir
        self.lock_path = data_dir / "csv_git_sync.lock"
        self.logger = logging.getLogger("EventMonitor.CsvGitSync")

    async def sync(self, reason: str) -> bool:
        if not self.enabled:
            self.logger.debug("CSV git sync is disabled")
            return False

        if not self._acquire_lock():
            self.logger.info("CSV git sync skipped because another sync is running")
            return False

        try:
            if not await self._is_git_repository():
                self.logger.warning("CSV git sync skipped because current directory is not a git repository")
                return False

            existing_paths = [path for path in self.paths if (self.repo_root / path).exists()]
            if not existing_paths:
                self.logger.warning("CSV git sync skipped because target CSV files do not exist")
                return False

            if not await self._has_target_changes():
                self.logger.info("CSV git sync skipped because target CSV files have no changes")
                return False

            branch = await self._current_branch()
            if not branch:
                self.logger.warning("CSV git sync skipped because current branch could not be detected")
                return False

            self.logger.info("Starting CSV git sync (%s): %s", reason, ", ".join(self.paths))

            ok, _, stderr = await self._git(["add", "--", *self.paths])
            if not ok:
                self.logger.error("CSV git sync failed during git add: %s", stderr)
                return False

            message = f"{self.commit_message_prefix} ({reason})"
            ok, stdout, stderr = await self._git(["commit", "--only", "-m", message, "--", *self.paths])
            if not ok:
                combined = f"{stdout}\n{stderr}".lower()
                if "nothing to commit" in combined or "no changes added" in combined:
                    self.logger.info("CSV git sync skipped because there was nothing to commit")
                    return False
                self.logger.error("CSV git sync failed during git commit: %s", stderr or stdout)
                return False

            ok, _, stderr = await self._git(["pull", "--rebase", "--autostash", self.remote, branch])
            if not ok:
                self.logger.error("CSV git sync failed during git pull --rebase: %s", stderr)
                return False

            ok, _, stderr = await self._git(["push", self.remote, branch])
            if not ok:
                self.logger.error("CSV git sync failed during git push: %s", stderr)
                return False

            self.logger.info("CSV git sync completed successfully")
            return True
        except Exception as exc:
            self.logger.error("CSV git sync failed unexpectedly: %s", exc, exc_info=True)
            return False
        finally:
            self._release_lock()

    async def _is_git_repository(self) -> bool:
        ok, stdout, _ = await self._git(["rev-parse", "--is-inside-work-tree"])
        return ok and stdout.strip() == "true"

    async def _current_branch(self) -> Optional[str]:
        ok, stdout, _ = await self._git(["branch", "--show-current"])
        branch = stdout.strip()
        return branch if ok and branch else None

    async def _has_target_changes(self) -> bool:
        ok, stdout, stderr = await self._git(["status", "--porcelain", "--", *self.paths])
        if not ok:
            self.logger.error("CSV git sync failed during git status: %s", stderr)
            return False
        return bool(stdout.strip())

    async def _git(self, args: List[str]) -> Tuple[bool, str, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False, "", f"timeout after {self.command_timeout}s"

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        return process.returncode == 0, stdout, stderr

    def _acquire_lock(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{os.getpid()},{time.time()}\n"

        for _ in range(2):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                return True
            except FileExistsError:
                if not self._remove_stale_lock():
                    return False
        return False

    def _remove_stale_lock(self) -> bool:
        try:
            content = self.lock_path.read_text(encoding="utf-8").strip()
            _, timestamp_text = content.split(",", 1)
            locked_at = float(timestamp_text)
        except Exception:
            locked_at = 0.0

        if time.time() - locked_at <= self.lock_stale_seconds:
            return False

        try:
            self.lock_path.unlink()
            self.logger.warning("Removed stale CSV git sync lock: %s", self.lock_path)
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            self.logger.warning("Failed to remove stale CSV git sync lock: %s", exc)
            return False

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.logger.warning("Failed to release CSV git sync lock: %s", exc)
