# EventMonitor スクリプト使用ガイド

スクリプトはカテゴリ別にサブディレクトリに整理されています。
全スクリプト一覧は `scripts/README.md` を参照してください。

## 目次
- [Hydrus連携 (scripts/hydrus/)](#hydrus連携)
- [HuggingFace関連 (scripts/huggingface/)](#huggingface関連)
- [修正・マイグレーション (scripts/fix/)](#修正マイグレーション)
- [メンテナンス (scripts/maintenance/)](#メンテナンス)
- [ユーティリティ (scripts/util/)](#ユーティリティ)

## Hydrus連携

### hydrus/reimport.py
ツイート画像をHydrus Clientに再インポートするスクリプトです。

```bash
python scripts/hydrus/reimport.py
python scripts/hydrus/reimport.py --username user1 user2
python scripts/hydrus/reimport.py --per-user-limit 300
python scripts/hydrus/reimport.py --event-only
python scripts/hydrus/reimport.py --force-all
```

**用途：**
- Hydrusの設定変更後の再インポート
- 失敗したインポートのやり直し
- 新しいタグルールの適用
- デフォルトは完了済みレコードをスキップ。全件再処理したい場合は `--force-all`。

### hydrus/dedup.py
Hydrus perceptual hash重複検知のスタンドアロン実行。

```bash
python scripts/hydrus/dedup.py [--dry-run] [--hamming N]
```

### hydrus/folder_import.py
外部フォルダからHydrusへの一括インポート。

```bash
python scripts/hydrus/folder_import.py                          # dry-run
python scripts/hydrus/folder_import.py --execute
python scripts/hydrus/folder_import.py --execute --source niconico
python scripts/hydrus/folder_import.py --source hitomi_twitter --user "@suisounobeta"
```

### hydrus/open_creator_page.py / .ahk
選択中画像のcreator:タグで新規検索ページを開く。

```bash
python scripts/hydrus/open_creator_page.py
# AHKホットキー: scripts/hydrus/open_creator_page.ahk をダブルクリックで常駐
```

### hydrus/apply_rank_tags.py / sync_rank_tags.py
Hydrusのrank:タグ一括適用・同期。

```bash
python scripts/hydrus/apply_rank_tags.py [--dry-run] [--platform pixiv] [--reset]
python scripts/hydrus/sync_rank_tags.py [--dry-run] [--platform pixiv]
```

### hydrus/cleanup_non_images.py
Hydrusから非画像ファイルを削除。

```bash
python scripts/hydrus/cleanup_non_images.py          # ドライラン
python scripts/hydrus/cleanup_non_images.py --delete  # 実際に削除
```

## HuggingFace関連

### huggingface/upload.py
メディアファイルをHuggingFaceにアップロード。

```bash
python scripts/huggingface/upload.py
python scripts/huggingface/upload.py username
python scripts/huggingface/upload.py --dry-run
python scripts/huggingface/upload.py --no-encrypt
python scripts/huggingface/upload.py --delete-after
```

### huggingface/check_urls.py
HuggingFaceバックアップ状態の検証。

```bash
python scripts/huggingface/check_urls.py
python scripts/huggingface/check_urls.py --batch-size 200 --delay 0.2
python scripts/huggingface/check_urls.py --resume
python scripts/huggingface/check_urls.py --check-missing
```

### huggingface/fix_structure.py / fix_urls.py
HuggingFaceリポジトリ構造・URL修正。

```bash
python scripts/huggingface/fix_structure.py
python scripts/huggingface/fix_urls.py [--dry-run]
```

## 修正・マイグレーション

### fix/import_times.py (汎用)
複数プラットフォーム対応のHydrus import time修正。

```bash
python scripts/fix/import_times.py --dry-run
python scripts/fix/import_times.py
python scripts/fix/import_times.py --platform pixiv --username 12345678
python scripts/fix/import_times.py --reset
```

### fix/import_times_{platform}.py
プラットフォーム別のimport time修正（twitter, pixiv, kemono, poipiku, nijie）。

```bash
python scripts/fix/import_times_twitter.py [--dry-run] [--username userA] [--limit 100]
python scripts/fix/import_times_pixiv.py [--dry-run] [--username 12345678]
python scripts/fix/import_times_kemono.py [--execute] [--user "fanbox/4894"]
python scripts/fix/import_times_poipiku.py [--dry-run] [--username 123456] [--base-date 2024-01-01]
python scripts/fix/import_times_nijie.py [--dry-run] [--username 12345] [--work-id 552002]
```

### fix/kemono_image_order.py
Kemono作品のfile/attachments順序修正（カバー画像が最後に来る問題）。

```bash
python scripts/fix/kemono_image_order.py --dry-run
python scripts/fix/kemono_image_order.py
python scripts/fix/kemono_image_order.py --update-db    # DB順序も修正
python scripts/fix/kemono_image_order.py --username fanbox/3316400
python scripts/fix/kemono_image_order.py --reset
```

### fix/import_order.py
インポート順序修正（汎用）。

```bash
python scripts/fix/import_order.py                          # dry-run
python scripts/fix/import_order.py --execute
python scripts/fix/import_order.py --execute --source fanbox_hieroglyph
```

### fix/r18_tags.py
Hydrus R-18タグ欠落修正。

```bash
python scripts/fix/r18_tags.py             # dry-run
python scripts/fix/r18_tags.py --execute
```

### fix/reprocess_sensitive.py
センシティブ判定の再処理。

```bash
python scripts/fix/reprocess_sensitive.py --all
python scripts/fix/reprocess_sensitive.py --pixiv-db
python scripts/fix/reprocess_sensitive.py --hydrus-sync [--platform pixiv] [--dry-run]
```

### fix/sync_twitter_account_sensitive.py
Twitter/X account-level `possibly_sensitive` を使って、既存Twitterレコードと Hydrus `rating:r-18` を同期。

```bash
python scripts/fix/sync_twitter_account_sensitive.py --dry-run
python scripts/fix/sync_twitter_account_sensitive.py --username CostRa777
python scripts/fix/sync_twitter_account_sensitive.py --db-only
python scripts/fix/sync_twitter_account_sensitive.py --hydrus-only
```

### fix/missing_local_media.py
local_media欠落の再構築・正規化。

```bash
python scripts/fix/missing_local_media.py --username foo bar
python scripts/fix/missing_local_media.py --limit N
```

### fix/reprocess_kemono_zips.py
Kemono ZIP展開の再処理。

```bash
python scripts/fix/reprocess_kemono_zips.py             # dry-run
python scripts/fix/reprocess_kemono_zips.py --execute
python scripts/fix/reprocess_kemono_zips.py --execute --user "fanbox/4894"
```

### fix/migrate_hydrus_columns.py
DBマイグレーション（Hydrus時刻カラム削除）。

## メンテナンス

### maintenance/scheduled_backup.py
定期バックアップ実行。

```bash
python scripts/maintenance/scheduled_backup.py
python scripts/maintenance/scheduled_backup.py --target crawler_media
python scripts/maintenance/scheduled_backup.py --dry-run
python scripts/maintenance/scheduled_backup.py --delete-after
```

### maintenance/cleanup_downloads.py
失敗したダウンロードファイルのクリーンアップ。

```bash
python scripts/maintenance/cleanup_downloads.py [username]
```

### maintenance/check_media_integrity.py / analyze_logs.py / sync_cookies.py / check_kemono_post.py
各種メンテナンスユーティリティ。

## ユーティリティ

### util/get_usernames.py
DB登録済みユーザー名の取得。

```bash
python scripts/util/get_usernames.py
python scripts/util/get_usernames.py --type normal
python scripts/util/get_usernames.py --type log
```

## 使用上の注意

1. **実行ディレクトリ**: プロジェクトのルートから実行してください。
2. **環境変数**: `.env` ファイルの設定を確認してください。
3. **仮想環境**: `venv` をアクティベートしてから実行してください。
