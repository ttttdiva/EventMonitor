from pathlib import Path

from src.misskey_extractor import MisskeyExtractor
from src.services.discord_account_ingest import DiscordAccountIngestor


class DummyTwitterMonitor:
    async def resolve_display_name(self, username: str) -> str:
        return username


def make_misskey_config(tmp_path: Path):
    return {
        "media": {
            "save_dir": str(tmp_path / "media"),
        },
        "misskey": {
            "enabled": True,
            "default_instance": "misskey.io",
            "known_hosts": ["voskey.icalo.net"],
        },
    }


def test_discord_ingest_extracts_voskey_profile_url(tmp_path):
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    result = ingestor._extract_username_from_url("https://voskey.icalo.net/@Torimeat")

    assert result == ("Torimeat@voskey.icalo.net", "misskey")


def test_discord_ingest_keeps_misskey_io_backward_compatible(tmp_path):
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    result = ingestor._extract_username_from_url("https://misskey.io/@kashiwatoriniku")

    assert result == ("kashiwatoriniku", "misskey")


def test_extract_work_info_uses_instance_aware_ids(tmp_path):
    extractor = MisskeyExtractor(make_misskey_config(tmp_path))

    work = extractor._extract_work_info(
        {
            "id": "abc123",
            "createdAt": "2026-03-09T00:00:00Z",
            "text": "hello",
            "user": {"name": "Torimeat"},
        },
        "voskey.icalo.net",
    )

    assert work["id"] == "voskey.icalo.net:abc123"
    assert work["note_id"] == "abc123"
    assert work["instance_host"] == "voskey.icalo.net"
    assert work["url"] == "https://voskey.icalo.net/notes/abc123"


def test_collect_downloaded_files_maps_back_to_instance_aware_ids(tmp_path):
    extractor = MisskeyExtractor(make_misskey_config(tmp_path))
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "abc123_1.jpg").write_text("x", encoding="utf-8")

    collected = extractor._collect_downloaded_files(
        {"voskey.icalo.net:abc123": "abc123"},
        output_dir,
        set(),
    )

    assert list(collected) == ["voskey.icalo.net:abc123"]
    assert collected["voskey.icalo.net:abc123"][0].name == "abc123_1.jpg"


# --- Pixiv artwork URL テスト ---


def test_discord_ingest_extracts_pixiv_artwork_url(tmp_path):
    """Pixiv artwork URLから artworks:{id} 形式の仮ユーザー名を抽出できること"""
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    result = ingestor._extract_username_from_url("https://www.pixiv.net/artworks/142284673")
    assert result == ("artworks:142284673", "pixiv")


def test_discord_ingest_extracts_pixiv_user_url_still_works(tmp_path):
    """既存の /users/ パターンが壊れないこと（回帰テスト）"""
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    result = ingestor._extract_username_from_url("https://www.pixiv.net/users/12345")
    assert result == ("12345", "pixiv")


def test_discord_ingest_pixiv_artwork_with_noise_text(tmp_path):
    """ゴミテキスト付きメッセージからもPixiv artwork URLを正しく抽出できること"""
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    content = (
        "FGO\u3000絆レベル15嫁ハベにゃん\u3000いちゃいちゃ体格差魔力供給編 | きつね（仮） "
        "#pixiv https://www.pixiv.net/artworks/142284673"
    )
    entries = ingestor._parse_accounts_from_content(content)
    assert len(entries) == 1
    username, _, _, platform, _, _ = entries[0]
    assert platform == "pixiv"
    assert username == "artworks:142284673"


def test_discord_ingest_pixiv_artwork_en_artworks_path(tmp_path):
    """英語版 /en/artworks/ パスからも正しく抽出できること"""
    ingestor = DiscordAccountIngestor(
        make_misskey_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(tmp_path / "accounts.csv"),
    )

    result = ingestor._extract_username_from_url("https://www.pixiv.net/en/artworks/142284673")
    assert result == ("artworks:142284673", "pixiv")

