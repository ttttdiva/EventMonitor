# scripts/ ディレクトリ構成

## hydrus/ — Hydrus API操作
| スクリプト | 用途 |
|---|---|
| `reimport.py` | Hydrusへの再インポート（タグ再適用等） |
| `dedup.py` | perceptual hash重複検知（スタンドアロン） |
| `folder_import.py` | 外部フォルダからHydrusへ一括インポート |
| `open_creator_page.py` | 選択中画像のcreator:タグで検索ページを開く |
| `open_creator_page.ahk` | ↑のAHKホットキー（F8） |
| `apply_rank_tags.py` | rank:タグの一括適用 |
| `sync_rank_tags.py` | rank:タグのCSV同期 |
| `cleanup_non_images.py` | 非画像ファイルの削除 |

## huggingface/ — HuggingFaceバックアップ
| スクリプト | 用途 |
|---|---|
| `upload.py` | メディアのHFアップロード |
| `check_urls.py` | バックアップ状態の検証 |
| `fix_structure.py` | リポジトリ構造の修正 |
| `fix_urls.py` | DB内HF URLの修正 |

## fix/ — 一回きりの修正・マイグレーション
| スクリプト | 用途 |
|---|---|
| `import_times.py` | Hydrus import time修正（汎用・複数プラットフォーム対応） |
| `import_times_twitter.py` | Twitter import time修正 |
| `import_times_pixiv.py` | Pixiv import time修正 |
| `import_times_kemono.py` | Kemono ZIP展開画像のimport time修正 |
| `import_times_poipiku.py` | Poipiku import time修正 |
| `import_times_nijie.py` | Nijie import time修正 |
| `kemono_image_order.py` | Kemono file/attachments順序修正（カバー画像問題） |
| `import_order.py` | インポート順序修正（汎用） |
| `r18_tags.py` | R-18タグ欠落修正 |
| `reprocess_sensitive.py` | センシティブ判定の再処理 |
| `sync_twitter_account_sensitive.py` | Twitter account sensitive sync |
| `missing_local_media.py` | local_media欠落の再構築 |
| `reprocess_kemono_zips.py` | Kemono ZIP展開の再処理 |
| `migrate_hydrus_columns.py` | DBマイグレーション（Hydrus時刻カラム削除） |

## maintenance/ — 定期メンテナンス・検証
| スクリプト | 用途 |
|---|---|
| `scheduled_backup.py` | 定期バックアップ実行 |
| `cleanup_downloads.py` | 失敗DLファイルのクリーンアップ |
| `check_media_integrity.py` | メディアファイルの整合性チェック |
| `analyze_logs.py` | 直近ログの分析 |
| `sync_cookies.py` | Cookie→.env同期 |
| `check_kemono_post.py` | Kemono投稿の確認 |

## util/ — ユーティリティ
| スクリプト | 用途 |
|---|---|
| `get_usernames.py` | DB登録済みユーザー名の取得 |
| `search_tweets.py` | DB内ツイート本文の検索（通常はルートの `search.bat` から利用） |
| `squash_large_repo.py` | Gitリポジトリ履歴の圧縮 |

## _debug/ — デバッグ用
| スクリプト | 用途 |
|---|---|
| `check.py` | デバッグ用チェック |
| `sensitive.py` | センシティブ判定のデバッグ |
