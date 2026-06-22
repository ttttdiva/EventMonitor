import json
import time
from pathlib import Path

import yaml

from scripts.maintenance import scheduled_backup as scheduled_backup_module


class DummyApi:
    def upload_large_folder(self, **_kwargs):
        return None


def _write_config(tmp_path: Path, images_path: Path, videos_path: Path, db_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config = {
        "huggingface_backup": {
            "repo_name": "dummy/repo",
        },
        "media_storage": {
            "images_path": str(images_path),
            "videos_path": str(videos_path),
        },
        "database": {
            "path": str(db_path),
        },
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def test_crawler_media_dry_run_writes_progress_summary(tmp_path, monkeypatch):
    images_dir = tmp_path / "media" / "images" / "user_a"
    videos_dir = tmp_path / "media" / "videos" / "user_b"
    images_dir.mkdir(parents=True)
    videos_dir.mkdir(parents=True)
    (images_dir / "sample.jpg").write_bytes(b"img")
    (videos_dir / "sample.mp4").write_bytes(b"vid")

    config_path = _write_config(
        tmp_path,
        images_dir.parent,
        videos_dir.parent,
        tmp_path / "eventmonitor.db",
    )
    progress_path = tmp_path / "backup_progress.json"

    monkeypatch.setenv("HUGGINGFACE_API_KEY", "dummy-token")
    monkeypatch.setattr(scheduled_backup_module, "HfApi", lambda token: DummyApi())
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "PROGRESS_FILE", progress_path)
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "MANIFEST_FILE", tmp_path / "backup_crawler_manifest.txt")

    backup = scheduled_backup_module.ScheduledBackup(config_path=str(config_path), dry_run=True)
    backup.run(targets=["crawler_media"])

    progress = json.loads(progress_path.read_text(encoding="utf-8"))

    assert progress["active_runs"] == {}
    assert progress["crawler_media"]["status"] == "dry_run"
    assert progress["crawler_media"]["phase"] == "completed"
    assert progress["crawler_media"]["total_files"] == 2
    assert progress["recent_runs"][0]["target_states"]["crawler_media"]["status"] == "dry_run"


def test_crawler_media_updates_progress_during_upload(tmp_path, monkeypatch):
    images_dir = tmp_path / "media" / "images" / "user_a"
    videos_dir = tmp_path / "media" / "videos" / "user_b"
    images_dir.mkdir(parents=True)
    videos_dir.mkdir(parents=True)
    (images_dir / "sample.jpg").write_bytes(b"img")
    (videos_dir / "sample.mp4").write_bytes(b"vid")

    config_path = _write_config(
        tmp_path,
        images_dir.parent,
        videos_dir.parent,
        tmp_path / "eventmonitor.db",
    )
    progress_path = tmp_path / "backup_progress.json"
    observed = {}

    monkeypatch.setenv("HUGGINGFACE_API_KEY", "dummy-token")
    monkeypatch.setattr(scheduled_backup_module, "HfApi", lambda token: DummyApi())
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "PROGRESS_FILE", progress_path)
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "MANIFEST_FILE", tmp_path / "backup_crawler_manifest.txt")
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "INDIVIDUAL_UPLOAD_THRESHOLD", 0)
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "_ensure_repo_exists", lambda self: None)

    backup = scheduled_backup_module.ScheduledBackup(config_path=str(config_path), dry_run=False)

    class HeartbeatApi:
        def upload_large_folder(self, **_kwargs):
            time.sleep(0.05)
            first = json.loads(progress_path.read_text(encoding="utf-8"))
            first_state = first["active_runs"][backup.run_id]["target_states"]["crawler_media"]
            observed["first_updated"] = first_state["last_updated"]
            observed["phase"] = first_state["phase"]
            observed["status"] = first_state["status"]

            time.sleep(0.06)
            second = json.loads(progress_path.read_text(encoding="utf-8"))
            second_state = second["active_runs"][backup.run_id]["target_states"]["crawler_media"]
            observed["second_updated"] = second_state["last_updated"]

    backup.api = HeartbeatApi()
    backup.run(targets=["crawler_media"])

    assert observed["phase"] == "phase_1_upload"
    assert observed["status"] == "running"
    assert observed["first_updated"] != observed["second_updated"]


