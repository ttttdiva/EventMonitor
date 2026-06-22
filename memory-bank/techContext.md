# techContext.md

## CSV git同期
- `config.yaml` の `csv_git_sync` で `monitored_accounts.csv` / `deleted_accounts.csv` のgit同期を制御する。
- 実装は `src/csv_git_sync.py`。対象CSVのみ commit/pull/push し、`data/csv_git_sync.lock` で多重実行を避ける。

## 技術スタック
- Python 3.11+ (asyncio/await全面採用)
- SQLite + SQLAlchemy ORM（sqlite3 CLIは禁止、pysqlite3-binary利用）
- twscrape 0.17.0 / gallery-dl（ツイート取得の二段構え）
- LLM: OpenAI GPT-5-nano/GPT-5-mini / Gemini 2.5 Flash（CLI + API）
- Discord Webhook + Bot REST API
- HuggingFace Hub SDK（バックアップ）
- Hydrus Client HTTP API
- RClone（暗号化）
- AoiTalk API（ステータスPush）

## プロジェクト構造
```
src/
  twitter_monitor.py           # ツイート取得（twscrape+gallery-dl統合、モンキーパッチ含む）
  gallery_dl_extractor.py      # gallery-dlメディア取得（Twitter）
  pixiv_extractor.py           # gallery-dlメディア取得（Pixiv、OAuth refresh-token）
  kemono_extractor.py          # gallery-dlメディア取得（Kemono）
  fanbox_extractor.py          # gallery-dlメディア取得（FANBOX、Cookie認証）
  fantia_extractor.py          # gallery-dlメディア取得（Fantia、Cookie認証）
  nijie_extractor.py           # gallery-dlメディア取得（Nijie、Cookie認証）
  skeb_extractor.py            # gallery-dlメディア取得（Skeb、Cookie認証）
  misskey_extractor.py         # gallery-dlメディア取得（Misskey、Cookie認証）
  gelbooru_extractor.py        # gallery-dlメディア取得（Gelbooru、タグ検索型）
  bluesky_extractor.py         # gallery-dlメディア取得（Bluesky、認証不要）
  tinami_extractor.py          # カスタムスクレイパー（TINAMI、requests+BS4）
  poipiku_extractor.py         # カスタムスクレイパー（Poipiku、requests+BS4）
  privatter_extractor.py       # カスタムスクレイパー（Privatter、requests+BS4、Cookie認証）
  subprocess_utils.py          # gallery-dlサブプロセス管理（アイドルタイムアウト）
  rate_limit_utils.py          # rate limit検出・待機ユーティリティ
  event_detector.py            # LLMイベント検出（GPT-5/Gemini/Gemini CLI）
  database.py                  # SQLAlchemy ORM（全プラットフォームテーブル）
  discord_notifier.py          # Discord Webhook通知
  backup_manager.py            # HuggingFace/RCloneバックアップ
  hydrus_client.py             # Hydrus Client API連携
  status_notifier.py           # DiscordダッシュボードとAoiTalk Push
  hydrus_dedup.py              # Hydrus perceptual hash重複検知
  gallery_dl_cookie_rotator.py # Cookieローテーション
  gallery_dl_wrapper.py        # gallery-dlラッパー（pysqlite3互換）
  cookie_manager.py            # Cookie管理（cookies/フォルダ統一管理）
  path_utils.py                # パス変換ユーティリティ
  rclone_client.py             # RClone暗号化クライアント
  utils.py                     # 汎用ユーティリティ
  services/
    account_processor.py       # アカウント別処理ロジック（全プラットフォーム統合）
    discord_account_ingest.py  # Discordからのアカウント追加
```

## 主要設定ファイル
- .env: Twitter Cookie/トークン（TWITTER_ACCOUNT_N_TOKEN/CT0）、APIキー、Webhook URL等
- config.yaml: 監視間隔、LLMモデル順、バックアップ、Hydrus、メディア保存先等
- monitored_accounts.csv: 監視対象リスト（username, display_name, notification, account_type, platform, custom_tags, rank, twitter_id）

## 主要コマンド
- 単発実行: `python main.py`
- 常駐: `python main.py --daemon`
- テスト: `pytest tests/unit -v`

## メディア保存先
- 画像: F:/48_EventMonitor_log/images/{username}/
- 動画: F:/48_EventMonitor_log/videos/{username}/
- ログアカウント一時: data/media/（アップロード後削除）

## Cookie管理
- cookies/フォルダで一元管理（twscrapeとgallery-dlで共有）
- ファイル形式: x.com_cookies_N.txt（ゼロパディング対応）
- gallery-dl: CookieRotatorで順番にローテーション
- twscrape: .envのTWITTER_ACCOUNT_N_TOKEN/CT0から初期化
- 鍵アカウント: config.yamlで専用Cookie/アカウント指定

## LLMルーティング
- `config.yaml` の `llm_providers` で CLI/API の実行方法を定義し、`llm_routes` で provider/model/effort と試行順を定義する。
- 既定順は `codex_cli:gpt-5.3-codex-spark:medium` → `gemini_cli:gemini-3-flash-preview` → `codex_cli:gpt-5.5:medium` → `gemini_api:gemini-3-flash-preview`。
- Codex CLI route は `--model <model>` と `-c model_reasoning_effort="<effort>"` を渡すため、Codex 側の対応モデルと effort を route 単位で指定できる。
- 利用可能な Codex CLI モデル/effort は `docs/llm_routing.md` に記録し、必要に応じて `codex debug models` で確認する。
- イベント判定は `all_tweets.checked_for_event=False` をキューとしてバックグラウンド処理し、アカウント巡回を判定完了待ちで止めない。
- `tweet_settings.pending_event_max_age_days`（既定7日）より古い未判定ツイートは、過去backlogとしてLLM判定せず `checked_for_event=True` にする。
- Gemini CLIが `QUOTA_EXHAUSTED` を返した場合は、stderrのリセット時間まで `gemini-cli` をスキップして後続モデルへフォールバックする。

## 診断ツール
- ツイートイベント判定診断: `scripts/util/judge_tweet.py`。任意のテキストを入力してLLM判定結果（JSON）と抽出情報を確認可能。
- Hydrusタグ汚染リセット: `scripts/hydrus/reset_tags_by_url.py`。Pixiv画像などのタグを正しいメタデータで再取得・上書きする。

## 制約/注意点
- DB操作はSQLAlchemy経由のみ（sqlite3 CLI禁止）
- 既存差分を勝手に巻き戻さない
- 破壊的操作は事前承認
- gallery-dlタイムアウト: 300秒（以前は3600秒）
- twscrapeモンキーパッチ: import順序に注意（xclid→API import前にパッチ適用）
