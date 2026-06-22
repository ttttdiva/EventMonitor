import hashlib
from types import SimpleNamespace
from pathlib import Path

import requests

from src.kemono_extractor import KemonoExtractor

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00"
    b"\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        status_code: int = 200,
        content_type: str = "image/jpeg",
    ):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} response",
                response=self,
            )
        return None

    def iter_content(self, chunk_size=1024 * 1024):
        yield self.content

    def close(self):
        return None


class FakeSession:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = []

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        if "n2.kemono.cr" in url:
            raise requests.ConnectTimeout("n2 timeout")
        return FakeResponse(self.content)


class PreviewFallbackSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None, stream=False, headers=None):
        self.calls.append(url)
        if "n2.kemono.cr" in url or "n3.kemono.cr" in url:
            raise requests.ConnectTimeout("storage timeout")
        if "img.kemono.cr" in url:
            return FakeResponse(PNG_BYTES, content_type="image/png")
        return FakeResponse(b"not found", status_code=404, content_type="text/html")


def test_kemono_cdn_fallback_tries_next_host_after_timeout(tmp_path):
    content = b"image-bytes"
    expected_hash = hashlib.sha256(content).hexdigest()
    extractor = KemonoExtractor({
        "media": {"save_dir": str(tmp_path / "media")},
        "kemono": {
            "cdn_fallback_hosts": ["n2.kemono.cr", "n3.kemono.cr"],
            "cdn_fallback_cooldown_seconds": 60,
        },
    })
    fake_session = FakeSession(content)
    extractor._cdn_session = fake_session

    paths = extractor._download_missing_media_via_cdn_fallback(
        ["fanbox_375579"],
        {"fanbox_375579": "375579"},
        {
            "fanbox_375579": {
                "https://kemono.cr/data/02/ac/"
                "02ac48900cbdc6daf701530de22968809330ed8af53c8f0b0d46d1f68a5bb75f.jpg": expected_hash
            }
        },
        {},
    )

    assert list(paths) == ["fanbox_375579"]
    downloaded = paths["fanbox_375579"][0]
    assert downloaded == tmp_path / "media" / "kemono" / "375579_01.jpg"
    assert downloaded.read_bytes() == content
    assert fake_session.calls == [
        "https://n2.kemono.cr/data/02/ac/"
        "02ac48900cbdc6daf701530de22968809330ed8af53c8f0b0d46d1f68a5bb75f.jpg",
        "https://n3.kemono.cr/data/02/ac/"
        "02ac48900cbdc6daf701530de22968809330ed8af53c8f0b0d46d1f68a5bb75f.jpg",
    ]


def test_kemono_cdn_fallback_skips_recently_failed_host(tmp_path):
    content = b"image-bytes"
    expected_hash = hashlib.sha256(content).hexdigest()
    extractor = KemonoExtractor({
        "media": {"save_dir": str(tmp_path / "media")},
        "kemono": {
            "cdn_fallback_hosts": ["n2.kemono.cr", "n3.kemono.cr"],
            "cdn_fallback_cooldown_seconds": 60,
        },
    })
    fake_session = FakeSession(content)
    extractor._cdn_session = fake_session

    payload = {
        "fanbox_1": {
            "https://kemono.cr/data/aa/bb/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg": expected_hash
        }
    }
    extractor._download_missing_media_via_cdn_fallback(
        ["fanbox_1"],
        {"fanbox_1": "1"},
        payload,
        {},
    )
    extractor._download_missing_media_via_cdn_fallback(
        ["fanbox_1"],
        {"fanbox_1": "1"},
        payload,
        {},
    )

    assert len([url for url in fake_session.calls if "n2.kemono.cr" in url]) == 1


