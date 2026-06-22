#!/usr/bin/env python3
"""
DB のみアップロードスクリプト — 画像を除いた軽量バックアップ

対象:
  1. EventMonitor DB (data/eventmonitor.db)
  2. Hydrus DB (client.db, client.mappings.db, client.master.db)
  3. creator_mapping.json (DBから自動生成)

画像・動画の全アップロードは数週間かかるため、
DBだけを先行してバックアップしたい場合に使用。

使用方法:
    # DB + creator_mapping をアップロード
    python scripts/maintenance/upload_db_only.py

    # EventMonitor DB のみ
    python scripts/maintenance/upload_db_only.py --skip-hydrus

    # Hydrus DB のみ
    python scripts/maintenance/upload_db_only.py --skip-eventmonitor

    # creator_mapping.json のみ生成・アップロード
    python scripts/maintenance/upload_db_only.py --mapping-only

    # ドライラン
    python scripts/maintenance/upload_db_only.py --dry-run
"""

import sys
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import argparse
from scheduled_backup import ScheduledBackup


def main():
    parser = argparse.ArgumentParser(
        description='DB のみを HuggingFace にアップロード（画像除外）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--skip-hydrus',
        action='store_true',
        help='Hydrus DB のアップロードをスキップ',
    )
    parser.add_argument(
        '--skip-eventmonitor',
        action='store_true',
        help='EventMonitor DB のアップロードをスキップ',
    )
    parser.add_argument(
        '--mapping-only',
        action='store_true',
        help='creator_mapping.json のみ生成・アップロード（DB はスキップ）',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='アップロードせず対象ファイルを確認するだけ',
    )
    parser.add_argument(
        '--config',
        default='config.yaml',
        help='config.yaml のパス（デフォルト: config.yaml）',
    )
    args = parser.parse_args()

    backup = ScheduledBackup(config_path=args.config, dry_run=args.dry_run)

    if args.mapping_only:
        # creator_mapping.json のみ
        backup.generate_creator_mapping()
        return

    # DB ターゲットを構築
    targets = []
    if not args.skip_eventmonitor:
        targets.append('eventmonitor_db')
    if not args.skip_hydrus:
        targets.append('hydrus_db')

    if not targets:
        print("アップロード対象がありません（両方スキップ指定）")
        return

    backup.run(targets=targets)


if __name__ == '__main__':
    main()
