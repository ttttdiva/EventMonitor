"""
センシティブ判定（R-18タグ）に関するユニットテスト

根本原因: Twitter APIのUserMediaクエリでpossibly_sensitiveが
レスポンスに含まれない場合、sensitive=Noneが設定され、
それがfalsyとして扱われてrating:r-18タグが欠落する問題の修正を検証。
"""

import json
from datetime import datetime

import pytest


# ============================================================
# Test: gallery_dl_extractor._extract_tweet_info
# ============================================================

class TestExtractTweetInfoSensitive:
    """gallery-dlの_extract_tweet_infoにおけるsensitive正規化テスト"""

    @pytest.fixture
    def extractor(self):
        """GalleryDLExtractorのインスタンスを作成"""
        from src.gallery_dl_extractor import GalleryDLExtractor
        config = {
            'tweet_settings': {
                'gallery_dl': {
                    'enabled': True,
                    'cookies_file': 'dummy.txt'
                }
            }
        }
        return GalleryDLExtractor(config)

    def _make_gallery_dl_data(self, sensitive=None, sensitive_flags=None):
        """gallery-dlっぽいデータを生成"""
        data = {
            'tweet_id': 123456789,
            'date': '2025-01-01 12:00:00',
            'user': {'name': 'testuser', 'nick': 'Test User'},
            'content': 'Test tweet content',
            'url': 'https://pbs.twimg.com/media/test.jpg',
            'favorite_count': 10,
            'retweet_count': 5,
            'reply_count': 1,
            'quote_count': 0,
        }
        if sensitive is not None:
            data['sensitive'] = sensitive
        if sensitive_flags is not None:
            data['sensitive_flags'] = sensitive_flags
        return data

    def test_sensitive_true(self, extractor):
        """sensitive=True → sensitive=Trueになること"""
        data = self._make_gallery_dl_data(sensitive=True)
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is True

    def test_sensitive_false(self, extractor):
        """sensitive=False → sensitive=Falseになること"""
        data = self._make_gallery_dl_data(sensitive=False)
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is False

    def test_sensitive_none_no_flags(self, extractor):
        """sensitive=None, sensitive_flags=[] → sensitive=Falseになること"""
        data = self._make_gallery_dl_data(sensitive=None, sensitive_flags=[])
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is False

    def test_sensitive_none_with_flags(self, extractor):
        """sensitive=None, sensitive_flags=['Nudity'] → sensitive=Trueになること
        
        これが根本原因のバグケース:
        Twitter APIがpossibly_sensitiveを返さない → gallery-dlがsensitive=Noneをセット
        → しかしsensitive_media_warningは存在する → sensitive_flags=['Nudity']
        """
        data = self._make_gallery_dl_data(sensitive=None, sensitive_flags=['Nudity'])
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is True, \
            "sensitive_flagsが存在する場合、sensitiveはTrueになるべき"

    def test_sensitive_missing_with_flags(self, extractor):
        """sensitiveキーが存在しない + sensitive_flags=['Sensitive'] → True"""
        data = self._make_gallery_dl_data(sensitive_flags=['Sensitive'])
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is True

    def test_sensitive_missing_no_flags(self, extractor):
        """sensitiveキーもsensitive_flagsも存在しない → False"""
        data = self._make_gallery_dl_data()
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is False

    def test_sensitive_flags_empty_tuple(self, extractor):
        """sensitive=None, sensitive_flags=() → sensitive=Falseになること
        
        gallery-dlは_extract_mediaでsensitive_media_warningがない場合に
        空タプル()をsensitive_flagsにセットする。
        """
        data = self._make_gallery_dl_data(sensitive=None, sensitive_flags=())
        result = extractor._extract_tweet_info(data)
        assert result['sensitive'] is False


# ============================================================
# Test: gallery_dl_extractor.merge_with_twscrape
# ============================================================

