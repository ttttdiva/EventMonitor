#!/usr/bin/env python3
"""
media_urls があるのに local_media が空、または絶対パスで記録されている
レコードを修正するスクリプト。

機能:
1. all_tweets/log_only_tweets から異常レコードを検出
2. 必要に応じてメディアを再ダウンロードした上で local_media を再構築
3. 絶対パスで保存されている local_media を相対パスに正規化
4. Hydrus Client が有効なら再インポート

`--dry-run` を付けると DB 更新やダウンロードを行わず、対象をログ出力のみする。
"""

import sys
from pathlib import Path
import json
import logging
from datetime import datetime
import argparse

# プロジェクトルートを追加
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.database import DatabaseManager, AllTweets
from src.hydrus_client import HydrusClient
from src.path_utils import convert_paths_to_relative, get_media_base_paths
import yaml
import asyncio
import subprocess
import tempfile
import shutil
import os
from dotenv import load_dotenv

# .env を読み込んで Hydrus などの認証情報を利用できるようにする
load_dotenv()

def load_config():
    """設定ファイルを読み込み"""
    config_path = project_root / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def parse_args():
    parser = argparse.ArgumentParser(description="Fix missing/absolute local_media entries")
    parser.add_argument('--dry-run', action='store_true', help='DB更新・ダウンロード・Hydrus連携を行わず対象だけ表示')
    parser.add_argument('--username', nargs='*', help='対象ユーザー (複数可)。未指定で全件')
    parser.add_argument('--limit', type=int, help='欠落レコード処理の件数上限')
    return parser.parse_args()


def construct_local_media_paths(username: str, media_urls, tweet_id: str, config: dict):
    images_base, videos_base = get_media_base_paths(config)
    local_paths = []

    for index, media_url in enumerate(media_urls):
        target_dirs = [(images_base, ['jpg', 'png', 'gif', 'webp'])]
        media_url_lower = (media_url or '').lower()
        if any(token in media_url_lower for token in ['.mp4', '.mov', '.avi', '.webm', '.mkv', 'video.twimg.com']):
            target_dirs = [(videos_base, ['mp4', 'mov', 'avi', 'webm', 'mkv'])]

        matched = False
        if tweet_id:
            for base_dir, extensions in target_dirs:
                for ext in extensions:
                    filename = f"{tweet_id}_{index+1}.{ext}"
                    file_path = base_dir / username / filename
                    if file_path.exists():
                        local_paths.append(str(file_path))
                        matched = True
                        break
                if matched:
                    break

        if matched:
            continue

        filename = media_url.split('/')[-1].split('?')[0] if media_url else ''
        candidates = [filename]
        if '.' in filename:
            stem = filename.rsplit('.', 1)[0]
            candidates.extend([f"{stem}.jpg", f"{stem}.png", f"{stem}.mp4"])

        for base_dir, _ in target_dirs:
            for name in candidates:
                if not name:
                    continue
                candidate = base_dir / username / name
                if candidate.exists():
                    local_paths.append(str(candidate))
                    matched = True
                    break
            if matched:
                break

    return local_paths

