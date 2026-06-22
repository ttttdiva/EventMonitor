import sys
try:
    # pysqlite3を標準のsqlite3より先にインポート（可能な場合）
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    # Windows等でpysqlite3がない場合は標準のsqlite3を使用
    pass

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Iterable, Tuple, Type
import json

from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean, Integer, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
from pathlib import Path


Base = declarative_base()


class AllTweets(Base):
    """全ツイートログを保存するテーブル"""
    __tablename__ = 'all_tweets'
    
    id = Column(String(64), primary_key=True)  # Tweet ID
    username = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    tweet_text = Column(Text, nullable=False)
    tweet_date = Column(DateTime, nullable=False, index=True)
    tweet_url = Column(String(500), nullable=False)
    
    # メディア情報
    media_urls = Column(Text)  # JSON配列として保存
    local_media = Column(Text)  # ローカルメディアパス（画像・動画、JSON配列として保存）
    huggingface_urls = Column(Text)  # アップロード後のURL（JSON配列）
    
    # センシティブ情報
    sensitive = Column(Boolean, default=False)  # ツイートのセンシティブフラグ

    # メタデータ
    created_at = Column(DateTime, default=datetime.now)
    checked_for_event = Column(Boolean, default=False)  # イベント検査済みフラグ
    hydrus_expected_count = Column(Integer, default=0)  # Hydrusインポート対象の想定件数
    hydrus_imported_count = Column(Integer, default=0)  # Hydrusインポート済み件数


class EventTweet(Base):
    """イベント関連ツイートのモデル"""
    __tablename__ = 'event_tweets'
    
    id = Column(String(64), primary_key=True)  # Tweet ID
    username = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    tweet_text = Column(Text, nullable=False)
    tweet_date = Column(DateTime, nullable=False, index=True)
    tweet_url = Column(String(500), nullable=False)
    
    # イベント情報
    is_event_related = Column(Boolean, default=True)
    event_type = Column(String(100))
    event_date = Column(String(50))  # 推定されるイベント日付
    participation_type = Column(String(50))  # サークル参加/一般参加/委託
    space_number = Column(String(50))
    circle_name = Column(String(200))
    confidence_score = Column(String(10))  # 判定の信頼度
    
    # メディア情報
    media_urls = Column(Text)  # JSON配列として保存
    local_media = Column(Text)  # ローカルメディアパス（画像・動画、JSON配列として保存）
    
    # 分析結果
    analysis_result = Column(Text)  # JSON形式で保存
    
    # センシティブ情報
    sensitive = Column(Boolean, default=False)  # ツイートのセンシティブフラグ

    # メタデータ
    created_at = Column(DateTime, default=datetime.now)
    notified = Column(Boolean, default=False)  # Discord通知済みフラグ
    hydrus_expected_count = Column(Integer, default=0)  # Hydrusインポート対象の想定件数
    hydrus_imported_count = Column(Integer, default=0)  # Hydrusインポート済み件数


class LogOnlyTweet(Base):
    """ログ専用アカウントのツイートを保存するテーブル"""
    __tablename__ = 'log_only_tweets'
    
    id = Column(String(64), primary_key=True)  # Tweet ID
    username = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    tweet_text = Column(Text, nullable=False)
    tweet_date = Column(DateTime, nullable=False, index=True)
    tweet_url = Column(String(500), nullable=False)
    media_urls = Column(Text)  # JSON配列として保存
    huggingface_urls = Column(Text)  # アップロード後のURL（JSON配列）
    sensitive = Column(Boolean, default=False)  # ツイートのセンシティブフラグ
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class TwitterRetryQueue(Base):
    """Twitter メディア取得失敗の再試行キュー"""
    __tablename__ = 'twitter_retry_queue'

    username = Column(String(100), primary_key=True)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class PixivWork(Base):
    """Pixiv作品を保存するテーブル"""
    __tablename__ = 'pixiv_works'

    id = Column(String(64), primary_key=True)  # 作品ID
    user_id = Column(String(100), nullable=False, index=True)  # PixivユーザーID（数値）
    display_name = Column(String(200))
    title = Column(Text)  # 作品タイトル
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    work_type = Column(String(50))  # illust / manga / ugoira
    tags = Column(Text)  # JSON配列
    page_count = Column(Integer, default=1)
    bookmark_count = Column(Integer, default=0)
    x_restrict = Column(Integer, default=0)  # 0=一般, 1=R-18, 2=R-18G
    sensitive = Column(Boolean, default=False)  # センシティブフラグ（x_restrict >= 1）
    media_urls = Column(Text)  # JSON配列（元URL）
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class PixivLogOnlyWork(Base):
    """Pixivログ専用アカウントの作品を保存するテーブル"""
    __tablename__ = 'pixiv_log_only_works'

    id = Column(String(64), primary_key=True)  # 作品ID
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    work_type = Column(String(50))
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # センシティブフラグ
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class KemonoWork(Base):
    """Kemono.cr作品を保存するテーブル"""
    __tablename__ = 'kemono_works'

    id = Column(String(100), primary_key=True)  # {service}_{post_id}
    user_id = Column(String(100), nullable=False, index=True)  # fanbox/3316400
    display_name = Column(String(200))
    title = Column(Text)
    content = Column(Text)  # 投稿本文
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    service = Column(String(50))  # fanbox / fantia / etc.
    file_count = Column(Integer, default=0)
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class KemonoLogOnlyWork(Base):
    """Kemonoログ専用アカウントの作品を保存するテーブル"""
    __tablename__ = 'kemono_log_only_works'

    id = Column(String(100), primary_key=True)  # {service}_{post_id}
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    content = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    service = Column(String(50))
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class TinamiWork(Base):
    """TINAMI作品を保存するテーブル"""
    __tablename__ = 'tinami_works'

    id = Column(String(100), primary_key=True)  # 作品ID
    user_id = Column(String(100), nullable=False, index=True)  # prof_id
    display_name = Column(String(200))
    title = Column(Text)  # 作品タイトル
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    work_type = Column(String(50))  # illustration / manga
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # R-18フラグ
    media_urls = Column(Text)  # JSON配列（元URL）
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class TinamiLogOnlyWork(Base):
    """TINAMIログ専用アカウントの作品を保存するテーブル"""
    __tablename__ = 'tinami_log_only_works'

    id = Column(String(100), primary_key=True)  # 作品ID
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    work_type = Column(String(50))
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class PoipikuWork(Base):
    """Poipiku投稿を保存するテーブル"""
    __tablename__ = 'poipiku_works'

    id = Column(String(100), primary_key=True)  # 投稿ID
    user_id = Column(String(100), nullable=False, index=True)  # PoipikuユーザーID
    display_name = Column(String(200))
    title = Column(Text)  # 投稿テキスト/カテゴリ
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # R-18フラグ
    media_urls = Column(Text)  # JSON配列（元URL）
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class PoipikuLogOnlyWork(Base):
    """Poipikuログ専用アカウントの投稿を保存するテーブル"""
    __tablename__ = 'poipiku_log_only_works'

    id = Column(String(100), primary_key=True)  # 投稿ID
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class FantiaWork(Base):
    """Fantia投稿を保存するテーブル"""
    __tablename__ = 'fantia_works'

    id = Column(String(100), primary_key=True)  # post_id
    user_id = Column(String(100), nullable=False, index=True)  # fanclub_id
    display_name = Column(String(200))
    title = Column(Text)  # post_title
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # rating=="adult"
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class FantiaLogOnlyWork(Base):
    """Fantiaログ専用アカウントの投稿を保存するテーブル"""
    __tablename__ = 'fantia_log_only_works'

    id = Column(String(100), primary_key=True)  # post_id
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class NijieWork(Base):
    """ニジエ投稿を保存するテーブル"""
    __tablename__ = 'nijie_works'

    id = Column(String(100), primary_key=True)  # image_id
    user_id = Column(String(100), nullable=False, index=True)  # ニジエユーザーID
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # tags内のR-18判定
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class NijieLogOnlyWork(Base):
    """ニジエログ専用アカウントの投稿を保存するテーブル"""
    __tablename__ = 'nijie_log_only_works'

    id = Column(String(100), primary_key=True)  # image_id
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class SkebWork(Base):
    """Skeb投稿を保存するテーブル"""
    __tablename__ = 'skeb_works'

    id = Column(String(100), primary_key=True)  # post_id
    user_id = Column(String(100), nullable=False, index=True)  # screen_name
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # nsfw判定
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class SkebLogOnlyWork(Base):
    """Skebログ専用アカウントの投稿を保存するテーブル"""
    __tablename__ = 'skeb_log_only_works'

    id = Column(String(100), primary_key=True)  # post_id
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class BilibiliWork(Base):
    """bilibili動態(opus)を保存するテーブル"""
    __tablename__ = 'bilibili_works'

    id = Column(String(100), primary_key=True)  # opus_id
    user_id = Column(String(100), nullable=False, index=True)  # 数値ユーザーID(mid)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # bilibiliは常にFalse
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class BilibiliLogOnlyWork(Base):
    """bilibiliログ専用アカウントの動態(opus)を保存するテーブル"""
    __tablename__ = 'bilibili_log_only_works'

    id = Column(String(100), primary_key=True)  # opus_id
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class MisskeyWork(Base):
    """Misskeyノートを保存するテーブル"""
    __tablename__ = 'misskey_works'

    id = Column(String(100), primary_key=True)  # note_id (alphanumeric)
    user_id = Column(String(100), nullable=False, index=True)  # username
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # cw判定
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class MisskeyLogOnlyWork(Base):
    """Misskeyログ専用アカウントのノートを保存するテーブル"""
    __tablename__ = 'misskey_log_only_works'

    id = Column(String(100), primary_key=True)  # note_id (alphanumeric)
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class GelbooruWork(Base):
    """Gelbooru投稿を保存するテーブル"""
    __tablename__ = 'gelbooru_works'

    id = Column(String(64), primary_key=True)  # post ID
    user_id = Column(String(500), nullable=False, index=True)  # 検索クエリ
    display_name = Column(String(200))  # 検索ラベル
    title = Column(Text)  # タグ文字列
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    source_url = Column(String(500))  # 元投稿URL (Pixiv等)
    score = Column(Integer, default=0)
    rating = Column(String(20))  # general/sensitive/questionable/explicit
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class GelbooruLogOnlyWork(Base):
    """Gelbooruログ専用の投稿を保存するテーブル"""
    __tablename__ = 'gelbooru_log_only_works'

    id = Column(String(64), primary_key=True)  # post ID
    user_id = Column(String(500), nullable=False, index=True)  # 検索クエリ
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    source_url = Column(String(500))
    score = Column(Integer, default=0)
    rating = Column(String(20))
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class FanboxWork(Base):
    """FANBOX作品を保存するテーブル"""
    __tablename__ = 'fanbox_works'

    id = Column(String(100), primary_key=True)  # post_id
    user_id = Column(String(100), nullable=False, index=True)  # creator_id
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # hasAdultContent
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class FanboxLogOnlyWork(Base):
    """FANBOXログ専用の作品を保存するテーブル"""
    __tablename__ = 'fanbox_log_only_works'

    id = Column(String(100), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class BlueskyWork(Base):
    """Bluesky投稿を保存するテーブル"""
    __tablename__ = 'bluesky_works'

    id = Column(String(100), primary_key=True)  # post_id（英数字）
    user_id = Column(String(100), nullable=False, index=True)  # handle
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # labelsベース判定
    media_urls = Column(Text)  # JSON配列
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class BlueskyLogOnlyWork(Base):
    """Blueskyログ専用の投稿を保存するテーブル"""
    __tablename__ = 'bluesky_log_only_works'

    id = Column(String(100), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class PrivatterWork(Base):
    """Privatter投稿を保存するテーブル"""
    __tablename__ = 'privatter_works'

    id = Column(String(100), primary_key=True)  # 投稿ID
    user_id = Column(String(100), nullable=False, index=True)  # TwitterユーザーID
    display_name = Column(String(200))
    title = Column(Text)  # 投稿タイトル
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)  # Privatterは常にTrue（R-18判定不可）
    media_urls = Column(Text)  # JSON配列（元URL）
    local_media = Column(Text)  # JSON配列（ローカルパス）
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)


