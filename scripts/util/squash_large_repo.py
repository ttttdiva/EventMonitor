#!/usr/bin/env python3
"""
大きなHuggingFaceリポジトリの履歴を圧縮するスクリプト
"""

from huggingface_hub import HfApi
import os
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('HUGGINGFACE_API_KEY')

if token:
    api = HfApi()
    try:
        print('処理中: disguisequence/CTAI_1 (9.66TB)')
        print('これには時間がかかる可能性があります...')
        result = api.super_squash_history(
            repo_id='disguisequence/EventMonitor_1',
            repo_type='dataset',
            token=token
        )
        print('✓ 履歴をsquashしました')
    except Exception as e:
        print(f'エラー: {e}')
else:
    print('HUGGINGFACE_API_KEYが設定されていません')