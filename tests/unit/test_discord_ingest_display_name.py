import csv

import pytest

from src.fanbox_extractor import FanboxExtractor
from src.services.discord_account_ingest import DiscordAccountIngestor


class DummyTwitterMonitor:
    async def resolve_display_name(self, username: str) -> str:
        return f"twitter-{username}"


def make_config(tmp_path):
    return {
        "discord_ingest": {
            "enabled": True,
            "guild_id": "1",
            "channel_id": "2",
            "display_name_timeout_seconds": 5,
        },
        "media": {
            "save_dir": str(tmp_path / "media"),
        },
    }


@pytest.mark.asyncio
async def test_discord_ingest_resolves_platform_display_name_before_csv_append(
    tmp_path,
    monkeypatch,
):
    csv_path = tmp_path / "accounts.csv"
    resolver_calls = []
    deleted_messages = []

    def resolve_fanbox_display_name(username: str) -> str:
        resolver_calls.append(username)
        return "Artist, Name"

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    ingestor = DiscordAccountIngestor(
        make_config(tmp_path),
        DummyTwitterMonitor(),
        csv_path=str(csv_path),
        display_name_resolvers={"fanbox": resolve_fanbox_display_name},
    )

    async def fake_fetch_all_messages(_session):
        return [{"id": "msg-1", "content": "https://www.fanbox.cc/@we53\n1"}]

    async def fake_delete_message(_session, message_id):
        deleted_messages.append(message_id)

    monkeypatch.setattr(ingestor, "_fetch_all_messages", fake_fetch_all_messages)
    monkeypatch.setattr(ingestor, "_delete_message", fake_delete_message)

    added = await ingestor.ingest_new_accounts()

    assert resolver_calls == ["we53"]
    assert deleted_messages == ["msg-1"]
    assert added == [
        {
            "username": "we53",
            "display_name": "Artist Name",
            "notification": "",
            "account_type": "",
            "platform": "fanbox",
            "rank": "1",
        }
    ]

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["username"] == "we53"
    assert rows[0]["display_name"] == "Artist Name"
    assert rows[0]["platform"] == "fanbox"
    assert rows[0]["rank"] == "1"


def test_fanbox_resolve_display_name_uses_list_api(monkeypatch, tmp_path):
    extractor = FanboxExtractor(
        {
            "media": {"save_dir": str(tmp_path / "media")},
            "fanbox": {},
        }
    )

    class DummyResponse:
        status_code = 200

        def json(self):
            return {
                "body": [
                    {
                        "creatorId": "we53",
                        "user": {"name": "FANBOX Artist"},
                    }
                ]
            }

    def fake_get(url, headers, cookies, timeout):
        assert url.endswith("creatorId=we53&limit=1")
        assert timeout == 15
        return DummyResponse()

    monkeypatch.setattr(extractor, "_load_fanbox_cookies", lambda: object())
    monkeypatch.setattr("src.fanbox_extractor.requests.get", fake_get)
    monkeypatch.setattr(
        extractor,
        "fetch_user_works",
        lambda *_args, **_kwargs: pytest.fail("gallery-dl path should not be used"),
    )

    assert extractor.resolve_display_name("we53") == "FANBOX Artist"