class PrivatterLogOnlyWork(Base):
    """Privatterログ専用アカウントの投稿を保存するテーブル"""
    __tablename__ = 'privatter_log_only_works'

    id = Column(String(100), primary_key=True)  # 投稿ID
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)  # JSON配列
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)  # JSON配列
    huggingface_urls = Column(Text)  # JSON配列
    created_at = Column(DateTime, default=datetime.now)
    uploaded_to_hf = Column(Boolean, default=False)


class ArtworkRetryQueue(Base):
    """アカウント単位の artwork 再試行キュー"""
    __tablename__ = 'artwork_retry_queue'

    platform = Column(String(50), primary_key=True)
    account_id = Column(String(100), primary_key=True)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class DatabaseManager:
    _ID_BATCH_SIZE = 900  # SQLite parameter limit safety margin (default max 999)

    # プラットフォーム名 → (MonitorModel, LogOnlyModel) のマッピング
    PLATFORM_MODELS = {
        'twitter':  (AllTweets, LogOnlyTweet),
        'pixiv':    (PixivWork, PixivLogOnlyWork),
        'kemono':   (KemonoWork, KemonoLogOnlyWork),
        'tinami':   (TinamiWork, TinamiLogOnlyWork),
        'poipiku':  (PoipikuWork, PoipikuLogOnlyWork),
        'fantia':   (FantiaWork, FantiaLogOnlyWork),
        'nijie':    (NijieWork, NijieLogOnlyWork),
        'skeb':     (SkebWork, SkebLogOnlyWork),
        'bilibili': (BilibiliWork, BilibiliLogOnlyWork),
        'misskey':  (MisskeyWork, MisskeyLogOnlyWork),
        'gelbooru': (GelbooruWork, GelbooruLogOnlyWork),
        'fanbox':   (FanboxWork, FanboxLogOnlyWork),
        'bluesky':  (BlueskyWork, BlueskyLogOnlyWork),
        'privatter': (PrivatterWork, PrivatterLogOnlyWork),
    }

    PLATFORM_IDENTITY_FIELDS = {
        'twitter': 'username',
    }

    PLATFORM_DATE_FIELDS = {
        'twitter': 'tweet_date',
    }

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.Database")
        self.engine = None
        self.Session = None
        self.db_config = None
        self._initialize_database()

    @staticmethod
    def _chunk_list(values: List[str], chunk_size: int) -> Iterable[List[str]]:
        """Split list into fixed-size chunks."""
        for idx in range(0, len(values), chunk_size):
            yield values[idx:idx + chunk_size]

    def _get_platform_models(self, platform: str) -> Tuple[Type[Base], Type[Base]]:
        try:
            return self.PLATFORM_MODELS[platform]
        except KeyError as exc:
            raise ValueError(f"Unsupported platform: {platform}") from exc

    def _get_platform_identity_field(self, platform: str) -> str:
        return self.PLATFORM_IDENTITY_FIELDS.get(platform, 'user_id')

    def _get_platform_date_field(self, platform: str) -> str:
        return self.PLATFORM_DATE_FIELDS.get(platform, 'work_date')

    def _load_existing_tweet_meta(self, session: Session, model: Type[Base], tweet_ids: List[str]) -> Dict[str, Optional[str]]:
        """Return {tweet_id: username} for tweets already stored in the given model."""
        normalized_ids = [tweet_id for tweet_id in dict.fromkeys(tweet_ids) if tweet_id]
        if not normalized_ids:
            return {}

        existing: Dict[str, Optional[str]] = {}
        for chunk in self._chunk_list(normalized_ids, self._ID_BATCH_SIZE):
            results = session.query(model.id, model.username).filter(model.id.in_(chunk)).all()
            for tweet_id, owner in results:
                existing[tweet_id] = owner

        return existing

    @staticmethod
    def _is_twitter_sensitive(tweet_data: Dict[str, Any]) -> bool:
        return bool(
            tweet_data.get('sensitive')
            or tweet_data.get('account_sensitive')
            or bool(tweet_data.get('sensitive_flags'))
        )
        
    def _initialize_database(self):
        """データベース接続を初期化"""
        try:
            db_config = self.config['database']
            self.db_config = db_config
            
            # SQLiteかMySQLかを判定
            if db_config.get('type') == 'sqlite':
                # SQLiteの場合
                db_path = db_config['path']
                # ディレクトリを作成
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                db_url = f"sqlite:///{db_path}"
                
                self.engine = create_engine(
                    db_url,
                    echo=False
                )
            else:
                # MySQLの場合（従来の処理）
                host = db_config['host']
                port = db_config['port']
                user = db_config['user']
                password = os.getenv('DB_PASSWORD', db_config['password'])
                database = db_config['database']
                
                db_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"
                
                self.engine = create_engine(
                    db_url,
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=10,
                    echo=False
                )
            
            # テーブルを作成
            Base.metadata.create_all(self.engine)
            self._ensure_hydrus_columns()
            
            # セッションファクトリーを作成
            self.Session = sessionmaker(bind=self.engine)
            
            self.logger.info("Database initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}")
            raise

    def _ensure_hydrus_columns(self) -> None:
        """Add Hydrus bookkeeping columns to older databases when needed."""
        if not self.db_config:
            return

        required_columns = [
            ("hydrus_expected_count", "INTEGER DEFAULT 0"),
            ("hydrus_imported_count", "INTEGER DEFAULT 0"),
            ("sensitive", "BOOLEAN DEFAULT 0"),
        ]
        table_names = [
            "all_tweets", "event_tweets", "log_only_tweets",
            "pixiv_works", "pixiv_log_only_works",
            "kemono_works", "kemono_log_only_works",
            "tinami_works", "tinami_log_only_works",
            "poipiku_works", "poipiku_log_only_works",
            "fantia_works", "fantia_log_only_works",
            "nijie_works", "nijie_log_only_works",
            "skeb_works", "skeb_log_only_works",
            "bilibili_works", "bilibili_log_only_works",
            "misskey_works", "misskey_log_only_works",
            "gelbooru_works", "gelbooru_log_only_works",
            "fanbox_works", "fanbox_log_only_works",
            "bluesky_works", "bluesky_log_only_works",
            "privatter_works", "privatter_log_only_works",
        ]

        try:
            inspector = inspect(self.engine)
            with self.engine.begin() as conn:
                for table_name in table_names:
                    existing = {
                        col["name"]
                        for col in inspector.get_columns(table_name)
                    }
                    for column_name, column_type in required_columns:
                        if column_name not in existing:
                            conn.execute(
                                text(
                                    f"ALTER TABLE {table_name} "
                                    f"ADD COLUMN {column_name} {column_type}"
                                )
                            )
        except Exception as e:
            self.logger.warning(f"Failed to ensure Hydrus columns: {e}")

    @staticmethod
    def estimate_hydrus_expected_count(local_media: List[str]) -> int:
        """Hydrusインポート対象の想定件数を推定（import_fileと同じホワイトリストで判定）"""
        if not local_media:
            return 0

        allowed_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.avif', '.gif'}
        count = 0
        for media_path in local_media:
            path_str = str(media_path).replace('\\', '/')
            if 'images/' not in path_str:
                continue
            if Path(path_str).suffix.lower() not in allowed_extensions:
                continue
            count += 1
        return count

    def update_hydrus_import_status(
        self,
        tweet_id: str,
        imported_count: int,
        expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        """Hydrusインポート状態を更新（all_tweets/event_tweets両方対象）

        Args:
            force: Trueの場合、max()を使わず直接上書き（リトライ時に使用）
        """
        session = self._get_session()
        try:
            target_models = (AllTweets, EventTweet)
            for model in target_models:
                tweet = session.query(model).filter(model.id == tweet_id).first()
                if not tweet:
                    continue
                if expected_count is not None:
                    if force:
                        tweet.hydrus_expected_count = expected_count
                    else:
                        current_expected = tweet.hydrus_expected_count or 0
                        tweet.hydrus_expected_count = max(current_expected, expected_count)
                if imported_count is not None:
                    if force:
                        tweet.hydrus_imported_count = imported_count
                    else:
                        current_imported = tweet.hydrus_imported_count or 0
                        tweet.hydrus_imported_count = max(current_imported, imported_count)
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to update Hydrus import status for {tweet_id}: {e}")
        finally:
            session.close()
    
    def _get_session(self) -> Session:
        """データベースセッションを取得"""
        return self.Session()
    
    def filter_new_tweets(self, tweets: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
        """新規ツイートのみをフィルタリング（all_tweetsテーブルを参照）"""
        session = self._get_session()
        new_tweets = []

        try:
            if not tweets:
                return []

            incoming_ids = [tweet.get('id') for tweet in tweets]
            existing_meta = self._load_existing_tweet_meta(session, AllTweets, incoming_ids)
            existing_ids = set(existing_meta.keys())

            # 新規ツイートのみを抽出
            for tweet in tweets:
                tweet_id = tweet.get('id')
                if not tweet_id:
                    self.logger.warning("Tweet without id received during filtering; treating as new entry")
                    new_tweets.append(tweet)
                    continue

                if tweet_id not in existing_ids:
                    new_tweets.append(tweet)
                else:
                    owner = existing_meta.get(tweet_id)
                    if owner:
                        self.logger.debug(
                            f"Tweet {tweet_id} already exists in database (originally from @{owner})"
                        )

            self.logger.info(f"Filtered {len(new_tweets)} new tweets out of {len(tweets)} total for @{username}")
            return new_tweets

        except SQLAlchemyError as e:
            self.logger.error(f"Database error in filter_new_tweets: {e}")
            return tweets  # エラー時は全ツイートを返す（安全側に倒す）
        finally:
            session.close()
    
    def save_single_tweet(self, tweet_data: Dict[str, Any], username: str) -> bool:
        """単一ツイートをall_tweetsテーブルに保存"""
        session = self._get_session()
        
        try:
            # 既存チェック
            existing = session.query(AllTweets).filter(
                AllTweets.id == tweet_data['id']
            ).first()
            
            if existing:
                # 既存ツイートは重複防止による正常な処理
                return True
            
            # 新規レコードを作成
            expected_count = self.estimate_hydrus_expected_count(tweet_data.get('local_media', []))
            tweet_record = AllTweets(
                id=tweet_data['id'],
                username=username,
                display_name=tweet_data.get('display_name', username),
                tweet_text=tweet_data['text'],
                tweet_date=datetime.fromisoformat(tweet_data['date'].replace('Z', '+00:00')),
                tweet_url=tweet_data['url'],
                media_urls=json.dumps(tweet_data.get('media', [])),
                local_media=json.dumps(tweet_data.get('local_media', [])),
                huggingface_urls=json.dumps(tweet_data.get('huggingface_urls', [])),
                checked_for_event=False,
                hydrus_expected_count=expected_count,
                hydrus_imported_count=0
            )
            
            session.add(tweet_record)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_single_tweet: {e}")
            return False
        finally:
            session.close()
    
    def save_all_tweets(self, tweets: List[Dict[str, Any]], username: str) -> int:
        """全ツイートをall_tweetsテーブルに保存"""
        session = self._get_session()
        saved_count = 0
        
        try:
            for tweet_data in tweets:
                # 既存チェック
                existing = session.query(AllTweets).filter(
                    AllTweets.id == tweet_data['id']
                ).first()
                
                if existing:
                    continue
                
                # 新規レコードを作成
                expected_count = self.estimate_hydrus_expected_count(tweet_data.get('local_media', []))
                tweet_record = AllTweets(
                    id=tweet_data['id'],
                    username=username,
                    display_name=tweet_data.get('display_name', username),
                    tweet_text=tweet_data['text'],
                    tweet_date=datetime.fromisoformat(tweet_data['date'].replace('Z', '+00:00')),
                    tweet_url=tweet_data['url'],
                    media_urls=json.dumps(tweet_data.get('media', [])),
                    local_media=json.dumps(tweet_data.get('local_media', [])),
                    huggingface_urls=json.dumps(tweet_data.get('huggingface_urls', [])),  # HF URLsを初期化
                    sensitive=self._is_twitter_sensitive(tweet_data),
                    checked_for_event=False,  # まだイベント検査していない
                    hydrus_expected_count=expected_count,
                    hydrus_imported_count=0
                )
                
                session.add(tweet_record)
                saved_count += 1
            
            session.commit()
            return saved_count
            
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_all_tweets: {e}")
            return 0
        finally:
            session.close()
    
    def save_event_tweets(self, event_tweets: List[Dict[str, Any]], username: str):
        """イベント関連ツイートをevent_tweetsテーブルに保存"""
        session = self._get_session()
        saved_count = 0
        
        try:
            for tweet_data in event_tweets:
                # 既にevent_tweetsに存在するかチェック
                existing = session.query(EventTweet).filter(
                    EventTweet.id == tweet_data['id']
                ).first()
                
                if existing:
                    self.logger.debug(f"Tweet {tweet_data['id']} already exists in event_tweets")
                    continue
                
                # イベント情報を抽出
                event_info = tweet_data.get('event_analysis', {})
                
                # 新規レコードを作成
                expected_count = self.estimate_hydrus_expected_count(tweet_data.get('local_media', []))
                tweet_record = EventTweet(
                    id=tweet_data['id'],
                    username=username,
                    display_name=tweet_data.get('display_name', username),
                    tweet_text=tweet_data['text'],
                    tweet_date=datetime.fromisoformat(tweet_data['date'].replace('Z', '+00:00')),
                    tweet_url=tweet_data['url'],
                    is_event_related=True,
                    event_type=event_info.get('event_type'),
                    event_date=event_info.get('event_date'),
                    participation_type=event_info.get('participation_type'),
                    confidence_score=str(event_info.get('confidence', 1.0)),
                    media_urls=json.dumps(tweet_data.get('media', [])),
                    local_media=json.dumps(tweet_data.get('local_media', [])),
                    analysis_result=json.dumps(event_info),
                    sensitive=self._is_twitter_sensitive(tweet_data),
                    hydrus_expected_count=expected_count,
                    hydrus_imported_count=0
                )
                
                # スペース番号やサークル名を抽出
                from .event_detector import EventDetector
                detector = EventDetector(self.config)
                extracted_info = detector.extract_event_info(tweet_data)
                tweet_record.space_number = extracted_info.get('space_number')
                tweet_record.circle_name = extracted_info.get('circle_name')
                
                session.add(tweet_record)
                saved_count += 1
                
                # all_tweetsのchecked_for_eventフラグをTrueに更新
                all_tweet = session.query(AllTweets).filter(
                    AllTweets.id == tweet_data['id']
                ).first()
                if all_tweet:
                    all_tweet.checked_for_event = True
            
            session.commit()
            self.logger.info(f"Saved {saved_count} event tweets to database")
            
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_event_tweets: {e}")
        finally:
            session.close()
    
    def save_tweets(self, tweets: List[Dict[str, Any]], username: str):
        """互換性のために残す（save_event_tweetsを呼び出す）"""
        self.save_event_tweets(tweets, username)
    
    def get_unnotified_tweets(self, since_date: Optional[datetime] = None) -> List[EventTweet]:
        """未通知のツイートを取得"""
        session = self._get_session()
        
        try:
            query = session.query(EventTweet).filter(
                EventTweet.notified == False
            )
            if since_date is not None:
                query = query.filter(EventTweet.tweet_date >= since_date)

            tweets = query.order_by(EventTweet.tweet_date.desc()).all()
            
            return tweets
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get unnotified tweets: {e}")
            return []
        finally:
            session.close()

    def get_tweets_pending_event_check(
        self,
        limit: int = 200,
        since_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """イベント判定が未完了の監視ツイートを取得"""
        session = self._get_session()

        try:
            query = session.query(AllTweets).filter(
                AllTweets.checked_for_event == False
            )
            if since_date is not None:
                query = query.filter(AllTweets.tweet_date >= since_date)

            tweets = query.order_by(AllTweets.tweet_date.asc()).limit(limit).all()

            result = []
            for tweet in tweets:
                result.append({
                    'id': tweet.id,
                    'username': tweet.username,
                    'display_name': tweet.display_name,
                    'text': tweet.tweet_text,
                    'date': tweet.tweet_date.isoformat(),
                    'url': tweet.tweet_url,
                    'media': json.loads(tweet.media_urls) if tweet.media_urls else [],
                    'local_media': json.loads(tweet.local_media) if tweet.local_media else [],
                    'huggingface_urls': json.loads(tweet.huggingface_urls) if tweet.huggingface_urls else [],
                    'sensitive': bool(tweet.sensitive),
                    'hydrus_expected_count': tweet.hydrus_expected_count or 0,
                    'hydrus_imported_count': tweet.hydrus_imported_count or 0,
                })

            return result
        except (SQLAlchemyError, json.JSONDecodeError) as e:
            self.logger.error(f"Failed to get tweets pending event check: {e}")
            return []
        finally:
            session.close()

    def mark_stale_tweets_checked_for_event(self, before_date: datetime) -> int:
        """古い未判定ツイートをLLM判定せずイベント判定済みにする"""
        session = self._get_session()

        try:
            updated_count = session.query(AllTweets).filter(
                AllTweets.checked_for_event == False,
                AllTweets.tweet_date < before_date,
            ).update(
                {AllTweets.checked_for_event: True},
                synchronize_session=False,
            )
            session.commit()
            return updated_count
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to mark stale tweets as checked_for_event: {e}")
            return 0
        finally:
            session.close()

    def mark_tweets_checked_for_event(self, tweet_ids: List[str]) -> int:
        """指定した監視ツイートをイベント判定済みにする"""
        session = self._get_session()

        try:
            normalized_ids = [str(tweet_id) for tweet_id in tweet_ids if tweet_id]
            if not normalized_ids:
                return 0

            updated_count = session.query(AllTweets).filter(
                AllTweets.id.in_(normalized_ids)
            ).update(
                {AllTweets.checked_for_event: True},
                synchronize_session=False,
            )
            session.commit()
            return updated_count
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to mark tweets as checked_for_event: {e}")
            return 0
        finally:
            session.close()
    
    def get_latest_tweet_date(self, username: str) -> Optional[datetime]:
        """指定ユーザーの最新ツイート日付を取得"""
        session = self._get_session()
        try:
            latest_tweet = session.query(AllTweets).filter(
                AllTweets.username == username
            ).order_by(AllTweets.tweet_date.desc()).first()
            
            if latest_tweet:
                # データベースの日時はタイムゾーンなしなので、UTCとして扱う
                if latest_tweet.tweet_date.tzinfo is None:
                    return latest_tweet.tweet_date.replace(tzinfo=timezone.utc)
                return latest_tweet.tweet_date
            return None
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get latest tweet date for {username}: {e}")
            return None
        finally:
            session.close()
    
    def get_latest_tweet_id(self, username: str) -> Optional[str]:
        """指定ユーザーの最新ツイートIDを取得（all_tweetsとlog_only_tweetsの両方を確認）"""
        session = self._get_session()
        try:
            # all_tweetsテーブルから最新ツイートID取得
            latest_all_tweet = session.query(AllTweets).filter(
                AllTweets.username == username
            ).order_by(AllTweets.tweet_date.desc()).first()

            # log_only_tweetsテーブルから最新ツイートID取得
            latest_log_tweet = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.username == username
            ).order_by(LogOnlyTweet.tweet_date.desc()).first()

            # 両方のテーブルから最新のものを選択
            candidates = []
            if latest_all_tweet:
                candidates.append((latest_all_tweet.tweet_date, latest_all_tweet.id))
            if latest_log_tweet:
                candidates.append((latest_log_tweet.tweet_date, latest_log_tweet.id))

            if candidates:
                # 日付が最新のツイートIDを返す
                latest_candidate = max(candidates, key=lambda x: x[0])
                return latest_candidate[1]

            return None

        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get latest tweet ID for {username}: {e}")
            return None
        finally:
            session.close()

    def check_tweet_exists(self, tweet_id: str) -> bool:
        """指定されたツイートIDがデータベース（all_tweetsまたはlog_only_tweets）に存在するかチェック"""
        session = self._get_session()
        try:
            # all_tweetsを確認
            exists_in_all = session.query(AllTweets.id).filter(AllTweets.id == tweet_id).first() is not None
            if exists_in_all:
                return True
            
            # log_only_tweetsを確認
            exists_in_log = session.query(LogOnlyTweet.id).filter(LogOnlyTweet.id == tweet_id).first() is not None
            return exists_in_log
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to check if tweet {tweet_id} exists: {e}")
            return False
        finally:
            session.close()
    
    def get_existing_tweet_ids(self, username: str) -> set:
        """指定ユーザーの既存ツイートIDセットを取得（重複チェック用）"""
        session = self._get_session()
        try:
            # all_tweetsテーブルから該当ユーザーの全ツイートIDを取得
            tweet_ids = session.query(AllTweets.id).filter(
                AllTweets.username == username
            ).all()
            
            # セットに変換して返す
            return {tweet_id[0] for tweet_id in tweet_ids}
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get existing tweet IDs for {username}: {e}")
            return set()
        finally:
            session.close()
    
    def update_all_tweet_hf_urls(self, tweet_id: str, huggingface_urls: List[str]):
        """all_tweetsテーブルのHugging Face URLsを更新"""
        session = self._get_session()
        
        try:
            tweet = session.query(AllTweets).filter(
                AllTweets.id == tweet_id
            ).first()
            
            if tweet:
                tweet.huggingface_urls = json.dumps(huggingface_urls)
                session.commit()
                self.logger.debug(f"Updated HF URLs for tweet {tweet_id}")
            else:
                self.logger.warning(f"Tweet {tweet_id} not found in all_tweets")
                
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to update HF URLs for tweet {tweet_id}: {e}")
        finally:
            session.close()
    
    def mark_as_notified(self, tweet_id: str):
        """ツイートを通知済みとしてマーク"""
        session = self._get_session()
        
        try:
            tweet = session.query(EventTweet).filter(
                EventTweet.id == tweet_id
            ).first()
            
            if tweet:
                tweet.notified = True
                session.commit()
                self.logger.debug(f"Marked tweet {tweet_id} as notified")
            else:
                self.logger.warning(f"Tweet {tweet_id} not found in database")
                
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to mark tweet as notified: {e}")
        finally:
            session.close()

    def get_pending_hydrus_tweets(self) -> List[Dict[str, Any]]:
        """Hydrus インポートが未完了の監視ツイートを取得"""
        session = self._get_session()

        try:
            tweets = session.query(AllTweets).filter(
                AllTweets.hydrus_expected_count > 0,
                AllTweets.hydrus_imported_count < AllTweets.hydrus_expected_count,
                AllTweets.local_media.isnot(None),
            ).order_by(AllTweets.tweet_date.asc()).all()

            result = []
            for tweet in tweets:
                local_media = json.loads(tweet.local_media) if tweet.local_media else []
                if not local_media:
                    continue

                result.append({
                    'id': tweet.id,
                    'username': tweet.username,
                    'display_name': tweet.display_name,
                    'text': tweet.tweet_text,
                    'date': tweet.tweet_date.isoformat(),
                    'url': tweet.tweet_url,
                    'media': json.loads(tweet.media_urls) if tweet.media_urls else [],
                    'local_media': local_media,
                    'sensitive': bool(tweet.sensitive),
                    'hydrus_expected_count': tweet.hydrus_expected_count or 0,
                    'hydrus_imported_count': tweet.hydrus_imported_count or 0,
                })

            self.logger.info(f"Found {len(result)} tweets pending Hydrus import")
            return result
        except (SQLAlchemyError, json.JSONDecodeError) as e:
            self.logger.error(f"Failed to get pending Hydrus tweets: {e}")
            return []
        finally:
            session.close()

    def is_event_tweet(self, tweet_id: str) -> bool:
        """指定ツイートが event_tweets に存在するか確認"""
        session = self._get_session()

        try:
            return session.query(EventTweet.id).filter(
                EventTweet.id == tweet_id
            ).first() is not None
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to check event_tweet status for {tweet_id}: {e}")
            return False
        finally:
            session.close()
    
    def get_recent_events(self, days: int = 30) -> List[Dict[str, Any]]:
        """最近のイベント情報を取得（統計用）"""
        session = self._get_session()
        
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            tweets = session.query(EventTweet).filter(
                EventTweet.tweet_date >= cutoff_date
            ).order_by(EventTweet.tweet_date.desc()).all()
            
            # 辞書形式に変換
            results = []
            for tweet in tweets:
                results.append({
                    'id': tweet.id,
                    'username': tweet.username,
                    'display_name': tweet.display_name,
                    'text': tweet.tweet_text,
                    'date': tweet.tweet_date.isoformat(),
                    'url': tweet.tweet_url,
                    'event_type': tweet.event_type,
                    'participation_type': tweet.participation_type,
                    'space_number': tweet.space_number,
                    'circle_name': tweet.circle_name
                })
            
            return results
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent events: {e}")
            return []
        finally:
            session.close()
    
    def filter_log_only_tweets(self, tweets: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
        """ログ専用アカウントの新規ツイートのみをフィルタリング"""
        session = self._get_session()
        new_tweets = []

        try:
            if not tweets:
                return []

            incoming_ids = [tweet.get('id') for tweet in tweets]
            existing_meta = self._load_existing_tweet_meta(session, LogOnlyTweet, incoming_ids)
            existing_ids = set(existing_meta.keys())

            # 新規ツイートのみを抽出
            for tweet in tweets:
                tweet_id = tweet.get('id')
                if not tweet_id:
                    self.logger.warning("Log-only tweet without id received; treating as new entry")
                    new_tweets.append(tweet)
                    continue

                if tweet_id not in existing_ids:
                    new_tweets.append(tweet)
                else:
                    owner = existing_meta.get(tweet_id)
                    if owner:
                        self.logger.debug(
                            f"Log-only tweet {tweet_id} already exists in database (originally from @{owner})"
                        )

            self.logger.info(f"Filtered {len(new_tweets)} new log-only tweets out of {len(tweets)} total for @{username}")
            return new_tweets
            
        except SQLAlchemyError as e:
            self.logger.error(f"Database error in filter_log_only_tweets: {e}")
            return tweets  # エラー時は全ツイートを返す（安全側に倒す）
        finally:
            session.close()
    
    def save_single_log_only_tweet(self, tweet_data: Dict[str, Any], username: str) -> bool:
        """単一ツイートをlog_only_tweetsテーブルに保存"""
        session = self._get_session()
        
        try:
            # 既存チェック
            existing = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.id == tweet_data['id']
            ).first()
            
            if existing:
                # 既存ツイートは重複防止による正常な処理
                return True
            
            # 新規レコードを作成
            tweet_record = LogOnlyTweet(
                id=tweet_data['id'],
                username=username,
                display_name=tweet_data.get('display_name', username),
                tweet_text=tweet_data['text'],
                tweet_date=datetime.fromisoformat(tweet_data['date'].replace('Z', '+00:00')),
                tweet_url=tweet_data['url'],
                media_urls=json.dumps(tweet_data.get('media', [])),
                huggingface_urls=json.dumps(tweet_data.get('huggingface_urls', [])),
                sensitive=self._is_twitter_sensitive(tweet_data),
                uploaded_to_hf=tweet_data.get('uploaded_to_hf', False)
            )

            session.add(tweet_record)
            session.commit()
            return True

        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_single_log_only_tweet: {e}")
            return False
        finally:
            session.close()

    def save_log_only_tweets(self, tweets: List[Dict[str, Any]], username: str) -> int:
        """ログ専用ツイートをlog_only_tweetsテーブルに保存"""
        session = self._get_session()
        saved_count = 0
        
        try:
            for tweet_data in tweets:
                # 既存チェック
                existing = session.query(LogOnlyTweet).filter(
                    LogOnlyTweet.id == tweet_data['id']
                ).first()
                
                if existing:
                    continue
                
                # 新規レコードを作成
                tweet_record = LogOnlyTweet(
                    id=tweet_data['id'],
                    username=username,
                    display_name=tweet_data.get('display_name', username),
                    tweet_text=tweet_data['text'],
                    tweet_date=datetime.fromisoformat(tweet_data['date'].replace('Z', '+00:00')),
                    tweet_url=tweet_data['url'],
                    media_urls=json.dumps(tweet_data.get('media', [])),
                    huggingface_urls=json.dumps(tweet_data.get('huggingface_urls', [])),
                    sensitive=self._is_twitter_sensitive(tweet_data),
                    uploaded_to_hf=tweet_data.get('uploaded_to_hf', False)
                )

                session.add(tweet_record)
                saved_count += 1

            session.commit()
            self.logger.info(f"Saved {saved_count} log-only tweets to database")
            return saved_count
            
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_log_only_tweets: {e}")
            return 0
        finally:
            session.close()
    
    def get_tweet_hf_urls(self, tweet_id: str) -> List[str]:
        """all_tweetsテーブルからHugging Face URLを取得"""
        session = self._get_session()
        
        try:
            tweet = session.query(AllTweets).filter(
                AllTweets.id == tweet_id
            ).first()
            
            if tweet and tweet.huggingface_urls:
                return json.loads(tweet.huggingface_urls)
            return []
        except Exception as e:
            self.logger.error(f"Failed to get HF URLs for tweet {tweet_id}: {e}")
            return []
        finally:
            session.close()
    
    def get_log_only_tweet_hf_urls(self, tweet_id: str) -> List[str]:
        """ログ専用ツイートのHugging Face URLを取得"""
        session = self._get_session()
        
        try:
            tweet = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.id == tweet_id
            ).first()
            
            if tweet and tweet.huggingface_urls:
                return json.loads(tweet.huggingface_urls)
            return []
        except Exception as e:
            self.logger.error(f"Failed to get HF URLs for log-only tweet {tweet_id}: {e}")
            return []
        finally:
            session.close()
    
    def update_log_only_tweet_hf_urls(self, tweet_id: str, huggingface_urls: List[str]):
        """ログ専用ツイートのHugging Face URLを更新"""
        session = self._get_session()
        
        try:
            tweet = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.id == tweet_id
            ).first()
            
            if tweet:
                tweet.huggingface_urls = json.dumps(huggingface_urls)
                tweet.uploaded_to_hf = True
                session.commit()
                self.logger.debug(f"Updated HF URLs for log-only tweet {tweet_id}")
            else:
                self.logger.warning(f"Log-only tweet {tweet_id} not found in database")
                
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to update log-only tweet HF URLs: {e}")
        finally:
            session.close()
    
    def get_tweet_count_for_user(self, username: str) -> int:
        """指定ユーザーのツイート数を取得（all_tweetsテーブル）"""
        session = self._get_session()
        try:
            count = session.query(AllTweets).filter(
                AllTweets.username == username
            ).count()
            return count
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get tweet count for {username}: {e}")
            return 0
        finally:
            session.close()
    
    def get_recent_post_count_twitter(self, username: str, days: int = 7) -> int:
        """直近N日間のTwitter投稿数を取得（all_tweets + log_only_tweets合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(AllTweets).filter(
                AllTweets.username == username,
                AllTweets.tweet_date >= cutoff
            ).count()
            count_log = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.username == username,
                LogOnlyTweet.tweet_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for twitter/{username}: {e}")
            return 0
        finally:
            session.close()

    def get_recent_post_count_pixiv(self, user_id: str, days: int = 7) -> int:
        """直近N日間のPixiv投稿数を取得（pixiv_works + pixiv_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(PixivWork).filter(
                PixivWork.user_id == user_id,
                PixivWork.work_date >= cutoff
            ).count()
            count_log = session.query(PixivLogOnlyWork).filter(
                PixivLogOnlyWork.user_id == user_id,
                PixivLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for pixiv/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_recent_post_count_kemono(self, user_id: str, days: int = 7) -> int:
        """直近N日間のKemono投稿数を取得（kemono_works + kemono_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(KemonoWork).filter(
                KemonoWork.user_id == user_id,
                KemonoWork.work_date >= cutoff
            ).count()
            count_log = session.query(KemonoLogOnlyWork).filter(
                KemonoLogOnlyWork.user_id == user_id,
                KemonoLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for kemono/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def has_any_posts(self, identifier: str, platform: str) -> bool:
        """指定アカウントにDB上の投稿が1件でもあるか判定（初回クロール判定用）"""
        session = self._get_session()
        try:
            if platform == 'twitter':
                exists = session.query(AllTweets.id).filter(
                    AllTweets.username == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(LogOnlyTweet.id).filter(
                        LogOnlyTweet.username == identifier
                    ).first() is not None
            elif platform == 'pixiv':
                exists = session.query(PixivWork.id).filter(
                    PixivWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(PixivLogOnlyWork.id).filter(
                        PixivLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'kemono':
                exists = session.query(KemonoWork.id).filter(
                    KemonoWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(KemonoLogOnlyWork.id).filter(
                        KemonoLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'tinami':
                exists = session.query(TinamiWork.id).filter(
                    TinamiWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(TinamiLogOnlyWork.id).filter(
                        TinamiLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'poipiku':
                exists = session.query(PoipikuWork.id).filter(
                    PoipikuWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(PoipikuLogOnlyWork.id).filter(
                        PoipikuLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'fantia':
                exists = session.query(FantiaWork.id).filter(
                    FantiaWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(FantiaLogOnlyWork.id).filter(
                        FantiaLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'nijie':
                exists = session.query(NijieWork.id).filter(
                    NijieWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(NijieLogOnlyWork.id).filter(
                        NijieLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'skeb':
                exists = session.query(SkebWork.id).filter(
                    SkebWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(SkebLogOnlyWork.id).filter(
                        SkebLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'bilibili':
                exists = session.query(BilibiliWork.id).filter(
                    BilibiliWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(BilibiliLogOnlyWork.id).filter(
                        BilibiliLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'misskey':
                exists = session.query(MisskeyWork.id).filter(
                    MisskeyWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(MisskeyLogOnlyWork.id).filter(
                        MisskeyLogOnlyWork.user_id == identifier
                    ).first() is not None
            elif platform == 'fanbox':
                exists = session.query(FanboxWork.id).filter(
                    FanboxWork.user_id == identifier
                ).first() is not None
                if not exists:
                    exists = session.query(FanboxLogOnlyWork.id).filter(
                        FanboxLogOnlyWork.user_id == identifier
                    ).first() is not None
            else:
                return True  # 未対応プラットフォームはDB登録済み扱い
            return exists
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to check posts for {platform}/{identifier}: {e}")
            return True  # エラー時はDB登録済み扱い（通常ソートに回す）
        finally:
            session.close()

    def get_existing_post_ids(self, identifier: str, platform: str) -> set:
        """指定プラットフォーム/アカウントの既存投稿ID集合を返す。"""
        session = self._get_session()
        try:
            monitor_model, log_only_model = self._get_platform_models(platform)
            identity_field = self._get_platform_identity_field(platform)
            existing_ids: set = set()

            for model in (monitor_model, log_only_model):
                column = getattr(model, identity_field)
                rows = session.query(model.id).filter(column == identifier).all()
                existing_ids.update(row[0] for row in rows)

            return existing_ids
        except (SQLAlchemyError, ValueError) as e:
            self.logger.error(f"Failed to get existing post IDs for {platform}/{identifier}: {e}")
            return set()
        finally:
            session.close()

    def get_latest_post_id(self, identifier: str, platform: str) -> Optional[str]:
        """指定プラットフォーム/アカウントの最新投稿IDを返す。"""
        session = self._get_session()
        try:
            monitor_model, log_only_model = self._get_platform_models(platform)
            identity_field = self._get_platform_identity_field(platform)
            date_field = self._get_platform_date_field(platform)
            candidates = []

            for model in (monitor_model, log_only_model):
                identity_column = getattr(model, identity_field)
                date_column = getattr(model, date_field)
                latest = session.query(model).filter(
                    identity_column == identifier
                ).order_by(date_column.desc()).first()
                if latest:
                    candidates.append((getattr(latest, date_field), latest.id))

            if candidates:
                return max(candidates, key=lambda item: item[0])[1]
            return None
        except (SQLAlchemyError, ValueError) as e:
            self.logger.error(f"Failed to get latest post ID for {platform}/{identifier}: {e}")
            return None
        finally:
            session.close()

    @staticmethod
    def _empty_artwork_retry_payload() -> Dict[str, Dict[str, Dict[str, Any]]]:
        return {"monitor": {}, "log_only": {}}

    def _load_artwork_retry_payload(self, raw_payload: Optional[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        payload = self._empty_artwork_retry_payload()
        if not raw_payload:
            return payload
        try:
            loaded = json.loads(raw_payload)
        except json.JSONDecodeError:
            return payload
        if isinstance(loaded, dict):
            for scope in ("monitor", "log_only"):
                bucket = loaded.get(scope)
                if isinstance(bucket, dict):
                    payload[scope] = bucket
        return payload

    @staticmethod
    def _empty_twitter_retry_payload() -> Dict[str, Dict[str, Dict[str, Any]]]:
        return {"monitor": {}, "log_only": {}}

    def _load_twitter_retry_payload(self, raw_payload: Optional[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        payload = self._empty_twitter_retry_payload()
        if not raw_payload:
            return payload
        try:
            loaded = json.loads(raw_payload)
        except json.JSONDecodeError:
            return payload
        if isinstance(loaded, dict):
            for scope in ("monitor", "log_only"):
                bucket = loaded.get(scope)
                if isinstance(bucket, dict):
                    payload[scope] = bucket
        return payload

    def upsert_twitter_retry(
        self,
        username: str,
        tweet: Dict[str, Any],
        *,
        is_log_only: bool = False,
        error: Optional[str] = None,
    ) -> None:
        tweet_id = tweet.get("id")
        if not username or not tweet_id:
            return

        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        try:
            record = session.query(TwitterRetryQueue).filter(
                TwitterRetryQueue.username == username,
            ).first()
            payload = self._load_twitter_retry_payload(record.payload_json if record else None)
            bucket = payload[scope]
            entry = bucket.get(tweet_id, {})
            bucket[tweet_id] = {
                "payload": tweet,
                "retry_count": int(entry.get("retry_count", 0)) + 1,
                "last_error": error,
            }
            raw_payload = json.dumps(payload, ensure_ascii=False, default=str)
            now = datetime.now()
            if record:
                record.payload_json = raw_payload
                record.updated_at = now
            else:
                session.add(
                    TwitterRetryQueue(
                        username=username,
                        payload_json=raw_payload,
                        updated_at=now,
                    )
                )
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to upsert twitter retry for @{username}/{tweet_id}: {e}")
        finally:
            session.close()

    def clear_twitter_retry(
        self,
        username: str,
        tweet_id: str,
        *,
        is_log_only: bool = False,
    ) -> None:
        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        try:
            record = session.query(TwitterRetryQueue).filter(
                TwitterRetryQueue.username == username,
            ).first()
            if not record:
                return
            payload = self._load_twitter_retry_payload(record.payload_json)
            payload[scope].pop(tweet_id, None)
            if not payload["monitor"] and not payload["log_only"]:
                session.delete(record)
            else:
                record.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                record.updated_at = datetime.now()
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to clear twitter retry for @{username}/{tweet_id}: {e}")
        finally:
            session.close()

    def get_twitter_retry_tweets(
        self,
        username: str,
        *,
        is_log_only: bool = False,
    ) -> List[Dict[str, Any]]:
        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        model = LogOnlyTweet if is_log_only else AllTweets
        try:
            record = session.query(TwitterRetryQueue).filter(
                TwitterRetryQueue.username == username,
            ).first()
            if not record:
                return []

            payload = self._load_twitter_retry_payload(record.payload_json)
            bucket = payload[scope]
            if not bucket:
                return []

            max_retry_count = 10
            stale_ids: List[str] = []
            tweets: List[Dict[str, Any]] = []
            for tweet_id, entry in bucket.items():
                exists = session.query(model.id).filter(model.id == tweet_id).first()
                if exists is not None:
                    stale_ids.append(tweet_id)
                    continue
                retry_count = int(entry.get("retry_count", 0))
                if retry_count >= max_retry_count:
                    self.logger.warning(
                        f"Dropping twitter retry @{username}/{tweet_id} after {retry_count} attempts "
                        f"(last error: {entry.get('last_error', 'unknown')})"
                    )
                    stale_ids.append(tweet_id)
                    continue
                tweet_payload = entry.get("payload")
                if isinstance(tweet_payload, dict):
                    tweets.append(tweet_payload)

            if stale_ids:
                for tweet_id in stale_ids:
                    bucket.pop(tweet_id, None)
                if not payload["monitor"] and not payload["log_only"]:
                    session.delete(record)
                else:
                    record.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                    record.updated_at = datetime.now()
                session.commit()

            return tweets
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to get twitter retry tweets for @{username}: {e}")
            return []
        finally:
            session.close()

    def upsert_artwork_retry(
        self,
        platform: str,
        account_id: str,
        work: Dict[str, Any],
        *,
        is_log_only: bool = False,
        error: Optional[str] = None,
    ) -> None:
        work_id = work.get("id")
        if not work_id:
            return

        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        try:
            record = session.query(ArtworkRetryQueue).filter(
                ArtworkRetryQueue.platform == platform,
                ArtworkRetryQueue.account_id == account_id,
            ).first()
            payload = self._load_artwork_retry_payload(record.payload_json if record else None)
            bucket = payload[scope]
            entry = bucket.get(work_id, {})
            bucket[work_id] = {
                "payload": work,
                "retry_count": int(entry.get("retry_count", 0)) + 1,
                "last_error": error,
            }
            raw_payload = json.dumps(payload, ensure_ascii=False, default=str)
            now = datetime.now()
            if record:
                record.payload_json = raw_payload
                record.updated_at = now
            else:
                session.add(
                    ArtworkRetryQueue(
                        platform=platform,
                        account_id=account_id,
                        payload_json=raw_payload,
                        updated_at=now,
                    )
                )
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to upsert artwork retry for {platform}/{account_id}/{work_id}: {e}")
        finally:
            session.close()

    def clear_artwork_retry(
        self,
        platform: str,
        account_id: str,
        work_id: str,
        *,
        is_log_only: bool = False,
    ) -> None:
        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        try:
            record = session.query(ArtworkRetryQueue).filter(
                ArtworkRetryQueue.platform == platform,
                ArtworkRetryQueue.account_id == account_id,
            ).first()
            if not record:
                return
            payload = self._load_artwork_retry_payload(record.payload_json)
            payload[scope].pop(work_id, None)
            if not payload["monitor"] and not payload["log_only"]:
                session.delete(record)
            else:
                record.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                record.updated_at = datetime.now()
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to clear artwork retry for {platform}/{account_id}/{work_id}: {e}")
        finally:
            session.close()

    def get_artwork_retry_works(
        self,
        platform: str,
        account_id: str,
        *,
        is_log_only: bool = False,
    ) -> List[Dict[str, Any]]:
        session = self._get_session()
        scope = "log_only" if is_log_only else "monitor"
        try:
            record = session.query(ArtworkRetryQueue).filter(
                ArtworkRetryQueue.platform == platform,
                ArtworkRetryQueue.account_id == account_id,
            ).first()
            if not record:
                return []

            payload = self._load_artwork_retry_payload(record.payload_json)
            bucket = payload[scope]
            if not bucket:
                return []

            MAX_RETRY_COUNT = 10
            model = self._get_platform_models(platform)[1 if is_log_only else 0]
            stale_ids: List[str] = []
            works: List[Dict[str, Any]] = []
            for work_id, entry in bucket.items():
                exists = session.query(model.id).filter(model.id == work_id).first()
                if exists is not None:
                    stale_ids.append(work_id)
                    continue
                retry_count = int(entry.get("retry_count", 0))
                if retry_count >= MAX_RETRY_COUNT:
                    self.logger.warning(
                        f"Dropping {platform}/{account_id}/{work_id} from retry queue "
                        f"after {retry_count} attempts (last error: {entry.get('last_error', 'unknown')})"
                    )
                    stale_ids.append(work_id)
                    continue
                work_payload = entry.get("payload")
                if isinstance(work_payload, dict):
                    works.append(work_payload)

            if stale_ids:
                for work_id in stale_ids:
                    bucket.pop(work_id, None)
                if not payload["monitor"] and not payload["log_only"]:
                    session.delete(record)
                else:
                    record.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                    record.updated_at = datetime.now()
                session.commit()

            return works
        except (SQLAlchemyError, ValueError) as e:
            session.rollback()
            self.logger.error(f"Failed to get artwork retry works for {platform}/{account_id}: {e}")
            return []
        finally:
            session.close()

    def get_log_only_tweet_count_for_user(self, username: str) -> int:
        """指定ユーザーのログ専用ツイート数を取得（log_only_tweetsテーブル）"""
        session = self._get_session()
        try:
            count = session.query(LogOnlyTweet).filter(
                LogOnlyTweet.username == username
            ).count()
            return count
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get log-only tweet count for {username}: {e}")
            return 0
        finally:
            session.close()
    
    def get_log_only_tweets(self, username: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """ログ専用ツイートを取得"""
        session = self._get_session()
        
        try:
            query = session.query(LogOnlyTweet)
            if username:
                query = query.filter(LogOnlyTweet.username == username)
            
            tweets = query.order_by(LogOnlyTweet.tweet_date.desc()).limit(limit).all()
            
            # 辞書形式に変換
            results = []
            for tweet in tweets:
                results.append({
                    'id': tweet.id,
                    'username': tweet.username,
                    'display_name': tweet.display_name,
                    'text': tweet.tweet_text,
                    'date': tweet.tweet_date.isoformat(),
                    'url': tweet.tweet_url,
                    'media_urls': json.loads(tweet.media_urls) if tweet.media_urls else [],
                    'huggingface_urls': json.loads(tweet.huggingface_urls) if tweet.huggingface_urls else [],
                    'uploaded_to_hf': tweet.uploaded_to_hf
                })
            
            return results
            
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get log-only tweets: {e}")
            return []
        finally:
            session.close()

    @staticmethod
    def _json_loads_list(value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if not value:
            return []
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value or [], ensure_ascii=False)

    @staticmethod
    def _parse_artwork_datetime(value: Any, fallback: Optional[datetime] = None) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                pass
        return fallback or datetime.now()

    @staticmethod
    def _get_artwork_media(work: Dict[str, Any]) -> List[Any]:
        media = work.get('media')
        if media is None:
            media = work.get('media_urls')
        return media if isinstance(media, list) else []

    @staticmethod
    def _model_columns(model: Type[Base]) -> set:
        return set(model.__table__.columns.keys())

    @staticmethod
    def _artwork_identity_alias(platform: str) -> Optional[str]:
        return {
            'fantia': 'fanclub_id',
            'fanbox': 'creator_id',
            'bluesky': 'handle',
        }.get(platform)

    def _artwork_model(self, platform: str, is_log_only: bool = False) -> Type[Base]:
        monitor_model, log_only_model = self._get_platform_models(platform)
        return log_only_model if is_log_only else monitor_model

    def _artwork_default_url(
        self,
        platform: str,
        work: Dict[str, Any],
        identity_value: str,
    ) -> str:
        work_id = work.get('id', '')
        if platform == 'pixiv':
            return f"https://www.pixiv.net/artworks/{work_id}"
        if platform == 'tinami':
            return f"https://www.tinami.com/view/{work_id}"
        if platform == 'poipiku':
            return f"https://poipiku.com/{identity_value}/{work_id}.html"
        if platform == 'fantia':
            return f"https://fantia.jp/posts/{work_id}"
        if platform == 'nijie':
            return f"https://nijie.info/view.php?id={work_id}"
        if platform == 'skeb':
            return f"https://skeb.jp/@{identity_value}"
        if platform == 'bilibili':
            return f"https://www.bilibili.com/opus/{work_id}"
        if platform == 'misskey':
            host = work.get('instance_host', 'misskey.io')
            note_id = work.get('note_id', work_id)
            return f"https://{host}/notes/{note_id}"
        if platform == 'gelbooru':
            return f"https://gelbooru.com/index.php?page=post&s=view&id={work_id}"
        if platform == 'fanbox':
            return f"https://www.fanbox.cc/@{identity_value}/posts/{work_id}"
        if platform == 'bluesky':
            return f"https://bsky.app/profile/{identity_value}/post/{work_id}"
        if platform == 'privatter':
            return f"https://privatter.net/i/{work_id}"
        return ''

    def _artwork_title(self, platform: str, work: Dict[str, Any]) -> str:
        title = work.get('title') or work.get('text') or ''
        if not title and platform == 'gelbooru':
            title = self._json_dumps(work.get('tags', []))
        return title

    @staticmethod
    def _is_artwork_sensitive(work: Dict[str, Any]) -> bool:
        try:
            x_restrict = int(work.get('x_restrict') or 0)
        except (TypeError, ValueError):
            x_restrict = 0
        rating = str(work.get('rating', '')).lower()
        return bool(
            work.get('sensitive')
            or work.get('adult')
            or work.get('has_adult_content')
            or x_restrict >= 1
            or rating in {'sensitive', 'questionable', 'explicit'}
        )

    def _artwork_record_kwargs(
        self,
        platform: str,
        work: Dict[str, Any],
        identity_value: str,
        is_log_only: bool = False,
    ) -> Dict[str, Any]:
        model = self._artwork_model(platform, is_log_only=is_log_only)
        columns = self._model_columns(model)
        identity_field = self._get_platform_identity_field(platform)
        media = self._get_artwork_media(work)
        local_media = work.get('local_media') if isinstance(work.get('local_media'), list) else []
        expected_count = self.estimate_hydrus_expected_count(local_media)

        kwargs: Dict[str, Any] = {
            'id': work['id'],
            identity_field: identity_value,
        }
        if 'display_name' in columns:
            kwargs['display_name'] = work.get('display_name', '')
        if 'title' in columns:
            kwargs['title'] = self._artwork_title(platform, work)
        if 'content' in columns:
            kwargs['content'] = work.get('content') or work.get('text', '')
        if 'work_date' in columns:
            kwargs['work_date'] = self._parse_artwork_datetime(work.get('date'))
        if 'work_url' in columns:
            kwargs['work_url'] = work.get('url') or self._artwork_default_url(
                platform, work, identity_value
            )
        if 'work_type' in columns:
            kwargs['work_type'] = work.get(
                'work_type',
                'illust' if platform == 'pixiv' else ''
            )
        if 'tags' in columns:
            kwargs['tags'] = self._json_dumps(work.get('tags', []))
        if 'page_count' in columns:
            kwargs['page_count'] = work.get('page_count', max(len(media), 1))
        if 'bookmark_count' in columns:
            kwargs['bookmark_count'] = work.get('bookmark_count', 0)
        if 'x_restrict' in columns:
            kwargs['x_restrict'] = work.get('x_restrict', 0)
        if 'service' in columns:
            kwargs['service'] = work.get('service', '')
        if 'file_count' in columns:
            kwargs['file_count'] = work.get('file_count', len(media))
        if 'source_url' in columns:
            kwargs['source_url'] = work.get('source_url', '')
        if 'score' in columns:
            kwargs['score'] = work.get('score', 0)
        if 'rating' in columns:
            kwargs['rating'] = work.get('rating', '')
        if 'sensitive' in columns:
            kwargs['sensitive'] = self._is_artwork_sensitive(work)
        if 'media_urls' in columns:
            kwargs['media_urls'] = self._json_dumps(media)
        if 'local_media' in columns:
            kwargs['local_media'] = self._json_dumps(local_media)
        if 'huggingface_urls' in columns:
            kwargs['huggingface_urls'] = self._json_dumps(work.get('huggingface_urls', []))
        if 'hydrus_expected_count' in columns:
            kwargs['hydrus_expected_count'] = expected_count
        if 'hydrus_imported_count' in columns:
            kwargs['hydrus_imported_count'] = 0
        if 'uploaded_to_hf' in columns:
            kwargs['uploaded_to_hf'] = work.get('uploaded_to_hf', False)
        return kwargs

    def _should_refresh_artwork_media(
        self,
        existing_media_json: Any,
        incoming_media: List[Any],
    ) -> bool:
        existing_media = self._json_loads_list(existing_media_json)
        if not existing_media and incoming_media:
            return True
        return len(incoming_media) > len(existing_media)

    def _update_existing_artwork_record(
        self,
        record: Base,
        platform: str,
        work: Dict[str, Any],
        is_log_only: bool = False,
    ) -> bool:
        columns = self._model_columns(type(record))
        media = self._get_artwork_media(work)
        local_media = work.get('local_media') if isinstance(work.get('local_media'), list) else []
        media_changed = (
            'media_urls' in columns
            and self._should_refresh_artwork_media(record.media_urls, media)
        )
        local_changed = (
            'local_media' in columns
            and local_media
            and local_media != self._json_loads_list(record.local_media)
        )
        if not media_changed and not local_changed:
            return False

        if 'media_urls' in columns and media_changed:
            record.media_urls = self._json_dumps(media)
        if 'local_media' in columns and local_changed:
            record.local_media = self._json_dumps(local_media)
        if 'tags' in columns:
            record.tags = self._json_dumps(work.get('tags', []))
        if 'sensitive' in columns:
            record.sensitive = self._is_artwork_sensitive(work)
        if 'huggingface_urls' in columns and work.get('huggingface_urls'):
            record.huggingface_urls = self._json_dumps(work.get('huggingface_urls', []))
        if 'hydrus_expected_count' in columns:
            record.hydrus_expected_count = self.estimate_hydrus_expected_count(local_media)
        if 'hydrus_imported_count' in columns and not is_log_only:
            record.hydrus_imported_count = 0
        return True

    def _filter_artwork_works(
        self,
        platform: str,
        works: List[Dict[str, Any]],
        identity_value: str,
        is_log_only: bool = False,
    ) -> List[Dict[str, Any]]:
        model = self._artwork_model(platform, is_log_only=is_log_only)
        session = self._get_session()
        try:
            if not works:
                return []

            incoming_ids = [w.get('id') for w in works if w.get('id')]
            if not incoming_ids:
                return works

            existing_media_by_id: Dict[str, Any] = {}
            for chunk in self._chunk_list(incoming_ids, self._ID_BATCH_SIZE):
                rows = session.query(model.id, model.media_urls).filter(model.id.in_(chunk)).all()
                existing_media_by_id.update({row_id: media for row_id, media in rows})

            selected = []
            refetch_count = 0
            increased_count = 0
            for work in works:
                work_id = work.get('id')
                if not work_id:
                    continue
                existing_media = existing_media_by_id.get(work_id)
                if existing_media is None:
                    selected.append(work)
                    continue
                if self._should_refresh_artwork_media(existing_media, self._get_artwork_media(work)):
                    old_count = len(self._json_loads_list(existing_media))
                    new_count = len(self._get_artwork_media(work))
                    if old_count == 0:
                        refetch_count += 1
                    elif new_count > old_count:
                        increased_count += 1
                    selected.append(work)

            label = f"{platform} {'log-only ' if is_log_only else ''}works"
            self.logger.info(
                f"Filtered {len(selected)} new {label} out of {len(works)} for {identity_value}"
                + (f" (including {refetch_count} refetch)" if refetch_count else "")
                + (f" (including {increased_count} media-increased)" if increased_count else "")
            )
            return selected
        except SQLAlchemyError as e:
            self.logger.error(f"Database error in filter_{platform}_works: {e}")
            return works
        finally:
            session.close()

    def _save_artwork_works(
        self,
        platform: str,
        works: List[Dict[str, Any]],
        identity_value: str,
    ) -> int:
        model = self._artwork_model(platform)
        session = self._get_session()
        saved_count = 0
        try:
            for work in works:
                if not work.get('id'):
                    continue
                existing = session.query(model).filter(model.id == work['id']).first()
                if existing:
                    if self._update_existing_artwork_record(existing, platform, work):
                        saved_count += 1
                        self.logger.info(
                            f"Updated {platform} work {work['id']} with refreshed media"
                        )
                    continue

                session.add(model(**self._artwork_record_kwargs(platform, work, identity_value)))
                saved_count += 1

            session.commit()
            self.logger.info(f"Saved {saved_count} {platform} works for {identity_value}")
            return saved_count
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_{platform}_works: {e}")
            return 0
        finally:
            session.close()

    def _save_single_artwork_log_only_work(
        self,
        platform: str,
        work: Dict[str, Any],
        identity_value: str,
    ) -> bool:
        model = self._artwork_model(platform, is_log_only=True)
        session = self._get_session()
        try:
            if not work.get('id'):
                return False
            existing = session.query(model).filter(model.id == work['id']).first()
            if existing:
                changed = self._update_existing_artwork_record(
                    existing, platform, work, is_log_only=True
                )
                if changed:
                    session.commit()
                    self.logger.info(
                        f"Updated {platform} log-only work {work['id']} with refreshed media"
                    )
                return True

            session.add(
                model(**self._artwork_record_kwargs(
                    platform, work, identity_value, is_log_only=True
                ))
            )
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Database error in save_single_{platform}_log_only_work: {e}")
            return False
        finally:
            session.close()

    def _update_artwork_hydrus_import_status(
        self,
        platform: str,
        work_id: str,
        imported_count: int,
        expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        model = self._artwork_model(platform)
        columns = self._model_columns(model)
        if 'hydrus_expected_count' not in columns or 'hydrus_imported_count' not in columns:
            return

        session = self._get_session()
        try:
            work = session.query(model).filter(model.id == work_id).first()
            if work:
                if expected_count is not None:
                    if force:
                        work.hydrus_expected_count = expected_count
                    else:
                        work.hydrus_expected_count = max(
                            work.hydrus_expected_count or 0, expected_count
                        )
                if imported_count is not None:
                    if force:
                        work.hydrus_imported_count = imported_count
                    else:
                        work.hydrus_imported_count = max(
                            work.hydrus_imported_count or 0, imported_count
                        )
                session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(
                f"Failed to update Hydrus status for {platform} work {work_id}: {e}"
            )
        finally:
            session.close()

    def _artwork_record_to_dict(self, platform: str, record: Base) -> Dict[str, Any]:
        identity_field = self._get_platform_identity_field(platform)
        identity_value = getattr(record, identity_field, '')
        tags = self._json_loads_list(getattr(record, 'tags', None))
        media = self._json_loads_list(getattr(record, 'media_urls', None))
        local_media = self._json_loads_list(getattr(record, 'local_media', None))
        date_value = getattr(record, self._get_platform_date_field(platform), None)

        result: Dict[str, Any] = {
            'id': record.id,
            'username': identity_value,
            'display_name': getattr(record, 'display_name', '') or '',
            'title': getattr(record, 'title', '') or '',
            'text': getattr(record, 'title', '') or '',
            'content': getattr(record, 'content', '') or '',
            'date': date_value.isoformat() if isinstance(date_value, datetime) else '',
            'url': getattr(record, 'work_url', '') or '',
            'media': media,
            'media_urls': media,
            'local_media': local_media,
            'huggingface_urls': self._json_loads_list(getattr(record, 'huggingface_urls', None)),
            'tags': tags,
            'custom_tags': [],
            'sensitive': bool(getattr(record, 'sensitive', False)),
            'hydrus_expected_count': getattr(record, 'hydrus_expected_count', 0) or 0,
            'hydrus_imported_count': getattr(record, 'hydrus_imported_count', 0) or 0,
        }

        alias = self._artwork_identity_alias(platform)
        if alias:
            result[alias] = identity_value

        for field in (
            'service', 'source_url', 'score', 'rating', 'work_type',
            'page_count', 'bookmark_count', 'x_restrict', 'file_count',
        ):
            if hasattr(record, field):
                result[field] = getattr(record, field)
        return result

    def get_pending_hydrus_works(self, platform: str) -> List[Dict[str, Any]]:
        model = self._artwork_model(platform)
        columns = self._model_columns(model)
        required = {'hydrus_expected_count', 'hydrus_imported_count', 'local_media'}
        if not required.issubset(columns):
            return []

        date_field = self._get_platform_date_field(platform)
        session = self._get_session()
        try:
            works = session.query(model).filter(
                model.hydrus_expected_count > 0,
                model.hydrus_imported_count < model.hydrus_expected_count,
                model.local_media.isnot(None),
            ).order_by(getattr(model, date_field).asc()).all()
            return [self._artwork_record_to_dict(platform, work) for work in works]
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get pending Hydrus {platform} works: {e}")
            return []
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Pixiv methods
    # ------------------------------------------------------------------

    def filter_new_pixiv_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("pixiv", works, user_id)

    def filter_pixiv_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("pixiv", works, user_id, is_log_only=True)

    def save_pixiv_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("pixiv", works, user_id)

    def save_single_pixiv_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("pixiv", work, user_id)

    def get_existing_pixiv_work_ids(self, user_id: str) -> set:
        """指定ユーザーの既存Pixiv作品IDセットを取得"""
        session = self._get_session()
        try:
            rows = session.query(PixivWork.id).filter(PixivWork.user_id == user_id).all()
            return {r[0] for r in rows}
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get existing Pixiv work IDs for user {user_id}: {e}")
            return set()
        finally:
            session.close()

    def get_latest_pixiv_work_id(self, user_id: str) -> Optional[str]:
        """指定ユーザーの最新Pixiv作品IDを取得"""
        session = self._get_session()
        try:
            latest = session.query(PixivWork).filter(
                PixivWork.user_id == user_id
            ).order_by(PixivWork.work_date.desc()).first()

            latest_log = session.query(PixivLogOnlyWork).filter(
                PixivLogOnlyWork.user_id == user_id
            ).order_by(PixivLogOnlyWork.work_date.desc()).first()

            candidates = []
            if latest:
                candidates.append((latest.work_date, latest.id))
            if latest_log:
                candidates.append((latest_log.work_date, latest_log.id))

            if candidates:
                return max(candidates, key=lambda x: x[0])[1]
            return None

        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get latest Pixiv work ID for user {user_id}: {e}")
            return None
        finally:
            session.close()

    def get_pixiv_work_count_for_user(self, user_id: str) -> int:
        """指定ユーザーのPixiv作品数を取得"""
        session = self._get_session()
        try:
            return session.query(PixivWork).filter(PixivWork.user_id == user_id).count()
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get Pixiv work count for user {user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pixiv_log_only_work_count_for_user(self, user_id: str) -> int:
        """指定ユーザーのPixivログ専用作品数を取得"""
        session = self._get_session()
        try:
            return session.query(PixivLogOnlyWork).filter(PixivLogOnlyWork.user_id == user_id).count()
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get Pixiv log-only work count for user {user_id}: {e}")
            return 0
        finally:
            session.close()

    def update_work_hf_urls(self, platform: str, work_id: str, huggingface_urls: List[str], is_log_only: bool = False):
        """任意プラットフォームの作品テーブルのHugging Face URLsを更新

        Args:
            platform: プラットフォーム名 (e.g. 'pixiv', 'fanbox', 'twitter')
            work_id: 作品/ツイートID
            huggingface_urls: HF URLのリスト
            is_log_only: Trueならlog-onlyテーブルを更新
        """
        models = self.PLATFORM_MODELS.get(platform)
        if not models:
            self.logger.error(f"Unknown platform for HF URL update: {platform}")
            return

        model_class = models[1] if is_log_only else models[0]
        session = self._get_session()
        try:
            work = session.query(model_class).filter(model_class.id == work_id).first()
            if work:
                existing = json.loads(work.huggingface_urls) if work.huggingface_urls else []
                for url in huggingface_urls:
                    if url not in existing:
                        existing.append(url)
                work.huggingface_urls = json.dumps(existing)
                session.commit()

                # Twitterの場合はEventTweetテーブルも同時更新
                if platform == 'twitter' and not is_log_only:
                    event = session.query(EventTweet).filter(EventTweet.id == work_id).first()
                    if event:
                        event_existing = json.loads(event.huggingface_urls) if event.huggingface_urls else []
                        for url in huggingface_urls:
                            if url not in event_existing:
                                event_existing.append(url)
                        event.huggingface_urls = json.dumps(event_existing)
                        session.commit()
            else:
                self.logger.warning(f"{platform} work {work_id} not found in {'log_only' if is_log_only else 'monitor'} table")
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to update HF URLs for {platform} work {work_id}: {e}")
        finally:
            session.close()

    def update_pixiv_work_hf_urls(self, work_id: str, huggingface_urls: List[str]):
        """pixiv_worksテーブルのHugging Face URLsを更新"""
        session = self._get_session()
        try:
            work = session.query(PixivWork).filter(PixivWork.id == work_id).first()
            if work:
                work.huggingface_urls = json.dumps(huggingface_urls)
                session.commit()
            else:
                self.logger.warning(f"Pixiv work {work_id} not found")
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to update HF URLs for Pixiv work {work_id}: {e}")
        finally:
            session.close()

    def update_pixiv_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "pixiv", work_id, imported_count, expected_count, force=force
        )

    # ------------------------------------------------------------------
    # Kemono methods
    # ------------------------------------------------------------------

    def filter_new_kemono_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("kemono", works, user_id)

    def filter_kemono_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("kemono", works, user_id, is_log_only=True)

    def save_kemono_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("kemono", works, user_id)

    def save_single_kemono_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("kemono", work, user_id)

    def update_kemono_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "kemono", work_id, imported_count, expected_count, force=force
        )

    def get_pending_hydrus_kemono_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("kemono")

    def get_pending_hydrus_pixiv_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("pixiv")

    # ------------------------------------------------------------------
    # TINAMI methods
    # ------------------------------------------------------------------

    def filter_new_tinami_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("tinami", works, user_id)

    def filter_tinami_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("tinami", works, user_id, is_log_only=True)

    def save_tinami_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("tinami", works, user_id)

    def save_single_tinami_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("tinami", work, user_id)

    def update_tinami_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "tinami", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_tinami(self, user_id: str, days: int = 7) -> int:
        """直近N日間のTINAMI投稿数を取得（tinami_works + tinami_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(TinamiWork).filter(
                TinamiWork.user_id == user_id,
                TinamiWork.work_date >= cutoff
            ).count()
            count_log = session.query(TinamiLogOnlyWork).filter(
                TinamiLogOnlyWork.user_id == user_id,
                TinamiLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for tinami/{user_id}: {e}")
            return 0
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Poipiku
    # ------------------------------------------------------------------

    def filter_new_poipiku_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("poipiku", works, user_id)

    def filter_poipiku_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("poipiku", works, user_id, is_log_only=True)

    def save_poipiku_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("poipiku", works, user_id)

    def save_single_poipiku_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("poipiku", work, user_id)

    def update_poipiku_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "poipiku", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_poipiku(self, user_id: str, days: int = 7) -> int:
        """直近N日間のPoipiku投稿数を取得（poipiku_works + poipiku_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(PoipikuWork).filter(
                PoipikuWork.user_id == user_id,
                PoipikuWork.work_date >= cutoff
            ).count()
            count_log = session.query(PoipikuLogOnlyWork).filter(
                PoipikuLogOnlyWork.user_id == user_id,
                PoipikuLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for poipiku/{user_id}: {e}")
            return 0
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Fantia methods
    # ------------------------------------------------------------------

    def filter_new_fantia_works(self, works: List[Dict[str, Any]], fanclub_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("fantia", works, fanclub_id)

    def filter_fantia_log_only_works(self, works: List[Dict[str, Any]], fanclub_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("fantia", works, fanclub_id, is_log_only=True)

    def save_fantia_works(self, works: List[Dict[str, Any]], fanclub_id: str) -> int:
        return self._save_artwork_works("fantia", works, fanclub_id)

    def save_single_fantia_log_only_work(self, work: Dict[str, Any], fanclub_id: str) -> bool:
        return self._save_single_artwork_log_only_work("fantia", work, fanclub_id)

    def update_fantia_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "fantia", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_fantia(self, fanclub_id: str, days: int = 7) -> int:
        """直近N日間のFantia投稿数を取得（fantia_works + fantia_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(FantiaWork).filter(
                FantiaWork.user_id == fanclub_id,
                FantiaWork.work_date >= cutoff
            ).count()
            count_log = session.query(FantiaLogOnlyWork).filter(
                FantiaLogOnlyWork.user_id == fanclub_id,
                FantiaLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for fantia/{fanclub_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_fantia_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("fantia")

    # ------------------------------------------------------------------
    # Nijie methods
    # ------------------------------------------------------------------

    def filter_new_nijie_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("nijie", works, user_id)

    def filter_nijie_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("nijie", works, user_id, is_log_only=True)

    def save_nijie_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("nijie", works, user_id)

    def save_single_nijie_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("nijie", work, user_id)

    def update_nijie_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "nijie", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_nijie(self, user_id: str, days: int = 7) -> int:
        """直近N日間のニジエ投稿数を取得（nijie_works + nijie_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(NijieWork).filter(
                NijieWork.user_id == user_id,
                NijieWork.work_date >= cutoff
            ).count()
            count_log = session.query(NijieLogOnlyWork).filter(
                NijieLogOnlyWork.user_id == user_id,
                NijieLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for nijie/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_nijie_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("nijie")

    # ------------------------------------------------------------------
    # Skeb
    # ------------------------------------------------------------------

    def filter_new_skeb_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("skeb", works, user_id)

    def filter_skeb_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("skeb", works, user_id, is_log_only=True)

    def save_skeb_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("skeb", works, user_id)

    def save_single_skeb_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("skeb", work, user_id)

    def update_skeb_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "skeb", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_skeb(self, user_id: str, days: int = 7) -> int:
        """直近N日間のSkeb投稿数を取得（skeb_works + skeb_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(SkebWork).filter(
                SkebWork.user_id == user_id,
                SkebWork.work_date >= cutoff
            ).count()
            count_log = session.query(SkebLogOnlyWork).filter(
                SkebLogOnlyWork.user_id == user_id,
                SkebLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for skeb/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_skeb_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("skeb")

    # ------------------------------------------------------------------
    # bilibili methods
    # ------------------------------------------------------------------

    def filter_new_bilibili_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("bilibili", works, user_id)

    def filter_bilibili_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("bilibili", works, user_id, is_log_only=True)

    def save_bilibili_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("bilibili", works, user_id)

    def save_single_bilibili_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("bilibili", work, user_id)

    def update_bilibili_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "bilibili", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_bilibili(self, user_id: str, days: int = 7) -> int:
        """直近N日間のbilibili投稿数を取得（bilibili_works + bilibili_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(BilibiliWork).filter(
                BilibiliWork.user_id == user_id,
                BilibiliWork.work_date >= cutoff
            ).count()
            count_log = session.query(BilibiliLogOnlyWork).filter(
                BilibiliLogOnlyWork.user_id == user_id,
                BilibiliLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for bilibili/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_bilibili_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("bilibili")

    # ------------------------------------------------------------------
    # Misskey methods
    # ------------------------------------------------------------------

    def filter_new_misskey_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("misskey", works, user_id)

    def filter_misskey_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("misskey", works, user_id, is_log_only=True)

    def save_misskey_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("misskey", works, user_id)

    def save_single_misskey_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("misskey", work, user_id)

    def update_misskey_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "misskey", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_misskey(self, user_id: str, days: int = 7) -> int:
        """直近N日間のMisskeyノート数を取得（misskey_works + misskey_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(MisskeyWork).filter(
                MisskeyWork.user_id == user_id,
                MisskeyWork.work_date >= cutoff
            ).count()
            count_log = session.query(MisskeyLogOnlyWork).filter(
                MisskeyLogOnlyWork.user_id == user_id,
                MisskeyLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for misskey/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_misskey_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("misskey")

    # ------------------------------------------------------------------
    # Gelbooru
    # ------------------------------------------------------------------

    def filter_new_gelbooru_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("gelbooru", works, user_id)

    def filter_gelbooru_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("gelbooru", works, user_id, is_log_only=True)

    def save_gelbooru_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("gelbooru", works, user_id)

    def save_single_gelbooru_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("gelbooru", work, user_id)

    def update_gelbooru_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "gelbooru", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_gelbooru(self, user_id: str, days: int = 7) -> int:
        """直近N日間のGelbooru投稿数を取得（gelbooru_works + gelbooru_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(GelbooruWork).filter(
                GelbooruWork.user_id == user_id,
                GelbooruWork.work_date >= cutoff
            ).count()
            count_log = session.query(GelbooruLogOnlyWork).filter(
                GelbooruLogOnlyWork.user_id == user_id,
                GelbooruLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for gelbooru/{user_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_gelbooru_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("gelbooru")

    # ------------------------------------------------------------------
    # FANBOX
    # ------------------------------------------------------------------

    def filter_new_fanbox_works(self, works: List[Dict[str, Any]], creator_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("fanbox", works, creator_id)

    def filter_fanbox_log_only_works(self, works: List[Dict[str, Any]], creator_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("fanbox", works, creator_id, is_log_only=True)

    def save_fanbox_works(self, works: List[Dict[str, Any]], creator_id: str) -> int:
        return self._save_artwork_works("fanbox", works, creator_id)

    def save_single_fanbox_log_only_work(self, work: Dict[str, Any], creator_id: str) -> bool:
        return self._save_single_artwork_log_only_work("fanbox", work, creator_id)

    def update_fanbox_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "fanbox", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_fanbox(self, creator_id: str, days: int = 7) -> int:
        """直近N日間のFANBOX投稿数を取得（fanbox_works + fanbox_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(FanboxWork).filter(
                FanboxWork.user_id == creator_id,
                FanboxWork.work_date >= cutoff
            ).count()
            count_log = session.query(FanboxLogOnlyWork).filter(
                FanboxLogOnlyWork.user_id == creator_id,
                FanboxLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for fanbox/{creator_id}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_fanbox_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("fanbox")

    def get_low_media_work_ids(self, identifier: str, platform: str, max_count: int = 1) -> List[str]:
        """メディア数が少ない投稿IDを取得（有料化後の自動再チェック用）

        media_urlsのJSON配列要素数が max_count 以下の投稿IDを返す。
        サムネだけDL済みの有料記事を検出するために使用。

        Args:
            identifier: ユーザーID（creator_id / fanclub_id）
            platform: プラットフォーム名（fanbox / fantia）
            max_count: この件数以下を「不完全」とみなす（デフォルト1）

        Returns:
            メディア不完全な投稿IDのリスト
        """
        platform_table_map = {
            "fanbox": (FanboxWork, FanboxLogOnlyWork),
            "fantia": (FantiaWork, FantiaLogOnlyWork),
        }
        if platform not in platform_table_map:
            return []

        monitor_table, log_only_table = platform_table_map[platform]
        session = self._get_session()
        try:
            result_ids: List[str] = []
            for table in (monitor_table, log_only_table):
                rows = session.query(table.id, table.media_urls).filter(
                    table.user_id == identifier
                ).all()
                for row in rows:
                    media_str = row[1] or ''
                    if not media_str or media_str in ('', '[]'):
                        # 空は既存の refetch ロジックで対応済み、ここではスキップ
                        continue
                    try:
                        media_count = len(json.loads(media_str))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if 0 < media_count <= max_count:
                        result_ids.append(row[0])

            if result_ids:
                self.logger.info(
                    f"Found {len(result_ids)} {platform} works with low media count "
                    f"(<= {max_count}) for {identifier}"
                )
            return result_ids

        except SQLAlchemyError as e:
            self.logger.error(f"Database error in get_low_media_work_ids: {e}")
            return []
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Bluesky
    # ------------------------------------------------------------------

    def filter_new_bluesky_works(self, works: List[Dict[str, Any]], handle: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("bluesky", works, handle)

    def filter_bluesky_log_only_works(self, works: List[Dict[str, Any]], handle: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("bluesky", works, handle, is_log_only=True)

    def save_bluesky_works(self, works: List[Dict[str, Any]], handle: str) -> int:
        return self._save_artwork_works("bluesky", works, handle)

    def save_single_bluesky_log_only_work(self, work: Dict[str, Any], handle: str) -> bool:
        return self._save_single_artwork_log_only_work("bluesky", work, handle)

    def update_bluesky_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "bluesky", work_id, imported_count, expected_count, force=force
        )

    def get_recent_post_count_bluesky(self, handle: str, days: int = 7) -> int:
        """直近N日間のBluesky投稿数を取得（bluesky_works + bluesky_log_only_works合算）"""
        session = self._get_session()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            count_all = session.query(BlueskyWork).filter(
                BlueskyWork.user_id == handle,
                BlueskyWork.work_date >= cutoff
            ).count()
            count_log = session.query(BlueskyLogOnlyWork).filter(
                BlueskyLogOnlyWork.user_id == handle,
                BlueskyLogOnlyWork.work_date >= cutoff
            ).count()
            return count_all + count_log
        except SQLAlchemyError as e:
            self.logger.error(f"Failed to get recent post count for bluesky/{handle}: {e}")
            return 0
        finally:
            session.close()

    def get_pending_hydrus_bluesky_works(self) -> List[Dict[str, Any]]:
        return self.get_pending_hydrus_works("bluesky")

    # ------------------------------------------------------------------
    # Privatter
    # ------------------------------------------------------------------

    def filter_new_privatter_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("privatter", works, user_id)

    def filter_privatter_log_only_works(self, works: List[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
        return self._filter_artwork_works("privatter", works, user_id, is_log_only=True)

    def save_privatter_works(self, works: List[Dict[str, Any]], user_id: str) -> int:
        return self._save_artwork_works("privatter", works, user_id)

    def save_single_privatter_log_only_work(self, work: Dict[str, Any], user_id: str) -> bool:
        return self._save_single_artwork_log_only_work("privatter", work, user_id)

    def update_privatter_hydrus_import_status(
        self, work_id: str, imported_count: int, expected_count: Optional[int] = None,
        force: bool = False,
    ) -> None:
        return self._update_artwork_hydrus_import_status(
            "privatter", work_id, imported_count, expected_count, force=force
        )