def test_kemono_preview_fallback_runs_after_cdn_hosts_fail(tmp_path, monkeypatch):
    extractor = KemonoExtractor({
        "media": {"save_dir": str(tmp_path / "media")},
        "media_storage": {
            "images_path": str(tmp_path / "images"),
            "videos_path": str(tmp_path / "videos"),
        },
        "kemono": {
            "cdn_fallback_hosts": ["n2.kemono.cr", "n3.kemono.cr"],
            "preview_fallback_hosts": ["img.kemono.cr", "kemono.cr"],
            "cdn_fallback_cooldown_seconds": 60,
        },
    })
    fake_session = PreviewFallbackSession()
    extractor._cdn_session = fake_session

    monkeypatch.setattr(
        "src.kemono_extractor.run_with_idle_timeout",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="download failed"),
    )

    paths = extractor.download_media_for_works(
        "fanbox/1184461",
        ["fanbox_687044"],
        hash_map={
            "fanbox_687044": {
                "https://kemono.cr/data/b2/24/"
                "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f.jpg": (
                    "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f"
                )
            }
        },
    )

    assert list(paths) == ["fanbox_687044"]
    downloaded = tmp_path / paths["fanbox_687044"][0]
    assert downloaded.name == "687044_01_preview.png"
    assert downloaded.read_bytes() == PNG_BYTES
    assert extractor._cdn_outage_preview_only is True
    assert fake_session.calls == [
        "https://n2.kemono.cr/data/b2/24/"
        "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f.jpg",
        "https://n3.kemono.cr/data/b2/24/"
        "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f.jpg",
        "https://img.kemono.cr/thumbnail/data/b2/24/"
        "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f.jpg",
    ]


def test_kemono_cdn_outage_mode_skips_full_download_for_rest_of_cycle(
    tmp_path,
    monkeypatch,
):
    extractor = KemonoExtractor({
        "media": {"save_dir": str(tmp_path / "media")},
        "media_storage": {
            "images_path": str(tmp_path / "images"),
            "videos_path": str(tmp_path / "videos"),
        },
        "kemono": {
            "cdn_fallback_hosts": ["n2.kemono.cr", "n3.kemono.cr"],
            "preview_fallback_hosts": ["img.kemono.cr", "kemono.cr"],
            "cdn_fallback_cooldown_seconds": 60,
        },
    })
    fake_session = PreviewFallbackSession()
    extractor._cdn_session = fake_session

    gallery_calls = {"count": 0}

    def fake_gallery_dl(*args, **kwargs):
        gallery_calls["count"] += 1
        return SimpleNamespace(returncode=1, stderr="download failed")

    monkeypatch.setattr(
        "src.kemono_extractor.run_with_idle_timeout",
        fake_gallery_dl,
    )

    extractor.download_media_for_works(
        "fanbox/1184461",
        ["fanbox_687044"],
        hash_map={
            "fanbox_687044": {
                "https://kemono.cr/data/b2/24/"
                "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f.jpg": (
                    "b224e35856e4fd36717e3ffdf09bf020e3e428fcd067162a2a6434586e4acc2f"
                )
            }
        },
    )
    assert extractor._cdn_outage_preview_only is True
    assert gallery_calls["count"] == 1
    calls_after_outage_detection = len(fake_session.calls)

    def fail_gallery_dl(*args, **kwargs):
        raise AssertionError("gallery-dl should be skipped in CDN outage mode")

    monkeypatch.setattr(
        "src.kemono_extractor.run_with_idle_timeout",
        fail_gallery_dl,
    )

    paths = extractor.download_media_for_works(
        "fanbox/1184461",
        ["fanbox_687045"],
        hash_map={
            "fanbox_687045": {
                "https://kemono.cr/data/aa/bb/"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg": (
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
            }
        },
    )

    assert list(paths) == ["fanbox_687045"]
    downloaded = tmp_path / paths["fanbox_687045"][0]
    assert downloaded.name == "687045_01_preview.png"
    assert downloaded.read_bytes() == PNG_BYTES
    assert fake_session.calls[calls_after_outage_detection:] == [
        "https://img.kemono.cr/thumbnail/data/aa/bb/"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
    ]


def test_kemono_cdn_outage_mode_resets_with_reachability_cache(tmp_path):
    extractor = KemonoExtractor({
        "media": {"save_dir": str(tmp_path / "media")},
        "kemono": {
            "cdn_fallback_hosts": ["n2.kemono.cr", "n3.kemono.cr"],
            "cdn_fallback_cooldown_seconds": 60,
        },
    })

    extractor._mark_cdn_host_failure("n2.kemono.cr", requests.ConnectTimeout("n2"))
    assert extractor._cdn_outage_preview_only is False
    extractor._mark_cdn_host_failure("n3.kemono.cr", requests.ConnectTimeout("n3"))

    assert extractor._cdn_outage_preview_only is True
    assert extractor._cdn_bad_until

    extractor.clear_reachability_cache()

    assert extractor._cdn_outage_preview_only is False
    assert extractor._cdn_bad_until == {}