class TestMergeWithTwscrapeSensitive:
    """merge_with_twscrapeでsensitive情報が保持されるかテスト"""

    @pytest.fixture
    def extractor(self):
        from src.gallery_dl_extractor import GalleryDLExtractor
        config = {
            'tweet_settings': {
                'gallery_dl': {
                    'enabled': True,
                    'cookies_file': 'dummy.txt'
                }
            }
        }
        return GalleryDLExtractor(config)

    def test_gallery_dl_sensitive_preserved_when_twscrape_missing(self, extractor):
        """twscrapeにsensitive=Falseの場合、gallery-dlのsensitive=Trueで補完"""
        gallery_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': True, 'sensitive_flags': ['Nudity']}
        ]
        twscrape_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': False}
        ]
        result = extractor.merge_with_twscrape(gallery_tweets, twscrape_tweets)
        assert result[0]['sensitive'] is True, \
            "twscrapeがsensitive=Falseでもgallery-dlのsensitive=Trueで補完されるべき"

    def test_gallery_dl_sensitive_flags_preserved(self, extractor):
        """twscrapeにsensitive_flagsがない場合、gallery-dlのsensitive_flagsで補完"""
        gallery_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': True, 'sensitive_flags': ['Nudity', 'Sensitive']}
        ]
        twscrape_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': False}
        ]
        result = extractor.merge_with_twscrape(gallery_tweets, twscrape_tweets)
        assert result[0]['sensitive_flags'] == ['Nudity', 'Sensitive']

    def test_twscrape_sensitive_true_not_overwritten(self, extractor):
        """twscrapeがsensitive=Trueの場合、gallery-dlで上書きしない"""
        gallery_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': False}
        ]
        twscrape_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': True}
        ]
        result = extractor.merge_with_twscrape(gallery_tweets, twscrape_tweets)
        assert result[0]['sensitive'] is True

    def test_twscrape_account_sensitive_preserved(self, extractor):
        """twscrapeのaccount_sensitiveをgallery-dl側へ補完する"""
        gallery_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': False}
        ]
        twscrape_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': False, 'account_sensitive': True}
        ]
        result = extractor.merge_with_twscrape(gallery_tweets, twscrape_tweets)
        assert result[0]['account_sensitive'] is True

    def test_non_overlapping_tweets_preserved(self, extractor):
        """重複しないツイートはそのまま保持"""
        gallery_tweets = [
            {'id': '1', 'date': '2025-01-01', 'sensitive': True, 'sensitive_flags': ['Nudity']}
        ]
        twscrape_tweets = [
            {'id': '2', 'date': '2025-01-02', 'sensitive': False}
        ]
        result = extractor.merge_with_twscrape(gallery_tweets, twscrape_tweets)
        assert len(result) == 2
        tweet_1 = next(t for t in result if t['id'] == '1')
        assert tweet_1['sensitive'] is True


# ============================================================
# Test: hydrus_client._generate_tags
# ============================================================

