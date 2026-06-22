"""
Hydrusにインポート済みの非画像ファイル（PSD, ZIP, 動画等）を検索・削除するスクリプト

使い方:
  python scripts/hydrus/cleanup_non_images.py          # ドライラン（一覧表示のみ）
  python scripts/hydrus/cleanup_non_images.py --delete  # 実際に削除
"""

import asyncio
import aiohttp
import argparse
import os
import sys
import yaml
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / '.env')

# 画像として許可するmimeタイプ
ALLOWED_MIMES = {
    'image/jpeg',
    'image/png',
    'image/webp',
    'image/bmp',
    'image/tiff',
    'image/avif',
    'image/gif',
}


async def main():
    parser = argparse.ArgumentParser(description='Hydrus内の非画像ファイルを検索・削除')
    parser.add_argument('--delete', action='store_true', help='実際に削除する（指定しなければドライラン）')
    args = parser.parse_args()

    # config読み込み
    config_path = project_root / 'config.yaml'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    hydrus_config = config.get('hydrus', {})
    api_url = hydrus_config.get('api_url', 'http://127.0.0.1:45869')
    access_key = os.environ.get('HYDRUS_ACCESS_KEY') or hydrus_config.get('access_key')

    if not access_key:
        print('エラー: HYDRUS_ACCESS_KEY が設定されていません')
        sys.exit(1)

    headers = {'Hydrus-Client-API-Access-Key': access_key}

    async with aiohttp.ClientSession() as session:
        # source:pixiv と source:kemono のファイルを検索
        targets = []
        for source_tag in ['source:pixiv', 'source:kemono']:
            print(f'\n=== {source_tag} のファイルを検索中... ===')

            params = {
                'tags': f'["{source_tag}"]',
                'file_sort_type': 6,  # import time
            }
            async with session.get(f'{api_url}/get_files/search_files', headers=headers, params=params) as resp:
                if resp.status != 200:
                    print(f'  検索失敗: HTTP {resp.status}')
                    continue
                data = await resp.json()
                file_ids = data.get('file_ids', [])
                print(f'  {len(file_ids)} 件のファイルが見つかりました')

            if not file_ids:
                continue

            # メタデータを一括取得（250件ずつ）
            batch_size = 250
            for i in range(0, len(file_ids), batch_size):
                batch = file_ids[i:i + batch_size]
                params = {'file_ids': str(batch)}
                async with session.get(f'{api_url}/get_files/file_metadata', headers=headers, params=params) as resp:
                    if resp.status != 200:
                        print(f'  メタデータ取得失敗: HTTP {resp.status}')
                        continue
                    data = await resp.json()

                for meta in data.get('metadata', []):
                    mime = meta.get('mime', '')
                    if mime not in ALLOWED_MIMES:
                        targets.append({
                            'file_id': meta.get('file_id'),
                            'hash': meta.get('hash'),
                            'mime': mime,
                            'size': meta.get('size', 0),
                            'source': source_tag,
                        })

        # 結果表示
        print(f'\n=== 非画像ファイル: {len(targets)} 件 ===')
        if not targets:
            print('対象ファイルはありませんでした。')
            return

        for t in targets:
            size_kb = t['size'] / 1024
            print(f"  [{t['source']}] {t['hash'][:12]}... | {t['mime']} | {size_kb:.1f} KB")

        if not args.delete:
            print(f'\nドライランモードです。実際に削除するには --delete を付けて実行してください。')
            return

        # 削除実行
        print(f'\n{len(targets)} 件を削除します...')
        hashes = [t['hash'] for t in targets]
        payload = {
            'hashes': hashes,
            'reason': 'cleanup: non-image files imported by mistake',
        }
        async with session.post(f'{api_url}/add_files/delete_files', headers=headers, json=payload) as resp:
            if resp.status == 200:
                print(f'削除完了: {len(hashes)} 件')
            else:
                text = await resp.text()
                print(f'削除失敗: HTTP {resp.status} - {text}')


if __name__ == '__main__':
    asyncio.run(main())