async def download_media_for_tweet(tweet, media_urls, config, logger):
    """ツイートのメディアファイルをダウンロード"""
    downloaded_files = []

    try:
        # config.yamlの設定に従ってディレクトリを準備
        media_config = config.get('media_storage', {})
        images_base = Path(media_config.get('images_path', 'images'))
        videos_base = Path(media_config.get('videos_path', 'videos'))

        images_dir = images_base / tweet.username
        videos_dir = videos_base / tweet.username
        images_dir.mkdir(parents=True, exist_ok=True)
        videos_dir.mkdir(parents=True, exist_ok=True)

        # gallery-dlを使用してダウンロード
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # gallery-dlの設定を作成（既存のcookieディレクトリを使用）
            cookies_dir = project_root / 'cookies'
            config_content = f"""
{{
    "extractor": {{
        "twitter": {{
            "cookies": "{cookies_dir / 'x.com_cookies.txt'}"
        }}
    }},
    "output": {{
        "directory": ["{temp_path}"],
        "filename": "{{tweet_id}}_{{num}}_{{filename}}.{{extension}}"
    }}
}}
"""

            config_file = temp_path / 'gallery-dl.conf'
            with open(config_file, 'w') as f:
                f.write(config_content)

            # ツイートURLでダウンロード実行（pysqlite3対応のwrapperを使用）
            tweet_url = tweet.tweet_url
            wrapper_path = project_root / 'src' / 'gallery_dl_wrapper.py'
            cmd = ['python', str(wrapper_path), '--config', str(config_file), tweet_url]

            # コマンド実行（設定ファイルパスを隠す）
            safe_cmd = ['python', str(wrapper_path), '--config', '[config_file]', tweet_url]
            logger.info(f"    Running command: {' '.join(safe_cmd)}")
            # 一時ディレクトリで実行してgitディレクトリを汚染しない（タイムアウト120秒）
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(temp_path))
            logger.info(f"    Command exit code: {result.returncode}")

            if result.stdout:
                logger.info(f"    Command stdout: {result.stdout}")
            if result.stderr:
                logger.info(f"    Command stderr: {result.stderr}")

            if result.returncode == 0:
                # "No results"かどうかチェック
                if "No results" in result.stderr:
                    logger.info(f"    gallery-dl: No results (tweet may be deleted/private)")

                    # 鍵アカウント用Cookieでリトライ
                    private_cookie = config.get('tweet_settings', {}).get('private_account_cookies', {}).get('gallery_dl_cookie')
                    if private_cookie:
                        private_cookie_path = project_root / private_cookie
                        if private_cookie_path.exists() and str(private_cookie_path) != str(cookies_dir / 'x.com_cookies.txt'):
                            logger.info(f"    Retrying with private account cookie: {private_cookie}")

                            # 新しい設定でリトライ
                            retry_config_content = f"""
{{
    "extractor": {{
        "twitter": {{
            "cookies": "{private_cookie_path}"
        }}
    }},
    "output": {{
        "directory": ["{temp_path}"],
        "filename": "{{tweet_id}}_{{num}}_{{filename}}.{{extension}}"
    }}
}}
"""
                            retry_config_file = temp_path / 'gallery-dl-retry.conf'
                            with open(retry_config_file, 'w') as f:
                                f.write(retry_config_content)

                            retry_cmd = ['python', str(wrapper_path), '--config', str(retry_config_file), tweet_url]
                            safe_retry_cmd = ['python', str(wrapper_path), '--config', '[retry_config_file]', tweet_url]
                            logger.info(f"    Running retry command: {' '.join(safe_retry_cmd)}")
                            retry_result = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=120, cwd=str(temp_path))

                            if retry_result.returncode == 0 and "No results" not in retry_result.stderr:
                                logger.info(f"    Retry successful!")
                                # ダウンロードされたファイルを処理
                                gallery_dl_dir = temp_path / 'gallery-dl'
                                if gallery_dl_dir.exists():
                                    for file_path in gallery_dl_dir.rglob('*'):
                                        if file_path.is_file():
                                            if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                                                dest_path = images_dir / file_path.name
                                            else:
                                                dest_path = videos_dir / file_path.name
                                            shutil.move(str(file_path), str(dest_path))
                                            downloaded_files.append(str(dest_path))
                                else:
                                    for file_path in temp_path.rglob('*'):
                                        if file_path.is_file() and not file_path.name.endswith('.conf'):
                                            if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                                                dest_path = images_dir / file_path.name
                                            else:
                                                dest_path = videos_dir / file_path.name
                                            shutil.move(str(file_path), str(dest_path))
                                            downloaded_files.append(str(dest_path))
                                logger.info(f"    Retry downloaded {len(downloaded_files)} files")
                            else:
                                logger.info(f"    Retry also failed")
                else:
                    # 一時ディレクトリ内のgallery-dlディレクトリを探索
                    gallery_dl_dir = temp_path / 'gallery-dl'
                    if gallery_dl_dir.exists():
                        # gallery-dl/twitter/username/ 以下のファイルを移動
                        for file_path in gallery_dl_dir.rglob('*'):
                            if file_path.is_file():
                                # ファイルタイプに基づいて移動先を決定
                                if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                                    dest_path = images_dir / file_path.name
                                else:
                                    dest_path = videos_dir / file_path.name

                                shutil.move(str(file_path), str(dest_path))
                                downloaded_files.append(str(dest_path))
                    else:
                        # gallery-dlディレクトリがない場合は一時ディレクトリ直下を確認
                        for file_path in temp_path.rglob('*'):
                            if file_path.is_file() and not file_path.name.endswith('.conf'):
                                # ファイルタイプに基づいて移動先を決定
                                if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                                    dest_path = images_dir / file_path.name
                                else:
                                    dest_path = videos_dir / file_path.name

                                shutil.move(str(file_path), str(dest_path))
                                downloaded_files.append(str(dest_path))

                logger.info(f"    gallery-dl downloaded {len(downloaded_files)} files")
            else:
                logger.info(f"    gallery-dl failed with code {result.returncode}")
                logger.info(f"    stderr: {result.stderr}")

    except subprocess.TimeoutExpired:
        logger.info(f"    gallery-dl timed out after 120 seconds")
    except Exception as e:
        logger.info(f"    Download error: {e}")
        import traceback
        logger.info(f"    Traceback: {traceback.format_exc()}")

    return downloaded_files