class TestGenerateTagsSensitive:
    """_generate_tagsでのsensitive判定テスト"""

    @pytest.fixture
    def hydrus_client(self):
        from src.hydrus_client import HydrusClient
        config = {
            'hydrus': {
                'enabled': True,
                'api_url': 'http://localhost:45869',
                'access_key': 'dummy',
                'tag_settings': {
                    'base_tags': ['source:twitter'],
                    'include_tweet_id_tag': False,
                    'include_title_tag': False,
                    'include_date_tag': False,
                    'include_detected_keywords': False,
                }
            }
        }
        return HydrusClient(config)

    def _make_tweet_data(self, sensitive=None, sensitive_flags=None, account_sensitive=None):
        data = {
            'id': '123456789',
            'username': 'testuser',
            'display_name': 'Test User',
        }
        if sensitive is not None:
            data['sensitive'] = sensitive
        if sensitive_flags is not None:
            data['sensitive_flags'] = sensitive_flags
        if account_sensitive is not None:
            data['account_sensitive'] = account_sensitive
        return data

    def test_sensitive_true(self, hydrus_client):
        """sensitive=True → rating:r-18タグあり"""
        data = self._make_tweet_data(sensitive=True)
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' in tags

    def test_sensitive_false_no_flags(self, hydrus_client):
        """sensitive=False, sensitive_flags=[] → rating:r-18タグなし"""
        data = self._make_tweet_data(sensitive=False, sensitive_flags=[])
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' not in tags

    def test_sensitive_none_no_flags(self, hydrus_client):
        """sensitive=None, sensitive_flags=[] → rating:r-18タグなし"""
        data = self._make_tweet_data(sensitive=None, sensitive_flags=[])
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' not in tags

    def test_sensitive_none_with_flags(self, hydrus_client):
        """sensitive=None, sensitive_flags=['Nudity'] → rating:r-18タグあり"""
        data = self._make_tweet_data(sensitive=None, sensitive_flags=['Nudity'])
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' in tags, \
            "sensitive_flagsが存在する場合、rating:r-18タグが追加されるべき"

    def test_sensitive_false_with_flags(self, hydrus_client):
        """sensitive=False, sensitive_flags=['Nudity'] → rating:r-18タグあり"""
        data = self._make_tweet_data(sensitive=False, sensitive_flags=['Nudity'])
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' in tags

    def test_no_sensitive_keys(self, hydrus_client):
        """sensitiveキーが存在しない → rating:r-18タグなし"""
        data = self._make_tweet_data()
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' not in tags

    def test_sensitive_flags_empty_tuple(self, hydrus_client):
        """sensitive=None, sensitive_flags=() → rating:r-18タグなし"""
        data = self._make_tweet_data(sensitive=None, sensitive_flags=())
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' not in tags

    def test_account_sensitive_true(self, hydrus_client):
        """account_sensitive=True → rating:r-18タグあり"""
        data = self._make_tweet_data(account_sensitive=True)
        tags = hydrus_client._generate_tags(data)
        assert 'rating:r-18' in tags


class TestHydrusMetadataTagExtraction:
    """Hydrusメタデータからタグを抽出する補助ロジックのテスト"""

    @pytest.fixture
    def hydrus_client(self):
        from src.hydrus_client import HydrusClient
        config = {
            'hydrus': {
                'enabled': True,
                'api_url': 'http://localhost:45869',
                'access_key': 'dummy',
                'tag_services': {
                    'gelbooru': 'danbooru tags',
                },
            }
        }
        client = HydrusClient(config)
        client._platform_to_service_key = {'gelbooru': 'gel-key'}
        return client

    def test_all_tag_service_keys_include_legacy_local_tags(self, hydrus_client):
        assert hydrus_client.all_tag_service_keys == [
            '6c6f63616c2074616773',
            'gel-key',
        ]

    def test_extract_display_tags_reads_current_hydrus_metadata_format(self, hydrus_client):
        metadata = {
            'tags': {
                '6c6f63616c2074616773': {
                    'display_tags': {
                        '0': ['imported_by:eventmonitor', 'rating:r-18'],
                    }
                }
            }
        }

        tags = hydrus_client._extract_display_tags_from_metadata(metadata)

        assert 'imported_by:eventmonitor' in tags
        assert 'rating:r-18' in tags

    def test_extract_display_tags_reads_legacy_hydrus_metadata_format(self, hydrus_client):
        metadata = {
            'service_keys_to_statuses_to_display_tags': {
                '6c6f63616c2074616773': {
                    '0': ['creator:test', 'title:hello'],
                }
            }
        }

        tags = hydrus_client._extract_display_tags_from_metadata(metadata)

        assert 'creator:test' in tags
        assert 'title:hello' in tags


