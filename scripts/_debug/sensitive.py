#!/usr/bin/env python3
"""指定URLのセンシティブ判定をgallery-dlで確認"""
import sys, os, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / '.env', override=True)

from src.gallery_dl_cookie_rotator import GalleryDLCookieRotator

wrapper_path = Path(__file__).parent.parent.parent / 'src' / 'gallery_dl_wrapper.py'
rotator = GalleryDLCookieRotator()
cookie_file = rotator.get_next_cookie()
if not cookie_file:
    cookie_file = Path('cookies/x.com_cookies.txt')

# 単一ツイートのメタデータ取得
url = "https://x.com/youyumekun/status/2020948354637930861"
cmd = [
    sys.executable,
    str(wrapper_path),
    '--cookies', str(cookie_file),
    '-q', '-j',
    url
]

print(f"コマンド: {' '.join(cmd)}")
print(f"Cookie: {cookie_file}")
print()

result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
print(f"returncode: {result.returncode}")
if result.stderr:
    print(f"stderr: {result.stderr[:300]}")

output = result.stdout.strip()
if output and output.startswith('['):
    items = json.loads(output)
    for item in items:
        if isinstance(item, list) and len(item) >= 2:
            item_type = item[0]
            item_data = item[1]
            if item_type == 2 and isinstance(item_data, dict):
                print(f"\n=== ツイート情報 (type=2) ===")
                print(f"  tweet_id: {item_data.get('tweet_id')}")
                print(f"  sensitive: {item_data.get('sensitive')}")
                print(f"  sensitive_flags: {item_data.get('sensitive_flags')}")
                print(f"  content: {item_data.get('content', '')[:100]}")
            elif item_type == 3:
                media_data = item[2] if len(item) > 2 else {}
                if isinstance(media_data, dict):
                    print(f"\n=== メディア情報 (type=3) ===")
                    print(f"  tweet_id: {media_data.get('tweet_id')}")
                    print(f"  sensitive_flags: {media_data.get('sensitive_flags')}")
                    print(f"  url: {item[1][:100] if isinstance(item[1], str) else 'N/A'}")
            elif item_type == 1:
                print(f"\n=== ディレクトリ情報 (type=1) ===")
                if isinstance(item_data, dict):
                    print(f"  directory: {item_data}")
else:
    print(f"出力なし or 不正な形式: {output[:200] if output else '(empty)'}")
