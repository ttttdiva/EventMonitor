import json
import logging
import os
import random
import string
import tempfile
import traceback as _traceback
from pathlib import Path
from typing import Generator

import httpx

_logger = logging.getLogger("EventMonitor.TwscrapeCompat")
_PATCHED = False


def apply_twscrape_compat_patches() -> None:
    """Apply local twscrape compatibility patches once."""
    global _PATCHED
    if _PATCHED:
        return

    _patch_xclid_parser()
    _patch_parse_tweets()
    _patch_account_client_timeout()
    _patch_transient_403_handling()
    _PATCHED = True


def _patch_xclid_parser() -> None:
    """x-client-transaction-id 生成の X 2026-06 レイアウト変更対応。

    2026-06 から x.com のホームページは新しい `/x-web/x-web/assets/*.js`
    構成になり、twscrape 0.18.1 までのパーサーが探す ondemand.s スクリプト
    参照がページから消えた (vladkens/twscrape#312)。
    ondemand.s 自体は旧URLで配信され続けているため、本家パーサーが失敗した
    場合だけ既知の ondemand.s URL へフォールバックする。
    twscrape 側が修正されたら本家ロジックが先に成功し、このパッチは素通りになる。
    """
    from twscrape import xclid

    if getattr(xclid.parse_anim_idx, "_eventmonitor_patched", False):
        return

    original_parse_anim_idx = xclid.parse_anim_idx
    fallback_url = os.getenv(
        "EVENTMONITOR_XCLID_ONDEMAND_URL",
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.c86191da.js",
    )

    async def _parse_anim_idx_with_fallback(text: str, clt: httpx.AsyncClient) -> list[int]:
        try:
            return await original_parse_anim_idx(text, clt)
        except Exception as e:
            _logger.warning(
                "twscrape parse_anim_idx failed (%s: %s); "
                "falling back to pinned ondemand.s script",
                type(e).__name__,
                e,
            )

        js_text = await xclid.get_tw_page_text(fallback_url, clt)
        items = [int(x.group(2)) for x in xclid.INDICES_REGEX.finditer(js_text)]
        if not items:
            raise Exception("Couldn't get XClientTxId indices (pinned ondemand.s fallback)")
        return items

    _parse_anim_idx_with_fallback._eventmonitor_patched = True
    xclid.parse_anim_idx = _parse_anim_idx_with_fallback


def _write_dump_win(kind: str, e: Exception, x: dict, obj: dict) -> None:
    import time as _time

    uniq = "".join(random.choice(string.ascii_lowercase) for _ in range(5))
    ts = _time.strftime("%Y-%m-%d_%H-%M-%S")
    dump_dir = Path(tempfile.gettempdir()) / "twscrape"
    dump_dir.mkdir(exist_ok=True)
    dumpfile = dump_dir / f"twscrape_parse_error_{ts}_{uniq}.txt"
    try:
        with open(dumpfile, "w", encoding="utf-8") as fp:
            fp.write("\n\n".join([
                f"Error parsing {kind}. Error: {type(e).__name__}: {e}",
                _traceback.format_exc(),
                json.dumps(x, default=str),
                json.dumps(obj, default=str),
            ]))
        _logger.error(f"Failed to parse response of {kind}, dump: {dumpfile}")
    except Exception:
        _logger.error(f"Failed to parse response of {kind}: {type(e).__name__}: {e}")