class TestArtworkTagGeneration:
    @pytest.fixture
    def hydrus_client(self):
        from src.hydrus_client import HydrusClient
        return HydrusClient({
            'hydrus': {
                'enabled': True,
                'api_url': 'http://localhost:45869',
                'access_key': 'dummy',
                'tag_settings': {
                    'base_tags': ['source:twitter', 'imported_by:eventmonitor'],
                    'creator_tag_format': 'creator:{name}',
                    'include_title_tag': True,
                },
            }
        })

    def test_platform_artwork_tags_use_common_builder(self, hydrus_client):
        tags = hydrus_client._generate_fanbox_tags({
            'id': 'post-1',
            'creator_id': 'creator-a',
            'display_name': 'Creator A',
            'text': 'A long title',
            'tags': ['fanbox-tag'],
            'sensitive': True,
            'custom_tags': ['custom:tag'],
        })

        assert tags == [
            'source:fanbox',
            'imported_by:eventmonitor',
            'fanbox_id:post-1',
            'creator:Creator A',
            'fanbox_user:creator-a',
            'title:A long title',
            'fanbox-tag',
            'rating:r-18',
            'rank:3',
            'custom:tag',
        ]

    def test_kemono_tags_keep_service_and_always_r18(self, hydrus_client):
        tags = hydrus_client._generate_kemono_tags({
            'id': 'fanbox_1',
            'username': '123',
            'display_name': 'Kemono User',
            'service': 'fanbox',
            'tags': ['not-forwarded'],
            'sensitive': False,
        })

        assert 'service:fanbox' in tags
        assert 'rating:r-18' in tags
        assert 'not-forwarded' not in tags

    def test_gelbooru_tags_resolve_artist_user_id_from_csv(self, hydrus_client):
        hydrus_client.config['monitored_accounts'] = [
            {
                'username': 'artist_user_id',
                'display_name': 'Artist Display',
                'platform': 'pixiv',
            }
        ]
        hydrus_client._csv_creator_map = None

        my_tags, danbooru_tags = hydrus_client._generate_gelbooru_tags_split({
            'id': '12345',
            'username': 'touhoku_kiritan',
            'display_name': '東北きりたん',
            'tags_artist': ['artist_user_id'],
            'tags_character': ['touhoku_kiritan'],
            'tags_copyright': ['voiceroid'],
            'sensitive': False,
            'rank': 2,
        })

        assert 'creator:Artist Display' in my_tags
        assert 'creator:artist_user_id' not in my_tags
        assert 'gelbooru_query:touhoku_kiritan' in my_tags
        assert 'gelbooru_artist:artist_user_id' in my_tags
        assert 'pixiv_user:artist_user_id' in my_tags
        assert 'artist_user_id' in danbooru_tags
        assert 'character:touhoku_kiritan' in danbooru_tags
        assert 'series:voiceroid' in danbooru_tags

    def test_gelbooru_tags_keep_unknown_artist_as_creator(self, hydrus_client):
        my_tags, danbooru_tags = hydrus_client._generate_gelbooru_tags_split({
            'id': '12345',
            'username': 'search_tag',
            'tags_artist': ['unknown_artist'],
            'sensitive': False,
        })

        assert 'creator:unknown_artist' in my_tags
        assert 'gelbooru_artist:unknown_artist' in my_tags
        assert 'unknown_artist' in danbooru_tags


class DummyRawResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestTwitterMonitorAccountSensitive:
    @pytest.fixture
    def monitor(self):
        from src.twitter_monitor import TwitterMonitor

        config = {
            'tweet_settings': {
                'gallery_dl': {
                    'enabled': False,
                }
            }
        }
        return TwitterMonitor(config)

    def test_extract_account_sensitive_from_raw_user(self, monitor):
        raw = DummyRawResponse(
            {
                'data': {
                    'user': {
                        'result': {
                            'legacy': {
                                'possibly_sensitive': True,
                            }
                        }
                    }
                }
            }
        )

        assert monitor._extract_account_sensitive_from_raw_user(raw) is True

    @pytest.mark.asyncio
    async def test_resolve_account_sensitive_uses_raw_user_payload(self, monitor, monkeypatch):
        async def noop():
            return None

        class DummyApi:
            async def user_by_login_raw(self, username):
                assert username == 'CostRa777'
                return DummyRawResponse(
                    {
                        'data': {
                            'user': {
                                'result': {
                                    'legacy': {
                                        'possibly_sensitive': True,
                                    }
                                }
                            }
                        }
                    }
                )

        monitor.api = DummyApi()
        monkeypatch.setattr(monitor, '_initialize_accounts', noop)

        assert await monitor._resolve_account_sensitive('CostRa777') is True
