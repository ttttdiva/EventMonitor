import os
import sys
from pathlib import Path

import yaml

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from event_detector import EventDetector  # noqa: E402


def load_project_config():
    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_event_keywords_cover_seion_application_posts():
    detector = EventDetector(load_project_config())

    cases = {
        "2059414111243649416": "此間も言ったけど、声音7次会申し込み済みです。絵は使い回し…… https://t.co/Dq3XnVxqm1",
        "2067096713484272120": "声音7次会確定みたいなので改めて出ます",
    }

    for tweet_id, text in cases.items():
        has_keywords, matched_keywords = detector._quick_keyword_check(text)

        assert has_keywords, tweet_id
        assert matched_keywords, tweet_id