def parse_tweets_unlimited(rep: httpx.Response, limit: int = -1) -> Generator:
    from collections import defaultdict

    from twscrape.models import Tweet as TweetModel, to_old_rep
    from twscrape.utils import get_typed_object

    res = rep if isinstance(rep, dict) else rep.json()
    obj = to_old_rep(res)

    def _extract_and_add_user(tweet_raw: dict, users_dict: dict) -> None:
        user_paths = [
            ("core", "user_results", "result"),
            ("tweet", "core", "user_results", "result"),
        ]
        for path in user_paths:
            user_data = tweet_raw
            try:
                for key in path:
                    user_data = user_data[key]
                if isinstance(user_data, dict) and "rest_id" in user_data:
                    user_id_str = str(user_data["rest_id"])
                    if user_id_str in users_dict:
                        continue
                    if "legacy" in user_data:
                        users_dict[user_id_str] = {
                            **user_data,
                            **user_data["legacy"],
                            "id_str": user_id_str,
                            "id": int(user_id_str),
                            "legacy": None,
                        }
            except (KeyError, TypeError):
                continue

    typed_objects = get_typed_object(res, defaultdict(list))
    ids = set()
    for x in obj["tweets"].values():
        try:
            user_id_str = x.get("user_id_str")
            if user_id_str and user_id_str not in obj["users"]:
                for tweet_raw in typed_objects.get("Tweet", []):
                    _extract_and_add_user(tweet_raw, obj["users"])
                for tweet_wrapper in typed_objects.get("TweetWithVisibilityResults", []):
                    if "tweet" in tweet_wrapper:
                        _extract_and_add_user(tweet_wrapper["tweet"], obj["users"])

            if user_id_str and user_id_str not in obj["users"]:
                _logger.debug(
                    "twscrape: Skipping tweet %s - user %s not found in response",
                    x.get("id_str"),
                    user_id_str,
                )
                continue

            tmp = TweetModel.parse(x, obj)
            if tmp.id not in ids:
                ids.add(tmp.id)
                yield tmp
        except Exception as e:
            _logger.warning(
                "twscrape: Tweet parse error (id=%s, user=%s): %s: %s",
                x.get("id_str"),
                x.get("user_id_str"),
                type(e).__name__,
                e,
            )
            _write_dump_win("tweet", e, x, obj)


def _patch_parse_tweets() -> None:
    import twscrape.api as twscrape_api
    import twscrape.models

    twscrape.models.parse_tweets = parse_tweets_unlimited
    twscrape_api.parse_tweets = parse_tweets_unlimited


def _patch_account_client_timeout() -> None:
    """Give twscrape's per-account HTTP client an explicit timeout.

    httpx defaults to 5s, which is too short for slow X GraphQL responses.
    Wrap the original make_client so proxy resolution and header setup stay
    in sync with the installed twscrape version.
    """
    import twscrape.account
    from httpx import AsyncClient

    if getattr(twscrape.account.Account.make_client, "_eventmonitor_patched", False):
        return

    original_make_client = twscrape.account.Account.make_client

    def make_client_with_timeout(self, proxy: str | None = None) -> AsyncClient:
        client = original_make_client(self, proxy=proxy)
        client.timeout = httpx.Timeout(180.0, connect=30.0)
        return client

    make_client_with_timeout._eventmonitor_patched = True
    twscrape.account.Account.make_client = make_client_with_timeout


def _patch_transient_403_handling() -> None:
    """Keep ambiguous 403 responses from permanently disabling cookie accounts.

    twscrape (0.17.0-0.18.1) treats a 403 response with no GraphQL error body as
    "session expired or banned" and marks the account inactive. X sometimes
    returns this shape for endpoint-level throttling, while the same Cookie can
    still work through gallery-dl. Treat only this ambiguous case as a temporary
    queue lock; explicit auth/ban errors still use twscrape's original logic.
    """
    import twscrape.queue_client as queue_client
    from twscrape.utils import utc

    if getattr(queue_client.QueueClient._check_rep, "_eventmonitor_patched", False):
        return

    original_check_rep = queue_client.QueueClient._check_rep

    async def _check_rep_with_transient_403(self, rep: httpx.Response) -> None:
        if self.debug:
            queue_client.dump_rep(rep)

        try:
            res = rep.json()
        except json.JSONDecodeError:
            res = {"_raw": rep.text}

        err_msg = "OK"
        if isinstance(res, dict) and "errors" in res:
            err_msg = set([f"({x.get('code', -1)}) {x['message']}" for x in res["errors"]])
            err_msg = "; ".join(list(err_msg))

        if err_msg == "OK" and rep.status_code == 403:
            limit_reset = int(rep.headers.get("x-rate-limit-reset", -1))
            reset_at = limit_reset if limit_reset > 0 else utc.ts() + 60 * 60
            queue_client.logger.warning(
                f"Ambiguous 403 from X on queue {self.queue}; temporarily locking account "
                f"instead of marking inactive: {rep.status_code:3d} - "
                f"{queue_client.req_id(rep)} - {err_msg}"
            )
            await self._close_ctx(reset_at)
            raise queue_client.HandledError()

        return await original_check_rep(self, rep)

    _check_rep_with_transient_403._eventmonitor_patched = True
    queue_client.QueueClient._check_rep = _check_rep_with_transient_403
