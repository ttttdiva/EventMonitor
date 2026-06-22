import time

import httpx
import pytest

from src.twscrape_compat import apply_twscrape_compat_patches


@pytest.mark.asyncio
async def test_ambiguous_403_temporarily_locks_instead_of_marking_inactive():
    apply_twscrape_compat_patches()

    import twscrape.queue_client as queue_client

    class DummyQueueClient:
        debug = False
        queue = "UserTweets"

        def __init__(self):
            self.closed = None

        async def _close_ctx(self, reset_at=-1, inactive=False, msg=None):
            self.closed = {
                "reset_at": reset_at,
                "inactive": inactive,
                "msg": msg,
            }

    client = DummyQueueClient()
    request = httpx.Request("GET", "https://x.com/i/api/graphql/test/UserTweets")
    response = httpx.Response(403, json={}, request=request)
    setattr(response, "__username", "cookie_user_01")

    with pytest.raises(queue_client.HandledError):
        await queue_client.QueueClient._check_rep(client, response)

    assert client.closed["inactive"] is False
    assert client.closed["msg"] is None
    assert client.closed["reset_at"] >= int(time.time()) + 55 * 60
