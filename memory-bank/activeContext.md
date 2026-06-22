# activeContext.md

## Recent work (2026-06-20)
- Kemono CDN outage preview-only mode:
  - Investigation of the active crawl log showed `fanbox/1184461` repeatedly spending minutes per work on `incomplete download (0/N)` while `kemono.cr` metadata stayed reachable and original CDN nodes were timing out.
  - Added a per-cycle Kemono CDN outage flag. Once all configured original CDN fallback hosts are marked unavailable, subsequent Kemono media downloads in the same crawl cycle skip gallery-dl/full CDN attempts and go directly to preview fallback.
  - `clear_reachability_cache()` now resets both the CDN failed-host cache and outage flag, so later cycles can return to full-resolution downloads after the CDN recovers.
  - Preview fallback now has separate shorter timeouts (`preview_fallback_connect_timeout`, `preview_fallback_read_timeout`) because thumbnail responses should fail fast compared with original media downloads.
- Verification:
  - `py_compile src/kemono_extractor.py tests/unit/test_kemono_cdn_fallback.py`: OK
  - `config.yaml` YAML parse: OK
  - `pytest tests/unit/test_kemono_cdn_fallback.py -q`: 5 passed
  - `pytest tests/unit/test_artwork_incremental.py tests/unit/test_kemono_cdn_fallback.py -q`: 12 passed
  - `run_tests.py --quick`: 99 passed, 1 warning

## Recent work (2026-06-18)
- Kemono preview fallback:
  - Investigation confirmed `kemono.cr` frontend/API and `img.kemono.cr` thumbnail delivery can be alive while original storage nodes `n1` through `n4` time out.
  - Added a final Kemono preview fallback after original gallery-dl and direct CDN fallback fail for missing hashes.
  - Preview fallback tries `img.kemono.cr/thumbnail/data/...` then `kemono.cr/thumbnail/data/...`, accepts only HTTP-success image content that passes Pillow validation, and names files with `_preview` so reduced-quality substitutes are visible.
  - Original SHA256 validation remains unchanged for full-resolution files; preview files are added only after original validation and are ordered back into the original media sequence by fallback index.
- Verification:
  - `py_compile src/kemono_extractor.py tests/unit/test_kemono_cdn_fallback.py`: OK
  - `pytest tests/unit/test_kemono_cdn_fallback.py -q`: 3 passed
  - `pytest tests/unit/test_artwork_incremental.py tests/unit/test_kemono_cdn_fallback.py -q`: 10 passed
  - `run_tests.py --quick`: 97 passed
  - Live thumbnail probe for `fanbox/1184461/post/687044` cover hash via `img.kemono.cr/thumbnail/data/...`: OK, 58,888 bytes

## Recent work (2026-06-17)
- Event detection keyword gate update:
  - Investigation for `mac_4229/status/2059414111243649416` showed the tweet was saved in `all_tweets` and marked `checked_for_event=True`, but never inserted into `event_tweets`.
  - Root cause was the pre-LLM `_quick_keyword_check`: the text `声音7次会申し込み済み` did not contain any existing event keywords, so LLM analysis and Discord notification were never reached.
  - Added `声音`, `申し込み`, `申込`, and `出ます` to `config.yaml` event keywords.
  - Added a unit regression test using the missed tweet text and the follow-up `声音7次会確定みたいなので改めて出ます` text.
  - Existing missed tweets already marked `checked_for_event=True` still need explicit reprocessing/reset if notification is required.
- Verification:
  - `py_compile tests/unit/test_event_keywords_config.py src/event_detector.py`: OK
  - `pytest tests/unit/test_event_keywords_config.py tests/unit/test_event_detector_cli_fallback.py -q`: 5 passed
  - DB-backed quick keyword check: `2059414111243649416` matched `声音`/`申し込み`; `2067096713484272120` matched `声音`/`出ます`
  - `run_tests.py --quick`: 96 passed

## Recent work (2026-06-16)
- Kemono CDN fallback:
  - `fanbox/1184461` logs showed repeated `Failed to download media for 1 kemono works` / `incomplete download (0/N)` while metadata fetch still succeeded.
  - Direct checks showed `kemono.cr/data/...` redirected to `n1.kemono.cr`, and `n1` through `n4` timed out from the current environment at that moment.
  - Added a Kemono-only fallback after gallery-dl download collection: missing `/data/...` files are retried directly against configured CDN hosts (`n2`, `n3`, `n4`, `n1` by default).
  - Fallback downloads validate SHA256 hashes from Kemono metadata before accepting files, cache failed CDN hosts for a cooldown window, and keep failed works in the existing artwork retry queue.
  - Extended Kemono gallery-dl stderr logging to keep the tail of long stderr snippets so future CDN errors are diagnosable.
- Verification:
  - `py_compile src/kemono_extractor.py tests/unit/test_kemono_cdn_fallback.py`: OK
  - `pytest tests/unit/test_artwork_incremental.py tests/unit/test_kemono_cdn_fallback.py -q`: 9 passed
  - `run_tests.py --quick`: 95 passed

