from pathlib import Path

import pytest

from src.csv_git_sync import CsvGitSyncer


class DummyCsvGitSyncer(CsvGitSyncer):
    def __init__(self, config, repo_root: Path, responses):
        super().__init__(config, repo_root=repo_root)
        self.responses = list(responses)
        self.commands = []

    async def _git(self, args):
        self.commands.append(args)
        if self.responses:
            return self.responses.pop(0)
        return True, "", ""


def _config(tmp_path):
    return {
        "system": {"data_dir": str(tmp_path / "data")},
        "csv_git_sync": {
            "enabled": True,
            "paths": ["monitored_accounts.csv", "deleted_accounts.csv"],
            "remote": "origin",
            "command_timeout_seconds": 5,
            "lock_stale_seconds": 60,
        },
    }


@pytest.mark.asyncio
async def test_csv_git_sync_skips_when_no_target_changes(tmp_path):
    syncer = DummyCsvGitSyncer(
        _config(tmp_path),
        tmp_path,
        [
            (True, "true", ""),
            (True, "", ""),
        ],
    )
    (tmp_path / "monitored_accounts.csv").write_text("username\n", encoding="utf-8")

    result = await syncer.sync("startup")

    assert result is False
    assert syncer.commands == [
        ["rev-parse", "--is-inside-work-tree"],
        ["status", "--porcelain", "--", "monitored_accounts.csv", "deleted_accounts.csv"],
    ]
    assert not syncer.lock_path.exists()


@pytest.mark.asyncio
async def test_csv_git_sync_commits_rebases_and_pushes_target_csvs(tmp_path):
    syncer = DummyCsvGitSyncer(
        _config(tmp_path),
        tmp_path,
        [
            (True, "true", ""),
            (True, " M monitored_accounts.csv", ""),
            (True, "main", ""),
            (True, "", ""),
            (True, "[main abc] sync", ""),
            (True, "", ""),
            (True, "", ""),
        ],
    )
    (tmp_path / "monitored_accounts.csv").write_text("username\nalice\n", encoding="utf-8")
    (tmp_path / "deleted_accounts.csv").write_text("username\n", encoding="utf-8")

    result = await syncer.sync("startup")

    assert result is True
    assert syncer.commands == [
        ["rev-parse", "--is-inside-work-tree"],
        ["status", "--porcelain", "--", "monitored_accounts.csv", "deleted_accounts.csv"],
        ["branch", "--show-current"],
        ["add", "--", "monitored_accounts.csv", "deleted_accounts.csv"],
        [
            "commit",
            "--only",
            "-m",
            "CSVアカウント一覧を同期 (startup)",
            "--",
            "monitored_accounts.csv",
            "deleted_accounts.csv",
        ],
        ["pull", "--rebase", "--autostash", "origin", "main"],
        ["push", "origin", "main"],
    ]
    assert not syncer.lock_path.exists()


@pytest.mark.asyncio
async def test_csv_git_sync_skips_when_lock_is_active(tmp_path):
    syncer = DummyCsvGitSyncer(_config(tmp_path), tmp_path, [])
    syncer.lock_path.parent.mkdir(parents=True)
    syncer.lock_path.write_text("999,9999999999\n", encoding="utf-8")

    result = await syncer.sync("startup")

    assert result is False
    assert syncer.commands == []
    assert syncer.lock_path.exists()
