import sys
import os
import json
import sqlite3
import yaml
from pathlib import Path

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from src.path_utils import to_absolute_path
except ImportError:
    # Fallback if src import fails (should not happen if path is correct)
    print("Warning: Could not import src.path_utils")
    def to_absolute_path(path, config): return Path(path)

DB_PATH = "data/eventmonitor.db"
CONFIG_PATH = "config.yaml"

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def main():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    print("Loading configuration...")
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading config: {e}")
        return

    print(f"Checking database at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    tables = ['all_tweets', 'event_tweets', 'log_only_tweets']
    
    total_media_files = 0
    missing_media_files = 0
    
    report_lines = []

    for table in tables:
        try:
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if not cursor.fetchone():
                continue
                
            print(f"Scanning table: {table}...")
            # Check for local_media column existing
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [info[1] for info in cursor.fetchall()]
            if 'local_media' not in columns:
                # log_only_tweets might use media_urls or similar if not local_media?
                # logic in database.py says it has media_urls but maybe not local_media column?
                # let's skip if no local_media column
                continue

            cursor.execute(f"SELECT id, username, local_media, tweet_date FROM {table} WHERE local_media IS NOT NULL AND local_media != '[]'")
            rows = cursor.fetchall()
            
            for row in rows:
                tweet_id, username, local_media_json, tweet_date = row
                try:
                    local_media_list = json.loads(local_media_json)
                except:
                    continue

                if not isinstance(local_media_list, list):
                    continue

                for media_path_str in local_media_list:
                    total_media_files += 1
                    
                    # Resolve path using config
                    abs_path = to_absolute_path(media_path_str, config)
                    
                    if not abs_path.exists():
                        missing_media_files += 1
                        # report_lines.append(f"[{table}] Date: {tweet_date} | Tweet {tweet_id}: Missing: {abs_path} (Original: {media_path_str})")

        except Exception as e:
            print(f"Error scanning table {table}: {e}")

    conn.close()

    print("\n" + "="*50)
    print("SCAN RESULTS (with path checking)")
    print("="*50)
    print(f"Total media files referenced: {total_media_files}")
    print(f"Missing media files: {missing_media_files}")
    print("="*50)
    
    if missing_media_files > 0:
        print("\nDetails (First 20):")
        for line in report_lines[:20]:
            print(line)
        
        # Also show most recent missing files to answer user concern
        print("\nMost recent missing files (Last 20):")
        # Sort by date if possible, but they are strings. SQLite date format is usually ISO.
        # Format is roughly: [table] Date: YYYY-MM-DD ...
        report_lines.sort(key=lambda x: x.split('|')[0], reverse=True)
        for line in report_lines[:20]:
            print(line)

if __name__ == "__main__":
    main()