def test_crawler_media_missing_base_marks_progress_failed(tmp_path, monkeypatch):
    missing_images = tmp_path / "missing" / "images"
    missing_videos = tmp_path / "missing" / "videos"
    config_path = _write_config(
        tmp_path,
        missing_images,
        missing_videos,
        tmp_path / "eventmonitor.db",
    )
    progress_path = tmp_path / "backup_progress.json"

    monkeypatch.setenv("HUGGINGFACE_API_KEY", "dummy-token")
    monkeypatch.setattr(scheduled_backup_module, "HfApi", lambda token: DummyApi())
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "PROGRESS_FILE", progress_path)
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "MANIFEST_FILE", tmp_path / "backup_crawler_manifest.txt")

    backup = scheduled_backup_module.ScheduledBackup(config_path=str(config_path), dry_run=True)
    backup.run(targets=["crawler_media"])

    progress = json.loads(progress_path.read_text(encoding="utf-8"))

    assert progress["crawler_media"]["status"] == "failed"
    assert "media base not found" in progress["crawler_media"]["error"]


def test_eventmonitor_db_uses_latest_path_only(tmp_path, monkeypatch):
    db_path = tmp_path / "eventmonitor.db"
    db_path.write_bytes(b"sqlite-data")
    config_path = _write_config(
        tmp_path,
        tmp_path / "media" / "images",
        tmp_path / "media" / "videos",
        db_path,
    )

    uploaded_paths = []

    monkeypatch.setenv("HUGGINGFACE_API_KEY", "dummy-token")
    monkeypatch.setattr(scheduled_backup_module, "HfApi", lambda token: DummyApi())
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "_ensure_repo_exists", lambda self: None)

    backup = scheduled_backup_module.ScheduledBackup(config_path=str(config_path), dry_run=False)
    monkeypatch.setattr(
        backup,
        "_upload_file_with_retry",
        lambda path_or_fileobj, path_in_repo: uploaded_paths.append(path_in_repo) or True,
    )

    backup.backup_eventmonitor_db()

    assert uploaded_paths == ["backup/eventmonitor_db/eventmonitor_latest.db"]


def test_hydrus_db_uses_latest_paths_only(tmp_path, monkeypatch):
    hydrus_dir = tmp_path / "hydrus"
    hydrus_dir.mkdir()
    for name in ["client.db", "client.mappings.db", "client.master.db"]:
        (hydrus_dir / name).write_bytes(b"hydrus-db")

    config_path = _write_config(
        tmp_path,
        tmp_path / "media" / "images",
        tmp_path / "media" / "videos",
        tmp_path / "eventmonitor.db",
    )

    uploaded_paths = []

    monkeypatch.setenv("HUGGINGFACE_API_KEY", "dummy-token")
    monkeypatch.setattr(scheduled_backup_module, "HfApi", lambda token: DummyApi())
    monkeypatch.setattr(scheduled_backup_module.ScheduledBackup, "_ensure_repo_exists", lambda self: None)

    backup = scheduled_backup_module.ScheduledBackup(config_path=str(config_path), dry_run=False)
    backup.hydrus_db_dir = hydrus_dir
    monkeypatch.setattr(
        backup,
        "_upload_file_with_retry",
        lambda path_or_fileobj, path_in_repo: uploaded_paths.append(path_in_repo) or True,
    )

    backup.backup_hydrus_db()

    assert uploaded_paths == [
        "backup/hydrus_db/client.db",
        "backup/hydrus_db/client.mappings.db",
        "backup/hydrus_db/client.master.db",
    ]
