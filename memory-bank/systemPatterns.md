# systemPatterns.md

## CSV git同期
- `CsvGitSyncer` が `monitored_accounts.csv` / `deleted_accounts.csv` のみを対象に git commit/pull/push する。
- 起動時は Discord ingest と display_name補完後にバックグラウンド同期する。
- 30日超過アーカイブで `deleted_accounts.csv` が更新された場合は追加同期を予約する。
- 同期対象外の作業ツリー差分は commit 対象にしないため、`git commit --only -- <csv>` を使う。
- `data/csv_git_sync.lock` で同時実行を避ける。

## 全体アーキテクチャ
- main.py → EventMonitorクラスが全体フローを統括
- AccountProcessorがアカウント別処理をハンドル
- 監視対象はmonitored_accounts.csvで管理（949件）
- 取得: gallery-dl優先 + twscrape補完（二段構え）
- 保存: ローカル保存 → DB記録 → イベント検出 → 通知 → Hydrusインポート

## アカウントタイプ別フロー
- 通常(監視)アカウント: 取得 → LLM判定 → Discord通知 → Hydrus連携 → ローカル保持
  - notification=空欄: イベント検出OFF（デフォルト、大多数）
  - notification=notice: イベント検出ON
  - rank: 1=最上位, 2=中位, 3=最下位(デフォルト/空欄) → Hydrusに `rank:N` タグ付与
- ログ専用アカウント(account_type=log): 取得 → HuggingFaceへ必ずアップロード → ローカル削除 → 通知/LLM/Hydrusはスキップ

## ツイート取得戦略（二段構え）
1. gallery-dl: メディア付きツイート取得 + ファイルDL（Cookieローテーション対応）
2. twscrape: テキストのみツイート取得（gallery-dlで取れない分を補完）
3. 新着チェック: 最新2件で新着有無を判定 → 新着があれば段階的にフェッチ
4. 鍵アカウント: 指定Cookie/指定twscrapeアカウントで対応

## データ保全/バックアップ
- HuggingFaceバックアップ（rclone暗号化対応、現在disabled）
- 画像/動画のアップロード成功でDBのURLを更新
- 失敗時はretry/reprocess系スクリプトで再処理
- parquetエクスポートとDB丸ごとバックアップ

## DBと永続化
- all_tweets / event_tweets / log_only_tweetsが主要テーブル（Twitter）
- twitter_retry_queue はTwitterメディア取得失敗の永続再試行キュー。monitor/log_only を分けて payload を保持し、通常の新着quick checkやlatest基準に依存せず次回処理へ合流させる。
- pixiv_works / pixiv_log_only_worksがPixivテーブル
- 既存ツイートをフィルタして新規のみ処理（ID重複チェック）
- SQLiteパラメータ上限対策でチャンク処理（900件単位）
- Hydrus管理: hydrus_expected_count / hydrus_imported_count の2カラムで追跡
- カラム自動マイグレーション: `_ensure_hydrus_columns()` で既存DBに不足カラムをALTER TABLE追加

## センシティブコンテンツ標準規約（全プラットフォーム共通）
新しいクローラー/プラットフォームを追加する際は、以下のパターンに従うこと:

### 1. Extractor（取得層）
- プラットフォーム固有のセンシティブ情報を取得し、統一フィールド `sensitive: bool` を導出
- Twitter: `sensitive` + `sensitive_flags` (gallery-dlのJSON出力から)
- Pixiv: `x_restrict >= 1` から `sensitive: True` を導出
- 新規プラットフォーム: 各サービス固有のNSFW/年齢制限フィールドから `sensitive` を導出

### 2. Database（保存層）
- 全テーブルに `sensitive = Column(Boolean, default=False)` カラムを持つ
- `_ensure_hydrus_columns()` のテーブルリストに新テーブルも追加すること
- saveメソッドで `sensitive=bool(data.get('sensitive', False))` を保存

### 3. Hydrus Client（タグ層）
- `sensitive` が True の場合、タグに `rating:r-18` を追加（`_generate_tags()` / `_generate_pixiv_tags()` 等）
- プラットフォーム固有タグ（Pixivの生タグ "R-18" 等）はそのまま保持し、統一タグ `rating:r-18` を**追加**する