async def fix_missing_local_media(args):
    """media_urls があるのに local_media が欠落/絶対パスのレコードを修正"""

    # ログ設定
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = project_root / f"logs/fix_missing_local_media_{timestamp}.log"
    log_file.parent.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Starting fix_missing_local_media script, log file: {log_file}")

    # 設定とマネージャーを初期化
    config = load_config()
    db_manager = DatabaseManager(config)

    # Hydrus Clientの初期化（監視アカウント用）
    hydrus_client = None
    hydrus_config = config.get('hydrus', {})
    if hydrus_config.get('enabled', False) and not args.dry_run:
        try:
            hydrus_client = HydrusClient(hydrus_config)
            logger.info("✅ Hydrus Client initialized successfully")
        except Exception as e:
            logger.info(f"⚠️  Hydrus Client initialization failed: {e}")
            logger.info("   Continuing without Hydrus integration...")

    session = db_manager._get_session()

    try:
        logger.info("=== Missing local_media Fix Script ===")

        # 1. all_tweetsテーブルの問題レコードを検索
        logger.info("\n1. Checking all_tweets table...")
        all_tweets_query = session.query(AllTweets).filter(
            AllTweets.media_urls.isnot(None),
            AllTweets.media_urls != '',
            AllTweets.media_urls != '[]'
        ).filter(
            (AllTweets.local_media.is_(None)) |
            (AllTweets.local_media == '') |
            (AllTweets.local_media == '[]')
        ).filter(
            (AllTweets.huggingface_urls.is_(None)) |
            (AllTweets.huggingface_urls == '') |
            (AllTweets.huggingface_urls == '[]') |
            (~AllTweets.huggingface_urls.contains('DOWNLOAD_FAILED'))
        )

        if args.username:
            all_tweets_query = all_tweets_query.filter(AllTweets.username.in_(args.username))
        if args.limit:
            all_tweets_query = all_tweets_query.limit(args.limit)

        all_tweets_problematic = all_tweets_query.all()
        logger.info(f"Found {len(all_tweets_problematic)} problematic records in all_tweets")
        if not all_tweets_problematic:
            logger.info("No missing local_media records detected.")

        logger.info("\n2. Checking log_only_tweets table...")
        logger.info("log_only_tweets table does not have local_media column - skipping (this is normal)")

        fixed_all_tweets = 0
        hydrus_imported = 0
        for tweet in all_tweets_problematic:
            try:
                media_urls = json.loads(tweet.media_urls) if tweet.media_urls else []
                if not media_urls:
                    continue

                local_media_paths = construct_local_media_paths(
                    tweet.username, media_urls, tweet.id, config
                )
                existing_files = [path for path in local_media_paths if Path(path).exists()]

                normalized_paths = []
                if existing_files:
                    relative_paths = convert_paths_to_relative(existing_files, config)
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] Would set local_media for tweet {tweet.id}: {relative_paths}")
                    else:
                        tweet.local_media = json.dumps(relative_paths)
                        session.commit()
                    normalized_paths = relative_paths
                    fixed_all_tweets += 1
                    logger.info(f"  Fixed tweet {tweet.id}: found {len(existing_files)} existing files")
                else:
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] Would download media for tweet {tweet.id}")
                        continue

                    logger.info(f"  Tweet {tweet.id}: files not found, attempting download...")
                    downloaded_files = await download_media_for_tweet(tweet, media_urls, config, logger)

                    if downloaded_files:
                        relative_paths = convert_paths_to_relative(downloaded_files, config)
                        tweet.local_media = json.dumps(relative_paths)
                        session.commit()
                        normalized_paths = relative_paths
                        fixed_all_tweets += 1
                        logger.info(f"  Downloaded and fixed tweet {tweet.id}: {len(downloaded_files)} files")
                    else:
                        tweet.huggingface_urls = json.dumps(["DOWNLOAD_FAILED"])
                        session.commit()
                        logger.info(f"  Failed to download files for tweet {tweet.id} - marked as failed")
                        continue

                if hydrus_client and normalized_paths:
                    try:
                        tweet_data = {
                            'id': tweet.id,
                            'username': tweet.username,
                            'display_name': tweet.display_name,
                            'text': tweet.tweet_text,
                            'date': tweet.tweet_date.isoformat(),
                            'url': tweet.tweet_url,
                            'media': media_urls,
                            'local_media': normalized_paths
                        }
                        imported = await hydrus_client.import_tweet_images(tweet_data, normalized_paths)
                        if imported:
                            hydrus_imported += len(imported)
                    except Exception as hydrus_error:
                        logger.info(f"  Hydrus import error for tweet {tweet.id}: {hydrus_error}")

            except Exception as e:
                logger.info(f"  Error fixing tweet {tweet.id}: {e}")
                session.rollback()

        if fixed_all_tweets:
            logger.info(f"\n3. Successfully fixed {fixed_all_tweets} records!")
            if hydrus_imported:
                logger.info(f"   - Hydrus imports: {hydrus_imported} files")
        else:
            logger.info("\n3. No records required reconstruction.")

        # 4. 絶対パスを正規化
        logger.info("\n4. Normalizing absolute local_media paths...")
        absolute_query = session.query(AllTweets).filter(
            AllTweets.local_media.isnot(None),
            AllTweets.local_media != '',
            AllTweets.local_media != '[]'
        )
        if args.username:
            absolute_query = absolute_query.filter(AllTweets.username.in_(args.username))

        absolute_records = []
        for tweet in absolute_query:
            try:
                paths = json.loads(tweet.local_media)
            except Exception:
                continue
            if not paths:
                continue
            has_absolute = any(isinstance(p, str) and (os.path.isabs(p) or p.startswith('\\\\')) for p in paths)
            if has_absolute:
                absolute_records.append((tweet, paths))

        logger.info(f"Found {len(absolute_records)} records with absolute local_media paths")

        normalized_count = 0
        for tweet, paths in absolute_records:
            relative_paths = convert_paths_to_relative(paths, config)
            if relative_paths == paths:
                continue
            if args.dry_run:
                logger.info(f"[DRY-RUN] Would normalize tweet {tweet.id}: {relative_paths}")
                normalized_count += 1
                continue
            tweet.local_media = json.dumps(relative_paths)
            session.commit()
            normalized_count += 1
            if normalized_count % 100 == 0:
                logger.info(f"  Normalized {normalized_count}/{len(absolute_records)} records...")

        logger.info(f"Normalization complete: {normalized_count} record(s) updated")

        # 5. 検証
        logger.info("\n5. Verification...")
        remaining_all = session.query(AllTweets).filter(
            AllTweets.media_urls.isnot(None),
            AllTweets.media_urls != '',
            AllTweets.media_urls != '[]'
        ).filter(
            (AllTweets.local_media.is_(None)) |
            (AllTweets.local_media == '') |
            (AllTweets.local_media == '[]')
        ).filter(
            (AllTweets.huggingface_urls.is_(None)) |
            (AllTweets.huggingface_urls == '') |
            (AllTweets.huggingface_urls == '[]') |
            (~AllTweets.huggingface_urls.contains('DOWNLOAD_FAILED'))
        ).count()

        logger.info(f"Remaining problematic records: all_tweets={remaining_all}")

        if remaining_all == 0:
            logger.info("✅ All problematic records have been fixed!")
        else:
            logger.info(f"⚠️  {remaining_all} records still need attention (files need to be re-downloaded).")

    except Exception as e:
        logger.info(f"Error during fix operation: {e}")
        logger.info("Rolling back database changes...")
        session.rollback()
        raise
    finally:
        session.close()

if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(fix_missing_local_media(cli_args))