## Recent work (2026-06-15)
- Browser extension Pixiv manual import time write removal:
  - Pixiv manual import keeps sequential Hydrus imports (`PIXIV_IMPORT_CONCURRENCY = 1`) for page-order stability during fresh imports.
  - Removed the Hydrus `edit_times/set_time` path from the extension so manual Pixiv imports no longer overwrite Hydrus file import time with Pixiv `uploadDate/createDate`.
  - Removed Pixiv `uploadDate/createDate` transfer from the content script and removed the now-unused extension `setFileImportTime` helper.
  - Built `extension/dist` and copied it to `C:\tool\Edge_Extension\Hydrus Importer\`.
- Verification:
  - `npm run build` in `extension/`: OK
  - Static `rg` check across `extension/src`, `extension/dist`, and `C:\tool\Edge_Extension\Hydrus Importer\`: no `setFileImportTime`, `edit_times/set_time`, `timestamp_type`, `pixivImportTimestamp`, `uploadDate`, or `createDate` remained.

## Recent work (2026-06-12)
- bilibili 動態(opus) クローラーを新規追加（add-crawler ガイド準拠）:
  - `src/bilibili_extractor.py` を新規作成。取得は2段構え。
    1. web-dynamic feed API (`opus/feed/space`) で opus_id 一覧を高速取得（Cookie不要）。`check_new_post_ids` はこのAPIだけで新着判定する。
    2. gallery-dl (1.31.1, BilibiliArticleExtractor対応済み) で各 opus 詳細（投稿日時 `pub_ts`・全画像・作者名）を取得・DL。
  - CSV username は数値ユーザーID(mid、生涯不変)。`bilibili_user:{mid}` タグ。本文は `module_content` 段落から抽出（`basic.title` は汎用ページタイトルなのでフォールバック）。
  - **sensitive は常に False**（bilibili に NSFW フラグは存在しない。Kemono の always_r18 とは逆）。
  - 投稿日時が取れるので account_processor の `_sort_artworks_oldest_first` (date, id) で古い順インポート。
  - account_processor の FANBOX 専用だった「軽量API新着チェック → 新着のみ詳細取得」高速パスを、`hasattr(extractor, "check_new_post_ids")` 判定へ一般化し bilibili も利用。
  - Discord ingest: `space.bilibili.com/{mid}` は mid 直接、`bilibili.com/opus/{id}` / `t.bilibili.com/{id}` / `m.bilibili.com/opus/{id}` は `opus:{id}` 仮IDとして登録し `_resolve_bilibili_opus_accounts` が mid に解決（Pixiv artwork と同パターン）。`/video/BV...` は無視。
  - config.yaml に `bilibili` セクション追加（enabled, max_batch_size, feed_max_pages）。Cookie は任意（`cookies/bilibili.com_cookies.txt` があればリスクコントロール緩和に使用）。
- Verification（全て実APIで確認）:
  - feed API / reachability / display_name 解決 / opus詳細(date=2026-05-14, media, mid) / 実DL(images/bilibili_289132019/) / Discord URL抽出4形式 / Hydrusタグ生成 / DB save-filter-incremental round-trip: OK
  - `py_compile` 全変更ファイル / config.yaml YAML: OK
  - `run_tests.py --quick`: 93 passed（回帰なし）

## Recent work (2026-06-11)
- twscrape IndexError 全アカウントロックアウトの解消:
  - 原因は X が 2026-06 にホームページを新 `/x-web/x-web/assets/*.js` 構成へ変更し、`x-client-transaction-id` 生成用の `ondemand.s` スクリプト参照がページから消えたこと (vladkens/twscrape#312)。twscrape 0.17.0 の `get_scripts_list` が IndexError になり、リトライ3回ごとにアカウントが15分ロックされていた。
  - twscrape を 0.18.1 へ更新（0.18.0 が 2026-05 の X 変更と JS バンドルパースを修正済み）。ただし 0.18.1 のパーサーも 2026-06 の新構成には未対応。
  - `src/twscrape_compat.py` の `_patch_xclid_parser` を書き直し、本家 `parse_anim_idx(text, clt)` が失敗した場合だけ既知の `ondemand.s.c86191da.js` URL へフォールバックする方式に変更（URL は `EVENTMONITOR_XCLID_ONDEMAND_URL` で上書き可）。本家が修正されれば素通りになる。
  - `_patch_account_client_timeout` は make_client 全体の再実装をやめ、本家実装を呼んでから timeout(180s/connect 30s) を設定するラッパーへ簡素化（0.18 の parse_proxy 対応を壊さないため）。
  - twscrape 0.18.0 で `user_by_id` API が削除されたため（X 側エンドポイント廃止）、`twitter_monitor.py` の ID 変更検出フォールバックを `hasattr` ガード付きに変更。現行バージョンでは ID 変更検出は機能しない。
  - `twscrape reset_locks` で全14アカウントのロックを解除。
- Verification:
  - `XClIdGen.create()` + 実APIで `user_by_login("tesla")` / `user_tweets` 5件取得: OK（フォールバック経由）
  - `py_compile src/twscrape_compat.py src/twitter_monitor.py`: OK
  - `run_tests.py --quick`: 全件成功

## Recent work (2026-06-09)
- Browser extension Pixiv manual import order/tag-service fix:
  - Pixiv manual import now runs Hydrus file imports sequentially and sets Hydrus file import time to Pixiv `uploadDate/createDate + pageIndex`, preserving artwork/page order even for existing files.
  - Extension tag-service defaults now use existing Hydrus services: Twitter/Pixiv/Bluesky -> `my tags`, Danbooru/Gelbooru -> `danbooru tags`.
  - Stored legacy defaults (`pixiv tags`, `twitter tags`, `bluesky tags`, `gelbooru tags`) are normalized at runtime so old extension settings stop routing Pixiv tags to Hydrus's first local tag service.
  - Hydrus tag-service resolution no longer silently falls back to the first local tag service when a configured service is missing; imports now fail visibly on missing tag services instead of contaminating another service.
  - Built `extension/dist` and copied it to `C:\tool\Edge_Extension\Hydrus Importer\` after removing old extension files while preserving the target `.git` directory.
- Verification:
  - `npm run build` in `extension/`: OK
  - Static check of `extension/dist` and `C:\tool\Edge_Extension\Hydrus Importer\`: updated defaults, no fallback string, Pixiv `uploadDate`, and `edit_times/set_time` present.

## Recent work (2026-05-17)
- Twitter incomplete media retry fix:
  - Twitter media download failures now persist failed tweet payloads in `twitter_retry_queue` instead of relying on the next normal quick-check window.
  - Normal monitor and log-only Twitter processing load queued retry tweets before/alongside fresh fetch results, so retry is attempted even when the account's latest saved tweet has advanced and the normal fetch returns no new tweets.
  - Successful save clears the matching retry entry; retry entries are scoped separately for monitor vs log-only.
  - Log-only Twitter media download now targets any tweet with media/videos, not only `source == "gallery-dl"`, so twscrape media tweets no longer become permanent incomplete skips.
- Verification:
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\database.py src\services\account_processor.py tests\unit\test_twitter_resume_safety.py tests\unit\test_database_filters.py`: OK
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_twitter_resume_safety.py tests\unit\test_database_filters.py -q`: 16 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 90 passed

## Recent work (2026-05-16)
- Discord ingest display_name registration:
  - Discord ingest now resolves platform display names before appending new rows to `monitored_accounts.csv` instead of writing blank names and relying on the startup-wide missing-name pass.
  - `EventMonitor` constructs platform extractors before `DiscordAccountIngestor` and passes available `resolve_display_name` callbacks into ingest.
  - FANBOX display name resolution now uses the lightweight `post.listCreator` API with a 15s HTTP timeout instead of launching `gallery-dl --range 1-1`, avoiding startup stalls before platform groups begin.
  - If a resolver is unavailable or fails, ingest uses the account identifier as `display_name` so CSV registration remains complete and the run can continue.
- Verification:
  - Target pytest for Discord ingest/FANBOX display names: OK
  - `compileall main.py src tests/unit/test_discord_ingest_display_name.py`: OK
  - `run_tests.py --quick`: 86 passed

## Recent work (2026-05-16)
- Event detection LLM routing refactor in progress:
  - `models` / `gemini_cli` / `codex_cli` legacy settings are being replaced with `llm_providers` and ordered `llm_routes`.
  - Target default route order: Codex CLI `gpt-5.3-codex-spark` medium → Gemini CLI `gemini-3-flash-preview` → Codex CLI `gpt-5.5` medium → Gemini API `gemini-3-flash-preview`.
  - Codex CLI reasoning effort is route-level config and is passed through `-c model_reasoning_effort="..."`.
  - Available Codex CLI models/efforts are documented in `docs/llm_routing.md` rather than kept as long config comments.

## Recent work (2026-05-09)
- Browser extension Pixiv manual import:
  - Pixiv content script no longer sends all downloaded page images in one `runtime.sendMessage` payload.
  - Each Pixiv page image is downloaded on the Pixiv page, split into 4 MiB Base64 chunks, transferred to the background service worker, reassembled there, and imported one page at a time.
  - This avoids Chrome's 64 MiB message limit for multi-page or large Pixiv works while preserving Pixiv Referer-based downloads.
- Verification:
  - `npm run build` in `extension/`: OK

## Recent work (2026-05-05)
- Booru creator tag cleanup/prevention:
  - Gelbooru Hydrus tag generation now resolves `tags_artist` against `monitored_accounts.csv` / loaded `monitored_accounts`; matched IDs/tags use CSV `display_name` for `creator:` and keep the raw artist under `gelbooru_artist:` plus the Booru tag service.
  - Unknown Gelbooru artist tags still produce `creator:{artist}` so valid Booru creator tagging is preserved.
  - Gelbooru monitor query tags are recorded as `gelbooru_query:` instead of being treated as creator data.
  - Manual Booru/folder imports now also preserve raw artist tags under `{platform}_artist:`.
  - Ran the one-shot Gelbooru creator cleanup against Hydrus: 105 `creator:{raw artist}` tags were removed, matching `gelbooru_artist:{raw artist}` tags were added, and follow-up dry-run reported 0 remaining planned changes. The temporary script was removed after execution.

## Recent work (2026-05-04)
- Artwork platform refactor:
  - `AccountProcessor.ARTWORK_PLATFORM_SPECS` now drives process dispatch and Hydrus retry. Platform-specific `_process_*_account` methods are compatibility delegates only.
  - `retry_pending_hydrus_imports()` now uses a shared loop with `get_pending_hydrus_works(platform)` first, so skeb / misskey / gelbooru / bluesky are covered by platform specs.
  - `DatabaseManager` now has common artwork helpers for filter/save/log-only save/Hydrus status/pending retrieval while keeping existing public platform method names as delegates.
  - `HydrusClient` now uses a shared artwork import loop and `ARTWORK_TAG_CONFIG` tag generation. Gelbooru tag-service splitting and FANBOX import-time adjustments remain platform-specific.
  - twscrape monkey patches moved to `src/twscrape_compat.py`; `twitter_monitor.py` applies the compat module and keeps monitor logic focused.
- Verification:
  - target pytest: 40 passed
  - `run_tests.py --quick`: 81 passed
  - `compileall src scripts`: OK
  - `run_tests.py --lint`: not executed because flake8 / black / isort are not installed

## 直近の作業（2026-05-03）
- **イベント判定の古い未判定backlog抑止 + Gemini CLI quota対応**:
  - `all_tweets.checked_for_event=False` の既存backlogを無制限にLLM判定していたため、`tweet_settings.pending_event_max_age_days` を追加し、既定7日より古い未判定ツイートはLLMへ投げず判定済みにする方針へ変更。
  - ローカルDB確認では `event_tweets` は3128件あり初期化ではなかった一方、未判定 `all_tweets` が372450件、うち365144件が7日超過だった。
  - Gemini CLIの `QUOTA_EXHAUSTED` stderrからリセット時刻を読み、期限まで `gemini-cli` をスキップして `codex-cli` 以降へ直接フォールバックするようにした。

- **Codex CLIイベント判定のJSON安定化**:
  - Codex CLIへの判定プロンプトをコマンドライン引数ではなくstdinで渡すよう変更。
  - `codex exec --output-schema` 用の一時JSON Schemaを生成し、最終応答をイベント判定JSONに制約。
  - 実Codex CLIで短いイベント告知サンプルがJSON判定されることを確認。

## 直近の作業（2026-05-02）
- **イベント判定のバックグラウンド化 + API最終フォールバック復帰**:
  - `config.yaml` の既定モデル順を `gemini-cli` → `codex-cli` → `gpt-5-nano` に変更。
  - Twitter巡回中はイベント判定を待たず、`all_tweets.checked_for_event=False` をバックグラウンド処理する方針へ変更。
  - CLI/API判定は巡回を止めにくくし、未通知イベントは既存のresume処理で再送可能にする。

## 直近の作業（2026-05-01）
- **イベント判定LLMフォールバック順の課金抑制対応**:
  - `config.yaml` の既定モデル順を `gemini-cli` → `codex-cli` に変更。
  - Gemini/OpenAI APIモデルは既定の `models` から外し、APIキー課金を避ける方針に変更。
  - `src/event_detector.py` に Codex CLI フォールバックを追加。`codex exec --model gpt-5.3-codex-spark` を既定とし、レート制限時は `codex_cli.fallback_model` の `gpt-5.4` で再試行する。

## 直近の作業（2026-04-25）
- **CSV git同期機能の追加**:
  - `src/csv_git_sync.py` を追加。`monitored_accounts.csv` / `deleted_accounts.csv` の差分だけを `git add` → `git commit --only` → `git pull --rebase --autostash` → `git push` する。
  - `main.py` は Discord ingest と display_name補完後に起動時同期をバックグラウンド起動し、30日超過アーカイブ後にも同期を予約する。
  - 同期中に追加同期要求が来た場合は pending として保持し、完了後にもう一度同期する。
  - `config.yaml` に `csv_git_sync` セクションを追加し、有効化済み。

## 直近の作業（2026-04-24）
- **ツイートイベント判定診断ツールの作成**:
  - `scripts/util/judge_tweet.py` を新規作成
  - 目的: 既存の `EventDetector` ロジックを使い、任意のテキストに対してイベント判定を個別に実行・確認できる
  - 機能: config.yamlの読み込み、LLM（Gemini/GPT）による分析、判定理由と抽出情報（イベント名、サークル名、スペース番号）の表示
  - 検証: 「今週末の4/25（土）はニコニコ超会議内、クリエイタークロスにITSUKA RECORDSで参加します！」で正常にイベント判定されることを確認

## 直近の作業（2026-04-08）
- **Hydrus重複削除タグ汚染リセットスクリプト作成**:
  - `scripts/hydrus/reset_tags_by_url.py` を新規作成
  - 入力: SHA256ハッシュ + 正しいPixiv artwork URL
  - 処理: Pixivからgallery-dlでメタデータ再取得 → 既存タグ全削除 → 正しいタグ再付与
  - 複数ページ作品対応: URL検索で同artwork内の全ファイルもまとめてリセット
  - dry-runモード / 確認プロンプト / -y自動承認オプション付き
  - テスト: artwork 93375065（7ページ作品）でdry-run成功。58個の汚染タグ→13個の正しいタグに

## 直近の作業（2026-04-07）
- 全Twitter画像の creator:{username} → twitter_user:{username} 一括移行（441,725ファイル）
- リネーム済み12アカウントのHydrusタグ修正（3,172ファイル）
- cleanup_creator_tags.py Phase 1.5 URL自動判別追加
- Twitter ID変更追跡機能を実装（詳細はprogress.md参照）

## 直近の判断
- **タグ名前空間の方針**: `creator:`は表示名専用、`{platform}_user:`はプラットフォーム固有ハンドル
- **自動判別の条件**: Twitter/X URLが1つだけ（他プラットフォームURLは無視）& ユーザー名一致する`creator:`タグがある場合のみ自動処理

## 次にやること
- `reset_tags_by_url.py` で実際にタグ汚染画像をリセット（本番実行）
- `cleanup_creator_tags.py` の自動判別フェーズを実際に使ってcreator:タグ重複を整理する
- vladkens/twscrape#312 の本家修正を定期確認し、修正リリース後に `_patch_xclid_parser` のフォールバックが不要になったか確認する
- `ondemand.s.c86191da.js` のハッシュが変わって取得失敗するようになったら、`EVENTMONITOR_XCLID_ONDEMAND_URL` で新URLを指定するか pinned URL を更新する

## ブロッカー
- なし（twscrape IndexError は 2026-06-11 に 0.18.1 更新 + xclid フォールバックで解消。ID変更検出の user_by_id フォールバックは X 側エンドポイント廃止により機能停止中）

---

## 2026-03-16 WSL2/Linux固有コードの削除
- ツールの完全Windows対応に伴い、WSL2/Linux固有コードを全削除した。
- `main.py`: `pysqlite3` パッチ（WSL環境でのSQLite互換性迂回策）を削除。
- `setup.sh`: Linux/WSL専用bashセットアップスクリプトを全削除。
- `setup.bat`: WSL用venv検出・警告ブロックを削除。
- `README.md`: `Linux / WSL (推奨)` セクションを削除し、Windows専用のインストール手順に整理。WSL関連の記述も除去。
- テスト: `run_tests.py --quick` 66 passed。

## 2026-03-14 Pixiv artwork URLからのユーザーID解決対応
- Discord経由でPixiv作品ページURL（`/artworks/{id}`）が投稿された場合に、作品IDからユーザーIDを解決して新規アカウント追加する機能を実装した。
- `src/services/discord_account_ingest.py`
  - `_extract_pixiv_username()` に `/artworks/{artwork_id}` パターンを追加。仮の username `"artworks:NNNNN"` として保持。
  - ロケールプレフィックス（`/en/`, `/ja/` 等）のスキップ処理を追加。
  - `_resolve_pixiv_artwork_accounts()` 非同期メソッドを追加。gallery-dl経由で artwork → user_id を解決。
  - コンストラクタに `pixiv_extractor` パラメータを追加。
- `src/pixiv_extractor.py`
  - `fetch_user_works_by_artwork_id()` メソッドを追加。gallery-dl `--range 1-1` で作品URLから最小限のメタデータを取得。
- `main.py`
  - `DiscordAccountIngestor` 初期化に `pixiv_extractor` を受け渡すよう更新。
- テスト: `tests/unit/test_misskey_instances.py` に4件追加（artwork URL抽出、/users/回帰、ゴミテキスト付き、/en/パス対応）。
- 検証: `test_misskey_instances.py` 8 passed、`run_tests.py --quick` 66 passed。


## 2026-03-14 Hydrus R-18 タグ確認の誤検知修正
- ユーザー報告「Hydrus拡張インポートで R-18 タグが付いていない気がする」を調査した。
- `scripts/fix/sync_twitter_account_sensitive.py --dry-run --username oreizmmiporin` は当初 `files_already_tagged=0 / files_tagged=1071` を返したが、Hydrus API の生メタデータでは `rating:r-18` を含む EventMonitor タグが実際に付与されていた。
- 根本原因は `src/hydrus_client.py` の `_get_file_tags()` / `_check_file_exists_with_metadata()` が Hydrus の旧メタデータ形式しか見ず、さらに platform 個別タグサービスが1つでもあると legacy の `my tags` (`local tags`) を確認対象から外していたこと。
- 現行設定は `gelbooru: "danbooru tags"` だけ個別設定しており、Twitter ファイルは `my tags` にタグがあるのに確認ヘルパーが空扱いしていた。
- 修正として、Hydrus の新形式 `metadata["tags"][service_key]["display_tags"]["0"]` と旧形式の両方を読める `_extract_display_tags_from_metadata()` を追加し、`all_tag_service_keys` は常に legacy local tags + 個別設定サービスの両方を返すよう変更した。
- 修正後の再確認では、`oreizmmiporin` の dry-run が `files_already_tagged=1071 / files_tagged=0` に変わり、R-18 タグ漏れではなく確認ロジックの誤検知だったことを確認した。
- 追加テストとして `tests/unit/test_sensitive_tagging.py` に mixed tag service 設定時でも legacy local tags を確認できるケースと、Hydrus 新旧メタデータ形式の両対応テストを追加した。

## 迴ｾ蝨ｨ縺ｮ驕狗畑繧ｳ繝ｳ繝・く繧ｹ繝茨ｼ・026-03-13譖ｴ譁ｰ・・
### 1) 遞ｼ蜒堺ｸｭ縺ｮ荳ｻ隕∽ｻ墓ｧ假ｼ育樟陦鯉ｼ・- artwork邉ｻ縺ｮ縺・■ Pixiv / Kemono 縺ｯ迴ｾ蝨ｨ縲∵悴蜿門ｾ怜・莉ｶ繧・`1蝗槭・ gallery-dl 繝励Ο繧ｻ繧ｹ` 縺ｧDL髢句ｧ九☆繧九・  - extractor 蛛ｴ縺ｧ `work` 蜊倅ｽ阪・螳御ｺ・ｒ讀懃衍縺励∝ｮ御ｺ・夂衍繧・AccountProcessor 縺ｸ霑斐☆縲・  - AccountProcessor 縺ｯ騾夂衍繧貞女縺代◆菴懷刀縺九ｉ蜊ｳ `validate -> DB save -> Hydrus import` 繧帝ｲ繧√ｋ縲・- 螟ｱ謨嶺ｽ懷刀縺ｯ `artwork_retry_queue` 縺ｫ菫晄戟縺励・壼ｸｸfetch縺檎ｩｺ縺ｧ繧よｬ｡蝗柮un縺ｧ蜀崎ｩｦ陦後☆繧九・- `subprocess_utils.py` 縺ｮ rate-limit 蠕・ｩ溷愛螳壹・菫ｮ豁｣貂医∩縺ｧ縲∝ｾ・ｩ溽峩蠕後・辟｡騾壻ｿ｡繧貞叉繧ｿ繧､繝繧｢繧ｦ繝域桶縺・＠縺ｪ縺・・
### 2) 逶ｴ霑代〒隗｣豎ｺ貂医∩縺ｮ隲也せ
- Kemono螟ｧ隕乗ｨ｡繧｢繧ｫ繧ｦ繝ｳ繝医〒縲粂ydrus縺悟虚縺九↑縺・阪ｈ縺・↓隕九∴縺滉ｻｶ縺ｯ縲∝ｮ滄圀縺ｯ蛛懈ｭ｢縺ｧ縺ｯ縺ｪ縺城聞譎る俣騾先ｬ｡蜃ｦ逅・□縺｣縺溘・- backup progress 縺ｯ `active_runs/recent_runs` 縺ｸ諡｡蠑ｵ貂医∩縺ｧ縲～crawler_media` 螳溯｡御ｸｭ繧ゅワ繝ｼ繝医ン繝ｼ繝域峩譁ｰ縺輔ｌ繧九・- Discord隱､騾∽ｿ｡縺ｮ蠕悟・逅・ｼ郁ｩｲ蠖薙Γ繝・そ繝ｼ繧ｸ蜑企勁・峨→ Twitter resume 螳牙・蛹悶・蟇ｾ蠢懈ｸ医∩縲・- Pixiv/Kemono 縺ｫ縲悟・莉ｶDL髢句ｧ・+ work螳御ｺ・＃縺ｨ縺ｮ蜊ｳ菫晏ｭ倥・蜊ｳHydrus import縲阪ｒ蜿肴丐縺励◆縲・- `run_tests.py` 縺ｯ迴ｾ陦・Python 迺ｰ蠅・・ `pytest` 繧剃ｽｿ縺・ｈ縺・ｿｮ豁｣縺励～--quick` 縺ｯ `pytest-xdist` 譛ｪ蟆主・縺ｧ繧ら峩蛻怜ｮ溯｡後∈繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縺吶ｋ繧医≧縺ｫ縺励◆縲・- GitHub Actions 縺ｮ `push` 繝・せ繝医・縲∝ｭ伜惠縺励↑縺・`tests/integration/` 繧・悴螳夂ｾｩ縺ｮ slow 繝・せ繝医〒關ｽ縺｡縺ｪ縺・ｈ縺・せ繧ｭ繝・・譚｡莉ｶ繧定ｿｽ蜉縺励◆縲・
### 3) 譛ｪ隗｣豎ｺ縺ｮ螳溷漁隱ｲ鬘鯉ｼ亥━蜈磯・ｼ・1. EventDetector / DiscordNotifier / backup 騾｣謳ｺ縺ｮ繝ｦ繝九ャ繝医ユ繧ｹ繝域僑蜈・2. 繝舌ャ繧ｯ繧｢繝・・蜃ｦ逅・・險ｭ險育ｰ｡邏蛹厄ｼ・B菫晏ｭ倬・ｺ上→螟ｱ謨苓ｨｱ螳ｹ譁ｹ驥昴・蝗ｺ螳夲ｼ・3. memory-bank蜀・・螻･豁ｴ縺ｨ迴ｾ迥ｶ繧ｹ繝・・繧ｿ繧ｹ縺ｮ蛻・屬驕狗畑
4. lint / security 邉ｻ繝√ぉ繝・け繧・`run_tests.py` 縺ｨ CI 縺ｮ荳｡譁ｹ縺ｧ謨ｴ逅・＠逶ｴ縺・
---

## Kemono/Pixiv 螟ｧ驥丞叙蠕怜ｯｾ遲厄ｼ・026-03-13蜿肴丐・・
### 迴ｾ蝨ｨ縺ｮ螳溯｣・- `PixivExtractor` / `KemonoExtractor` 縺ｯ縲∝ｯｾ雎｡菴懷刀蜈ｨ莉ｶ縺ｮ URL 繧・`1蝗槭・ gallery-dl 螳溯｡形 縺ｫ貂｡縺吶・- extractor 蛛ｴ縺ｯ荳譎・L繝・ぅ繝ｬ繧ｯ繝医Μ繧堤屮隕悶＠縲～work` 蜊倅ｽ阪〒繝輔ぃ繧､繝ｫ謨ｰ縺ｨ螳牙ｮ壼喧繧堤｢ｺ隱阪＠縺ｦ螳御ｺ・夂衍繧定ｿ斐☆縲・- `AccountProcessor` 縺ｯ callback 繧貞女縺代◆鬆・↓ monitor/log-only 縺ｮ菫晏ｭ伜・逅・ｒ騾ｲ繧√ｋ縲・
### 蜉ｹ譫・- `1菴懷刀縺斐→縺ｫ gallery-dl 繧定ｵｷ蜍輔☆繧義 繝懊ヨ繝ｫ繝阪ャ繧ｯ繧定ｧ｣豸医・- 蛻晏屓繧ｯ繝ｭ繝ｼ繝ｫ繧・､ｧ驥乗悴蜿門ｾ玲凾縺ｧ繧ゅ∵怙蛻昴・菫晏ｭ倥・Hydrus import 縺後悟・菴懷刀螳御ｺ・ｾ後阪〒縺ｯ縺ｪ縺上梧怙蛻昴・菴懷刀螳御ｺ・凾轤ｹ縲阪〒蟋九∪繧九・- 譌｢蟄倥・ `artwork_retry_queue` 縺ｯ邯ｭ謖√＠縲∵悴螳御ｺ・ｽ懷刀縺縺第ｬ｡蝗柮un縺ｸ蝗槭☆縲・
### 讀懆ｨｼ迥ｶ豕・- `tests/unit/test_artwork_incremental.py` 縺ｧ縲゜emono/Pixiv 縺ｮ蜈ｨ莉ｶ荳諡ｬ髢句ｧ九∝ｮ御ｺ・・叉蜃ｦ逅・∝､ｱ謨嶺ｽ懷刀縺ｮ retry 邯ｭ謖√ｒ遒ｺ隱肴ｸ医∩縲・- `venv\Scripts\python.exe -X utf8 run_tests.py --quick` 縺ｯ 55 passed 縺ｧ騾夐℃縲・

---

## 2026-03-13 Twitter gap recovery
- 現行HEADでもTwitter増分取得に取りこぼし固定化の穴があった。
- 原因1: quick check が最新2件だけを見て全件スキップしていた。
- 原因2: twscrape incremental が latest_tweet_date 以降だけを取得し、最新保存ツイートより少し古い未取得ツイートを old 扱いで落としていた。
- 原因3: twscrape incremental が最初の既知ツイートで停止していたため、既知ツイート直後の未取得ツイートを回収できなかった。
- 対応: overlap window を導入し、quick check を最近20件/48時間重なり確認に変更。twscrape は既知ツイートを重複スキップしつつ一定件数連続で既知なら停止する方式へ変更。
- テスト: `tests/unit/test_twitter_gap_recovery.py` を追加し、「最新2件の先の欠損検出」と「既知ツイート直後の欠損回収」を再現確認した。

## 2026-03-13 Twitter focused recovery + Hydrus order fix
- URL直指定で `2031592287034290469` (`s7_d82`) と `2032093421167656965` (`29herase`) を再取得し、DB保存と Hydrus インポートまで実施した。
- `2032135476484981128` (`RedRam_Marder`) はすでに DB / ローカル画像とも存在していた。
- `s7_d82` は protected account のため、通常の timeline 増分回収では拾えず、private cookie を使った status URL 直取得が必要だった。
- `scripts/fix/import_times_twitter.py` に `--created-on` を追加し、今日作成分だけを対象に Hydrus import time を tweet_date 順へ補正できるようにした。
- 同スクリプトは `local_media` 順を優先して file hash を解決するよう変更し、同一ツイート内のページ順が崩れにくいようにした。

## 2026-03-13 Kemono/Pixiv artwork retry 方針見直し
- `7715ad5` (`Pixiv/Kemono作品処理をストリーミング化`) が、Pixiv/Kemono artwork を `stream_download_media_for_works()` 経路へ切り替えた分岐点だった。
- 1週間前前後で問題が出ていなかった状態は `ffb6aab` / `6ae20c0` 系で、retry queue は使っていても処理は `download_media_for_works([work_id])` の逐次実行だった。
- 今回の Kemono backlog 問題は retry queue の未再開ではなく、large backlog を 1 回の streaming gallery-dl に載せた結果、`idle_timeout=180` で未完了分が大量に retry queue へ戻ることだった。
- 修正として、`AccountProcessor` 側で Pixiv/Kemono の streaming artwork 経路を止め、逐次処理へ戻した。retry queue はそのまま維持している。
- `run_with_idle_timeout()` の rate-limit retry は既定で無制限に変更した。rate-limit と判定できる限り待機と再試行を続け、非 rate-limit エラーでは即終了する。
- rate-limit の待機秒数は、サーバーが `Retry-After` や `retry in ...` を返した場合のみその値を使い、明示値がない場合は指数バックオフをやめて固定 60 秒に変更した。
- HTTP ベースの `request_with_rate_limit_retry()` も既定の retry 上限を外し、rate-limit が続く限り固定間隔で再試行する挙動へ揃えた。
- 速度差の調査では、`subprocess_utils.py` に gallery-dl をグローバルに 1 本へ制限するロックはなかった。一方で `main.py` の platform 内アカウント処理は以前から直列で、これは今回の差分ではない。
- 追加対応として、無効化だけで残っていた streaming dead code を物理削除した。対象は `AccountProcessor` の streaming 分岐、`PixivExtractor` / `KemonoExtractor` の `stream_download_media_for_works()` と専用 helper、`subprocess_utils.py` の `run_with_idle_timeout_stream()`、および関連テストのダミー実装。
- streaming 用にだけ残っていた `batch_size` 読み込みも Pixiv/Kemono から削除した。現行の artwork 経路では batch 処理を使わない。
- 削除後の検証結果は `pytest tests/unit/test_artwork_incremental.py -q` が 6 passed、`pytest tests/unit/test_rate_limit_handling.py -q` が 9 passed、`run_tests.py --quick` が 59 passed。

## 2026-03-14 Kemono クローラー速度調査
- 対象は進行中の `logs/app_20260314_171638.log`。2026-03-14 22:52:12 JST 時点で `fanbox/43115256` の kemono ダウンロード試行 105 件を集計した。
- 速度は `Downloading media for 1 kemono works in a single run` から `Moved ... kemono media files` までを 1 DL 区間とみなし、保存先 `F:\48_EventMonitor_log\images\fanbox_43115256\` の work ID 単位ファイル合計サイズで算出した。
- 既知サイズ 102 件の合計は 11.29 GiB / 5.02 時間で、加重平均 0.64 MiB/s（約 5.4 Mbps）。`Imported ... to Hydrus` まで到達した成功分 69 件だけでも 9.45 GiB / 2.85 時間で 0.94 MiB/s（約 7.9 Mbps）。
- 直近 20 件は加重平均 1.10 MiB/s、中央値 0.53 MiB/s。小さい partial / incomplete を含む全体中央値は 0.22 MiB/s。
- 代表例
  - 2026-03-14 22:45:04 -> 22:51:39, `fanbox_7986037`, timeout 後 26 files / 171.5 MiB, 0.43 MiB/s
  - 2026-03-14 22:21:19 -> 22:29:52, `fanbox_7975749`, 156 files / 1204.9 MiB, 2.35 MiB/s
  - 2026-03-14 21:39:03 -> 21:55:28, `fanbox_7966561`, 130 files / 992.3 MiB, 1.09 MiB/s
- 極端に遅い 0.001 MiB/s 前後のケースは 0.15 MiB 前後の 1-file incomplete 投稿が中心で、実データ転送より待ち時間の影響が大きい。
## 2026-03-14 Kemono 壁時計ベース再集計
- 前回の `MiB/s` 集計は「実ダウンロード区間の帯域」で、ユーザー要望の「処理全体で1ファイルに何秒かかっているか」とはズレていたため、`logs/app_20260314_171638.log` を壁時計ベースで再集計した。
- 対象は `fanbox/43115256` の backlog。開始は `2026-03-14 17:18:13.411` の最初の `Downloading media for 1 kemono works in a single run`、最新確認点は `2026-03-14 23:16:05.594` の Hydrus import 行。
- この時点で `107/420 works`、`1380 files moved`。経過 `5時間57分52秒` に対して平均 `15.56秒/ファイル`、`200.67秒/作品`、`231.84ファイル/時`。
- 直近の 107 件目は `23:05:50.899` 開始、`23:15:22.304` に `76 files moved`、`23:16:05.594` 時点でも Hydrus import 継続中。壁時計で見ると当該 work はその時点まで `10分14.7秒`、`8.09秒/ファイル` ペース。