## Hydrus タグ名前空間の方針
- `creator:{表示名}`: 人間が読むハンドルネーム専用（1画像に原則1つ）
- `twitter_user:{handle}`: Twitterユーザー名（@以下）
- `pixiv_user:{id}`: Pixivユーザー名/ID（将来追加予定）
- `gelbooru_artist:{tag}` / `danbooru_artist:{tag}`: Booru系のartistタグ。CSV登録済みのID/タグに一致する場合は、`creator:` にはCSVの `display_name` を入れ、artistタグ自体はこの補助namespaceとBooru専用タグサービスへ保持する。
- `gelbooru_query:{tag}`: Gelbooru監視CSVの検索タグ。creatorとは別。
- `{platform}_id:{id}`: 作品/投稿のID（`pixiv_id:`, `tweet_id:`, `kemono_id:` 等）— ユーザーIDとは別
- `source:{platform}`: コンテンツの出典（`source:twitter`, `source:pixiv` 等）
- `rating:r-18`: センシティブコンテンツ
- `rank:{1-3}`: CSV定義のランク
- `title:{text}`: 作品タイトル/ツイート本文
- 既存タグの整理: `scripts/hydrus/cleanup_creator_tags.py`（対話式、dry-run/apply/export）
  - Phase 1.5: URL自動判別フェーズ — known_urlsからTwitterユーザー名を抽出し、一致するcreator:タグを自動でtwitter_user:に移動（対話不要）
- リネームタグ修正: `scripts/fix/fix_renamed_hydrus_tags.py`（ワンショット、recover_renamed_accounts.py実行後のHydrusタグ修正用）

## Hydrus重複検知（perceptual hash）
- クロール後のバッチ後処理として実行（potential discoveryがバックグラウンド非同期のため）
- ポーリングでpotentials_countが安定するまで待機 → ペア一括取得 → 各ペアを処理
- import_timeで新旧判定（古い方を残す）、同一時はファイルサイズ大の方を残す
- set_file_relationships(relationship=4, do_default_content_merge=true, delete_b=true)
- スタンドアロン実行: `python scripts/hydrus/dedup.py [--dry-run] [--hamming N]`

## アカウントID変更（スクリーンネーム変更）追跡
- Twitterの数値ユーザーID（twitter_id）はアカウント生涯不変。usernameは変更可能。
- monitored_accounts.csv に `twitter_id` カラムで数値IDを保存。初回はuser_by_login成功時にキャッシュ→CSVに書き戻し。
- `check_account_reachable()`: user_by_login(username)失敗時、twitter_idがあればuser_by_id()でフォールバック。username変更を検出した場合、`_detected_renames`に蓄積。
- `process_account()`: フラグ済みアカウントのリチェック後に`_detected_renames`を確認し、CSV自動更新（username/display_name/twitter_id）。
- リカバリスクリプト `scripts/fix/recover_renamed_accounts.py`: 既存のflagged/deletedアカウントを一括調査。

## 効率化パターン
- twscrape新着チェック: 既知IDなら即スキップ
- 新着が多い場合は段階取得（20件→無制限→gallery-dl全量）
- gallery-dlタイムアウト300秒 + Cookieローテーション最大3回リトライ
- リツイート/リポスト除外（複数検出方法の組み合わせ）

## 外部連携
- Discord Webhook通知（イベント検出結果のEmbed送信）
- Discord Bot REST API（アカウント追加ingestion + ダッシュボード更新）
- Hydrus Client API（タグ付与/既存スキップ/SHA256重複排除）
- AoiTalk API（ステータスPush通知）
- HuggingFace Hub SDK（メディア/DBバックアップ）

## ステータス通知
- StatusNotifier: Discord + AoiTalkへの一元配信
- ステータス色: 緑=実行中, 黄色=開始, グレー=待機中, 赤=エラー, 青=停止
- レート制限: 60秒最小間隔
- エラーログ: 専用チャンネルに新規投稿

## モンキーパッチ（twitter_monitor.py）
- twscrape xclid: Twitter JS形式変更への対応（get_scripts_listパッチ）
- twscrape models: 200件制限の回避（parse_tweets_unlimited）
- twscrape account: タイムアウト付きAsyncClient
- loguru: twscrapeのERRORログをWARNINGに降格

## 2026-05-04 artwork platform refactor
- Artwork platform entry points are driven by `AccountProcessor.ARTWORK_PLATFORM_SPECS`. When adding a platform, keep extractor/filter/save/Hydrus import/update/unreachable specs aligned with Database and Hydrus config.
- Artwork DB filter/save/log-only save/Hydrus status/pending retrieval should go through `DatabaseManager` common helpers. Platform-specific public methods are compatibility delegates.
- Hydrus artwork tags are generated from `HydrusClient.ARTWORK_TAG_CONFIG` and `_generate_artwork_tags()`. Gelbooru tag-service splitting and FANBOX import-time adjustments remain platform-specific differences.
- twscrape monkey patches are isolated in `src/twscrape_compat.py`; `twitter_monitor.py` imports `apply_twscrape_compat_patches()` and then keeps normal monitor behavior.
