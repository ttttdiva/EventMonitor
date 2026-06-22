# progress.md

## 2026-06-20 Kemono CDN全滅後のプレビュー専用モード
- 作業形態:
  - Base branch: `main`
  - User requested direct work on `main`; no worktree/branch was created.
  - Existing unrelated state: local `main` already had `CSVアカウント一覧を同期 (archive)` ahead of `origin/main` before this change.
- 問題:
  - 稼働中ログでは `fanbox/1184461` の初回クロールが `incomplete download (0/N)` を各workで繰り返し、CDN全滅状態でも gallery-dl のタイムアウトログで処理が長時間進まなかった。
  - `kemono.cr` metadata と preview/thumbnail 配信は利用できる一方、原寸CDN候補が全滅したクロールでは以降も原寸DLを試す価値が低かった。
- 実装:
  - `src/kemono_extractor.py`
    - 全 `cdn_fallback_hosts` が unavailable になった時点で `_cdn_outage_preview_only` を有効化。
    - 同一サイクル中の以降の `download_media_for_works()` は `hash_map` がある場合 gallery-dl と原寸CDN fallback をスキップし、preview fallback だけ実行。
    - `clear_reachability_cache()` で CDN failed-host cache と preview-only フラグをリセットし、次サイクルでは原寸DLを再試行できるようにした。
  - `config.yaml`
    - preview fallback 専用の短い timeout (`preview_fallback_connect_timeout: 3`, `preview_fallback_read_timeout: 15`) を追加。
  - `tests/unit/test_kemono_cdn_fallback.py`
    - CDN全滅後にプレビュー専用モードへ入り、2件目以降で `run_with_idle_timeout` が呼ばれないことを回帰テスト化。
    - サイクルクリアでフラグとCDN failed-host cacheがリセットされることを追加確認。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile src\kemono_extractor.py tests\unit\test_kemono_cdn_fallback.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -c "import yaml; yaml.safe_load(open('config.yaml', encoding='utf-8')); print('config yaml ok')"`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_kemono_cdn_fallback.py -q`: 5 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_artwork_incremental.py tests\unit\test_kemono_cdn_fallback.py -q`: 12 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 99 passed, 1 warning
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: Python crawler の取得経路制御変更であり、画面・実機・ブラウザ確認は不要。通常検証と単体回帰テストで確認。

## 2026-06-18 merge: PR #11 Kemono preview fallback 追加
- Base branch: `main`
- Target branch: `codex/kemono-preview-fallback`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/11
- Merge worktree: `D:\Dev\48_EventMonitor_merge_pr11_kemono_preview_fallback`
- Merge branch: `codex/merge-pr11-kemono-preview-fallback`
- Release gate:
  - Before merge: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
  - Merge worktree target diff: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Mobile changed: false
- APK build / GitHub Release upload / `latest.json`: not required
- Work verification summary:
  - PR側で `py_compile`, Kemono fallback target pytest, artwork incremental + fallback pytest, `run_tests.py --quick`, and live `img.kemono.cr` thumbnail probe を確認済み。
- Merge result:
  - `origin/main` から作成した merge worktree で `origin/codex/kemono-preview-fallback` を `--no-ff` merge。
  - Conflict: none.
- Merge verification:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile src\kemono_extractor.py tests\unit\test_kemono_cdn_fallback.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_kemono_cdn_fallback.py -q`: 3 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_artwork_incremental.py tests\unit\test_kemono_cdn_fallback.py -q`: 10 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 97 passed
- Debug policy: `off`
- Heavy debug result: skipped
- Env/dependency prerequisite: merge worktree used existing `D:\Dev\48_EventMonitor\venv`; no `.env` or secret copy required.

## 2026-06-18 Kemono preview fallback 追加
- 作業形態:
  - Base branch: `main`
  - Work branch: `codex/kemono-preview-fallback`
  - Worktree: `D:\Dev\48_EventMonitor_kemono_preview_fallback`
  - Existing unrelated diff: none.
- 問題:
  - `kemono.cr` のページ/APIは見えるが、原寸ファイルの storage node `n1.kemono.cr` から `n4.kemono.cr` が ConnectTimeout になり、CDN fallback でも `incomplete download (0/N)` が継続していた。
  - 外部情報と手元確認では、frontend/API と `img.kemono.cr` thumbnail 配信は生きていて、原寸 storage だけ別IP帯で落ちている状態と判断。
- 実装:
  - `src/kemono_extractor.py`
    - gallery-dl と原寸 CDN fallback、SHA256検証の後に、まだ足りない Kemono media だけ `img.kemono.cr/thumbnail/data/...` / `kemono.cr/thumbnail/data/...` へ preview fallback する処理を追加。
    - preview は原寸SHA256と一致しないため、原寸検証後に別枠で追加し、Pillowで画像として読めるものだけ採用。
    - 保存名に `_preview` を付け、Content-Type に合わせて `.jpg` / `.png` 等の拡張子へ補正。
    - preview を含む場合も元の file→attachments 順へ戻す並び替えを追加。
  - `config.yaml`
    - `kemono.preview_fallback_hosts` を追加し、既定を `img.kemono.cr`, `kemono.cr` にした。
  - `tests/unit/test_kemono_cdn_fallback.py`
    - 原寸 CDN 全滅後に preview fallback が走り、`_preview` ファイルを移動先へ保存する回帰テストを追加。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile src\kemono_extractor.py tests\unit\test_kemono_cdn_fallback.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_kemono_cdn_fallback.py -q`: 3 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_artwork_incremental.py tests\unit\test_kemono_cdn_fallback.py -q`: 10 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 97 passed
  - 実ネットワーク確認: `fanbox/1184461/post/687044` の cover hash preview を `img.kemono.cr/thumbnail/data/...` から取得成功（58,888 bytes）。
- 注意:
  - attachment の thumbnail が `img.kemono.cr` でも 404 の場合は、そのファイルは引き続き取得不能。全 expected count に届かなければ既存どおり DB save は skipped になり retry queue に残る。
  - preview は低解像度/再圧縮の代替であり、原寸 storage 復旧後の再取得とは品質が異なる。
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: Python crawler の取得経路追加であり、画面・実機・ブラウザ確認は不要。通常検証と実HTTPの小さな preview probe で確認。

## 2026-06-17 イベント判定キーワード追加
- 作業形態:
  - Base branch: `main`
  - User requested direct work on `main`; no worktree/branch was created.
  - Existing unrelated diff: none.
- 問題:
  - `mac_4229/status/2059414111243649416` は `all_tweets` に保存済みかつ `checked_for_event=True` だったが、`event_tweets` には存在せず、Discord通知処理に到達していなかった。
  - 原因は LLM 判定前の `_quick_keyword_check`。本文 `声音7次会申し込み済み` が既存キーワードに一致せず、LLMへ投げられる前に除外されていた。
  - 同アカウントの `2067096713484272120` (`声音7次会確定みたいなので改めて出ます`) も同じ理由で前段ゲートを通っていなかった。
- 実装:
  - `config.yaml`
    - event_detection keywords に `声音`, `申し込み`, `申込`, `出ます` を追加。
  - `tests/unit/test_event_keywords_config.py`
    - 取りこぼした2文面が現行 config の `_quick_keyword_check` を通ることを回帰テスト化。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile tests\unit\test_event_keywords_config.py src\event_detector.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_keywords_config.py tests\unit\test_event_detector_cli_fallback.py -q`: 5 passed
  - DB本文での `_quick_keyword_check`: `2059414111243649416` は `['声音', '申し込み']`、`2067096713484272120` は `['声音', '出ます']` にマッチ。
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 96 passed
- 注意:
  - 既存の取りこぼし投稿はすでに `checked_for_event=True` のため、この設定変更だけでは自動再判定されない。通知が必要な場合は対象投稿の再判定/checked flag reset が別途必要。
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: 設定語彙と単体テストの変更であり、画面・実機・ブラウザ確認は不要。

## 2026-06-16 Kemono CDN フォールバック追加
- 作業形態:
  - Base branch: `main`
  - User requested direct work on `main`; no worktree/branch was created.
  - Existing unrelated diff: none.
- 問題:
  - `logs/app_20260616_100518.log` で `fanbox/1184461` の Kemono 初回フルクロール454件中、少なくとも45件が `Failed to download media for 1 kemono works` と `incomplete download (0/N), skipping DB save` で失敗していた。
  - gallery-dl のメタデータ/API取得は成功していたが、実ファイルURLが `n1.kemono.cr` へリダイレクトされた後に接続タイムアウトしていた。
  - 現環境では確認時点で `n1` から `n4` まで全て ConnectTimeout だったため、外部CDN障害時はコードだけで必ず回収できる状態ではない。
- 実装:
  - `src/kemono_extractor.py`
    - gallery-dl 実行後、Kemono の `media_hashes` を持つ作品で不足ファイルがある場合、`/data/...` URL を `cdn_fallback_hosts` の各ホストへ差し替えて直接DLするフォールバックを追加。
    - DL後はKemonoメタデータ由来のSHA256で検証し、一致したファイルだけ既存の移動/DB保存/Hydrus処理へ渡す。
    - ConnectTimeout / ConnectionError / 5xx はホスト単位で `cdn_fallback_cooldown_seconds` の間スキップし、同一runで同じ死んだCDNへ大量再試行しないようにした。
    - `gallery-dl issues` ログを stderr 先頭200文字から末尾最大2000文字へ変更し、原因詳細が残るようにした。
  - `config.yaml`
    - `kemono.cdn_fallback_hosts`, `cdn_fallback_connect_timeout`, `cdn_fallback_read_timeout`, `cdn_fallback_cooldown_seconds` を追加。
  - `tests/unit/test_kemono_cdn_fallback.py`
    - タイムアウトしたホストを飛ばして次ホストで成功すること、クールダウン中のホストを再試行しないことを追加。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile .\src\kemono_extractor.py .\tests\unit\test_kemono_cdn_fallback.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest .\tests\unit\test_kemono_cdn_fallback.py -q`: 2 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest .\tests\unit\test_artwork_incremental.py .\tests\unit\test_kemono_cdn_fallback.py -q`: 9 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 .\run_tests.py --quick`: 95 passed
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: Python crawler の取得フォールバック修正で、画面・実機・ブラウザ確認は不要。外部CDNは確認時点で全候補がタイムアウトしていたため、実DL成功確認はできず、単体テストでフォールバック挙動を検証。

## 2026-06-15 拡張機能 Pixiv import time 書き換え停止
- 作業形態:
  - Base branch: `main`
  - User requested direct work on `main`; no worktree/branch was created.
  - Existing unrelated diff: none.
- 問題:
  - Pixiv手動インポートの順番崩れ対策として、拡張機能が Hydrus の file import time を Pixiv `uploadDate/createDate + pageIndex` に書き換えていた。
  - そのため「今日インポートしたファイル」でも Hydrus 上の import time が投稿日時になっていた。
- 実装:
  - `extension/src/background.ts`
    - Pixiv import の逐次処理 (`PIXIV_IMPORT_CONCURRENCY = 1`) は維持。
    - `pixivImportTimestamp()` と `api.setFileImportTime(...)` 呼び出しを削除。
  - `extension/src/lib/hydrus-api.ts`
    - 拡張機能内の `edit_times/set_time` ラッパーと file service key 解決を削除。
  - `extension/src/content/pixiv.ts` / `extension/src/lib/types.ts`
    - Pixiv `uploadDate/createDate` の受け渡しを削除。
  - `extension/dist` をビルドし、`C:\tool\Edge_Extension\Hydrus Importer\` へコピー。
- 検証:
  - `npm run build` in `extension/`: OK
  - `rg` で `extension/src`, `extension/dist`, `C:\tool\Edge_Extension\Hydrus Importer\` に `setFileImportTime`, `edit_times/set_time`, `timestamp_type`, `pixivImportTimestamp`, `uploadDate`, `createDate` が残っていないことを確認。
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: ブラウザ実操作はdebug-policy offのため未実行。通常検証として拡張機能ビルドと生成物/配布先の静的確認を実施。

## 2026-06-12 bilibili 動態(opus) クローラー追加
- 作業形態: ユーザー指示により `main` 上で直接作業（worktree/branchなし）。既存の無関係差分なし。
- 新規: `src/bilibili_extractor.py`（feed API一覧 + gallery-dl詳細/DLの2段構え、Cookie任意、sensitive常時False）。
- 配線: `database.py`(BilibiliWork/BilibiliLogOnlyWork + PLATFORM_MODELS + _ensure_hydrus_columns + has_any_posts + default_url + 委譲メソッド)、`hydrus_client.py`(ARTWORK_TAG_CONFIG + import/_generate_bilibili_tags + default_url)、`account_processor.py`(SPECS + constructor + reachability + _record_unreachable + FANBOX高速パスを汎用化)、`main.py`(import/init/resolvers/ingest/processor/cache/display_name)、`discord_account_ingest.py`(URL抽出 + opus→mid非同期解決)、`config.yaml`。
- 検証: 実API/実DL/DB round-trip/Discord URL抽出/Hydrusタグ生成すべてOK。`run_tests.py --quick` 93 passed。
- 詳細は activeContext.md 2026-06-12 を参照。

## 2026-06-09 拡張機能 Pixiv インポート順・タグサービス修正
- 作業形態:
  - Base branch: `main`
  - User requested direct work on `main`; no worktree/branch was created.
  - Existing unrelated diff: `config.yaml` had pre-existing user changes and was not touched.
- 問題:
  - Pixiv手動インポートが background 側で6並列になっており、Hydrusのimport timeが完了順になってページ/作品順が崩れていた。
  - 拡張機能の既定 Pixiv タグサービスが存在しない `pixiv tags` で、Hydrus側の最初のlocal tag serviceである `danbooru tags` へ黙ってフォールバックしていた。
- 実装:
  - `extension/src/background.ts`
    - Pixiv import concurrency を1にし、ページ順に処理。
    - Pixiv APIの `uploadDate/createDate` とページindexから Hydrus import time を `edit_times/set_time` で設定。
    - タグ追加失敗を握りつぶさず、インポートエラーとして返すよう変更。
    - 既定タグサービスを Twitter/Pixiv/Bluesky=`my tags`, Danbooru/Gelbooru=`danbooru tags` に変更し、旧既定値を実行時正規化。
  - `extension/src/lib/hydrus-api.ts`
    - 存在しないタグサービス名を最初のlocal tag serviceへフォールバックしないよう変更。
    - ファイルサービスキー解決と import time 設定APIを追加。
  - `extension/src/content/pixiv.ts` / `extension/src/lib/types.ts`
    - Pixiv `uploadDate/createDate` を background へ渡すよう追加。
  - `extension/src/popup/*`
    - 既定値/プレースホルダを現行Hydrusサービスに合わせ、保存済み旧既定値を正規化。
  - `extension/dist` をビルドし、`C:\tool\Edge_Extension\Hydrus Importer\` の旧ファイルを削除してコピー（`.git` は保持）。
- 検証:
  - `npm run build` in `extension/`: OK
  - `rg` で `extension/dist` と `C:\tool\Edge_Extension\Hydrus Importer\` に `my tags` 既定、`Tag service ... not found`、`edit_times/set_time`、Pixiv `uploadDate` が含まれることを確認。
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: ブラウザ実操作はdebug-policy offのため未実行。通常検証として拡張機能ビルドと生成物静的確認を実施。

## 2026-05-19 merge: PR #10 ツイート本文検索bat追加
- Base branch: `main`
- Target branch: `codex/add-tweet-search`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/10
- Merge result: merge branch `codex/merge-add-tweet-search` で `origin/codex/add-tweet-search` を `--no-ff` merge。
- 実装:
  - `search.bat`: ルートから `search.bat <検索語>` でDB内ツイート本文検索を起動。
  - `scripts/util/search_tweets.py`: `all_tweets` / `event_tweets` / `log_only_tweets` を横断検索。`--table`、`--username`、`--limit`、`--full`、`--db` に対応。
  - `scripts/README.md`: util 一覧に検索スクリプトを追記。
- Release gate:
  - Before merge worktree: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Mobile changed: false
- APK build / GitHub Release upload / `latest.json`: not required
- Work verification summary:
  - PR側で `py_compile`、`--help`、実DB検索（全テーブル/イベントテーブル）を確認済み。
- Merge verification:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile scripts\util\search_tweets.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 scripts\util\search_tweets.py --help`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 scripts\util\search_tweets.py コミケ --db D:\Dev\48_EventMonitor\data\eventmonitor.db --limit 3`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 scripts\util\search_tweets.py コミケ --db D:\Dev\48_EventMonitor\data\eventmonitor.db --table event --limit 1`: OK
- Debug policy: `off`
- Heavy debug result: skipped

## 2026-05-17 Twitter incomplete media retry queue
- 問題:
  - TwitterメディアDL失敗時に `skipping DB save` しても、同じアカウントで後続/新しいツイートが保存されると latest 基準が進み、次回 quick check / overlap 範囲から失敗ツイートが落ちる可能性があった。
  - そのためログの `they will be retried next run` は、通常取得範囲に再登場する場合にしか成立しなかった。
  - log-only処理ではDL対象を `source == "gallery-dl"` に限定していたため、twscrape由来で media URL を持つツイートが `incomplete download (0/N)` になりやすかった。
- 実装:
  - `src/database.py`
    - `twitter_retry_queue` テーブルを追加。username単位に monitor/log_only 別 payload、retry_count、last_error をJSONで保持。
    - `upsert_twitter_retry` / `get_twitter_retry_tweets` / `clear_twitter_retry` を追加。
    - 既存DBでは `Base.metadata.create_all()` により新テーブルが作成される。
  - `src/services/account_processor.py`
    - 通常監視・log-onlyのTwitter処理で、通常fetchとは別に retry queue を先に読み込み、取得結果に合流するよう変更。
    - incomplete download はDB保存前に retry queue へ保存し、保存成功後に該当retryを削除。
    - log-onlyのメディアDL対象を `source == "gallery-dl"` 限定から media/videos 有無へ変更。
- テスト:
  - `tests/unit/test_database_filters.py`: Twitter retry queue の保存/クリア、monitor/log_only分離を追加。
  - `tests/unit/test_twitter_resume_safety.py`: 通常fetchが空でもqueued tweetを再試行するケース、twscrape由来log-only mediaをDLしてretryを消すケースを追加。
- 検証:
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\database.py src\services\account_processor.py tests\unit\test_twitter_resume_safety.py tests\unit\test_database_filters.py`: OK
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_twitter_resume_safety.py tests\unit\test_database_filters.py -q`: 16 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 90 passed
- Debug policy: `off`
- Heavy debug result: skipped

## 2026-05-16 merge: PR #8 Discord ingest の表示名登録停止を修正
- Base branch: `main`
- Target branch: `codex/fix-discord-ingest-display-name`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/8
- Merge result: merge branch `codex/merge-pr8-discord-ingest-display-name` で `origin/codex/fix-discord-ingest-display-name` を `--no-ff` merge。
- Release gate:
  - Before merge worktree: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
  - After merge: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Mobile changed: false
- APK build / GitHub Release upload / `latest.json`: not required
- Work verification summary:
  - PR側で Discord ingest/FANBOX display_name target pytest 10 passed、`compileall` OK、`run_tests.py --quick` 86 passed、実cookieで `FanboxExtractor.resolve_display_name("we53")` が `we53` を返すことを確認済み。
- Merge verification:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_discord_ingest_display_name.py tests\unit\test_misskey_instances.py -q`: 10 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m compileall main.py src tests\unit\test_discord_ingest_display_name.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 86 passed
- Debug policy: `off`
- Heavy debug result: skipped
- Env/dependency prerequisite: merge worktree used existing `D:\Dev\48_EventMonitor\venv`; no `.env` or secret copy required.

## 2026-05-16 Discord ingest の FANBOX 表示名取得停止対策
- 問題:
  - Discord ingest で追加した非Twitterアカウントの `display_name` を空欄でCSV登録し、起動直後の `_resolve_missing_display_names()` に委譲していた。
  - FANBOX表示名解決は `gallery-dl --range 1-1` を起動するため、`fanbox:we53` 追加時にプラットフォーム別巡回開始前で待機し、Twitter/Pixiv等の並列処理に進めなかった。
- 実装:
  - `src/services/discord_account_ingest.py`
    - `display_name_resolvers` を受け取り、Discord ingest のCSV追記前に platform 別 `display_name` を解決するよう変更。
    - 取得失敗時は空欄ではなく識別子を `display_name` として使い、CSV登録を完結させる。
    - 表示名のカンマ除去と空白正規化を共通化。
  - `main.py`
    - platform extractor 初期化後に `DiscordAccountIngestor` を構築し、各 extractor の `resolve_display_name` を渡すよう変更。
  - `src/fanbox_extractor.py`
    - FANBOX表示名解決を `gallery-dl` から `post.listCreator` APIへ変更し、15秒HTTPタイムアウトで `user.name` を取得するよう変更。
  - `tests/unit/test_discord_ingest_display_name.py`
    - Discord ingest がFANBOX resolver結果をCSVへ書くこと、FANBOX表示名解決がAPI経路を使うことを追加。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_discord_ingest_display_name.py tests\unit\test_misskey_instances.py -q`: 10 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m compileall main.py src tests\unit\test_discord_ingest_display_name.py`: OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick`: 86 passed
  - 実cookieを使った `FanboxExtractor.resolve_display_name("we53")`: `we53`
- Debug policy: `off`
- Heavy debug result: skipped

## 2026-05-16 merge: PR #7 LLMルーティング設定整理
- Base branch: `main`
- Target branch: `codex/refactor-llm-routing`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/7
- Release gate: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Merge result: merge branch `codex/merge-pr7-llm-routing` で `origin/codex/refactor-llm-routing` を `--no-ff` merge。
- Merge fix:
  - `tests/live_test_gemini_cli.py` が Gemini CLI 単独テストにもかかわらず既定 route 先頭の Codex model を流用していたため、`gemini-3-flash-preview` 固定に修正。
- Work verification summary:
  - PR本文で `py_compile`、LLM routing unit、CLI/Markdown scripts、`compileall src scripts`、`run_tests.py --quick` 84 passed を確認。
- Merge verification:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py scripts\util\judge_tweet.py tests\mock_codex_cli.py tests\mock_gemini_cli.py tests\unit\test_event_detector_cli_fallback.py tests\test_cli_integration.py tests\test_markdown.py tests\verify_fallback.py tests\live_test_gemini_cli.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py -q` : 4 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 tests\test_cli_integration.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 tests\test_markdown.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m compileall src scripts` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 84 passed
- Debug policy: `off`
- Heavy debug result: `skipped`
- Reason: Python設定/LLM判定ロジック/テスト/ドキュメント変更で、画面・実機・ブラウザ確認は不要。
- APK build / GitHub Release upload / `latest.json`: not required
- Env/dependency prerequisite: merge worktree に `venv` がないため、元 checkout の `D:\Dev\48_EventMonitor\venv` を使用。

## 2026-05-16 LLMルーティング改修
- 作業ブランチ: `codex/refactor-llm-routing`
- 方針:
  - 後方互換なしで `models` / `gemini_cli` / `codex_cli` 設定を廃止し、`llm_providers` と `llm_routes` へ移行。
  - provider は実行方法、route は provider/model/effort と試行順を持つ。
  - 既定順は `codex_cli:gpt-5.3-codex-spark:medium` → `gemini_cli:gemini-3-flash-preview` → `codex_cli:gpt-5.5:medium` → `gemini_api:gemini-3-flash-preview`。
  - Codex CLI effort は `-c model_reasoning_effort="..."` で渡す。
  - 利用可能な Codex CLI モデル/effort は `docs/llm_routing.md` に記録。
- 検証:
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py scripts\util\judge_tweet.py tests\mock_codex_cli.py tests\mock_gemini_cli.py tests\unit\test_event_detector_cli_fallback.py tests\test_cli_integration.py tests\test_markdown.py tests\verify_fallback.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py -q` : 4 passed
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 tests\test_cli_integration.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 tests\test_markdown.py` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 -m compileall src scripts` : OK
  - `D:\Dev\48_EventMonitor\venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 84 passed

## 2026-05-09 merge: PR #6 Pixiv大容量インポート修正
- Base branch: `main`
- Target branch: `codex/fix-pixiv-large-message`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/6
- Merge result: merge branch `codex/merge-pr6-pixiv-large-message` で `origin/codex/fix-pixiv-large-message` を `--no-ff` merge。
- Release gate: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Mobile changed: false
- APK build / GitHub Release upload / `latest.json`: not required
- Work verification: PR本文で `npm run build` in `extension/` OK を確認。
- Merge verification: merge後の `npm run build` in `extension/` OK。
- Debug policy: `off`
- Heavy debug result: skipped
- Env/dependency prerequisite: merge worktree の `extension/` で `npm ci` 実行。`npm audit` は既存依存に 1 moderate / 5 high を報告したが、今回の差分による dependency 変更なし。

## 2026-05-09 拡張機能のPixiv大容量インポート修正
- 問題:
  - Pixiv手動インポートで複数ページ/大容量作品をDL後、画像Base64配列を一括 `runtime.sendMessage` していたため、Chromeの `Message exceeded maximum allowed size of 64MiB` でHydrus import前に失敗していた。
  - ユーザー報告内の `ERR_BLOCKED_BY_CLIENT` や BOOTH 404 はページ側/広告ブロッカー由来の副次ログで、直接の失敗原因は拡張機能のメッセージサイズ制限。
- `extension/src/content/pixiv.ts`
  - 全画像をまとめて保持・送信する処理を廃止。
  - 1ページずつDLし、Base64を4 MiBチャンクに分割してbackgroundへ送信するよう変更。
  - 各ページのimport結果を集計し、部分失敗はconsole warningに残す。
- `extension/src/background.ts` / `extension/src/lib/types.ts`
  - Pixiv画像チャンク転送の start/chunk/finish/abort メッセージを追加。
  - background側でチャンクを再結合し、既存のPixiv import処理へ1画像単位で渡す。
- 検証:
  - `npm run build` in `extension/`: OK

## 2026-05-05 Booru creatorタグのCSV正規化
- `src/hydrus_client.py`
  - Gelbooru `tags_artist` を `monitored_accounts` / `monitored_accounts.csv` に照合し、一致したID/タグはCSVの `display_name` を `creator:` に使うよう変更。
  - 生のBooru artistタグは `gelbooru_artist:` とBooru専用タグサービスに保持し、検索クエリは `gelbooru_query:` に分離。
  - CSV未登録のartistタグは従来通り `creator:{artist}` として残し、Booru由来の正しいcreator付与を維持。
- `extension/src/lib/tag-generator.ts` / `scripts/hydrus/folder_import.py`
  - 手動Booru/フォルダimportで `{platform}_artist:` / `danbooru_artist:` も付与し、生artistタグをcreator以外のnamespaceにも保持。
- 既存Hydrusタグ整理:
  - 一回限りのGelbooru creator整理スクリプトを実行し、105件の `creator:{raw artist}` を削除、対応する `gelbooru_artist:{raw artist}` を追加。
  - 実行後dry-runで planned changes 0 を確認し、スクリプトは削除済み。
- テスト:
  - `py_compile`: OK
  - `pytest tests/unit/test_sensitive_tagging.py -q`: 29 passed
  - `npm --prefix extension run build`: OK
  - `run_tests.py --quick`: 83 passed

## 2026-05-04 artwork platform refactor
- `src/services/account_processor.py`: removed unreachable old platform blocks; process dispatch and pending Hydrus retry now use `ARTWORK_PLATFORM_SPECS`.
- `src/database.py`: common artwork DB helpers for filter/save/log-only save/Hydrus status/pending retrieval; platform methods delegate to them; Hydrus column migration uses SQLAlchemy inspect/text.
- `src/hydrus_client.py`: common artwork import loop and `ARTWORK_TAG_CONFIG` tag builder; Gelbooru split tags and FANBOX import-time ordering kept.
- `src/twitter_monitor.py` / `src/twscrape_compat.py`: twscrape monkey patches isolated in compat module.
- Tests:
  - target pytest: 40 passed
  - `run_tests.py --quick`: 81 passed
  - `compileall src scripts`: OK
  - `run_tests.py --lint`: skipped because flake8/black/isort are not installed


## 2026-05-03 merge: PR #5 古いイベント判定backlog抑止
- Base branch: `main`
- Target branch: `codex/fix-event-reprocessing`
- PR: https://github.com/ttttdiva/48_EventMonitor/pull/5
- Merge result: merge commit `1cf13a32f7fb1af1a3871c96f112fb7a6d579a2d`
- Release gate: `MOBILE_CHANGED=False`, `RELEASE_REQUIRED=False`
- Debug policy: `auto`
- Debug result: `done`
- Reason: DB/daemon系の処理変更。PR側で `compileall` と unit quick を実行済み、merge前にも `run_tests.py --quick` を再実行して76 passed。
- CI note: GitHub checks were failing, but main branch already had the same class of failures (repo-wide flake8 debt, Python 3.9 dependency incompatibility, deprecated `actions/upload-artifact@v3`). Target-specific local verification passed.
- Branch cleanup: remote/local `codex/fix-event-reprocessing` deleted by PR merge.

## 2026-05-03 イベント判定の古い未判定backlog抑止 + Gemini CLI quota対応
- 調査:
  - `data/eventmonitor.db` は初期化されておらず、`event_tweets` は3128件存在。
  - `all_tweets.checked_for_event=False` が372450件残り、うち365144件が7日超過。該当ログの `1863202577950892043` は `rasra25` の2024-12-01ツイートで、古い未判定backlogから再開判定されていた。
- `config.yaml`
  - `tweet_settings.pending_event_max_age_days: 7` を追加。0以下で無制限。
- `src/database.py` / `src/services/account_processor.py`
  - 未判定イベントキュー取得に `since_date` フィルタを追加。
  - 設定日数より古い未判定ツイートは、LLM判定せず `checked_for_event=True` に更新してbacklogを掃除するよう変更。
- `src/event_detector.py`
  - Gemini CLIの `QUOTA_EXHAUSTED` / `TerminalQuotaError` を検出し、stderrの `retryDelayMs` または `quota will reset after ...` からクールダウン期限を設定。
  - クールダウン中は `gemini-cli` を起動せず、`codex-cli` / APIフォールバックへ進める。quota時の長いスタックトレースは通常エラーとして出さない。
- テスト:
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py src\database.py src\services\account_processor.py tests\unit\test_event_detector_cli_fallback.py tests\unit\test_twitter_resume_safety.py` : OK
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py -q` : 3 passed
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_twitter_resume_safety.py -q` : 6 passed
  - `venv\Scripts\python.exe -X utf8 -m compileall src scripts` : OK
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 76 passed

## 2026-05-03 Codex CLIイベント判定のJSON安定化
- `src/event_detector.py`
  - Codex CLIへのプロンプト受け渡しを末尾の巨大なコマンドライン引数からstdin入力（`codex exec -`）へ変更。
  - 一時JSON Schemaを生成し、`codex exec --output-schema` で `is_event_related` / `confidence` / `event_type` / `event_date` / `participation_type` / `reason` のJSONオブジェクトに応答形を制約。
  - ツイート本文が空でも質問返しせず `is_event_related=false` のJSONを返すよう追加指示を明記。
- `tests/mock_codex_cli.py` / `tests/unit/test_event_detector_cli_fallback.py`
  - Codex CLIモックをstdin入力と `--output-schema` に対応。
  - フォールバック時にCodex CLIへツイート本文がstdinで渡り、schema指定も付くことを確認する回帰テストを追加。
- 検証:
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py tests\mock_codex_cli.py tests\unit\test_event_detector_cli_fallback.py` : OK
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py -q` : 2 passed
  - `venv\Scripts\python.exe -X utf8 scripts\util\judge_tweet.py "コミケに参加します。スペースは東A-12aです。" --model codex-cli` : JSON判定OK

## 2026-05-02 イベント判定バックグラウンド化 + API最終フォールバック
- `config.yaml`
  - 既定の `models` を `gemini-cli` → `codex-cli` → `gpt-5-nano` に変更。
  - CLI経路を優先し、最後にOpenAI APIへフォールバックする運用へ戻した。
- `src/event_detector.py`
  - OpenAI API / Gemini API 呼び出しを `asyncio.to_thread()` 経由に変更し、APIフォールバック時もイベントループを直接ブロックしないようにした。
- `src/services/account_processor.py`
  - Twitter通常巡回中の同期イベント判定をやめ、`all_tweets.checked_for_event=False` をバックグラウンド判定キューとして扱うようにした。
  - `schedule_pending_event_detection()` / `wait_for_pending_event_detection()` を追加し、巡回中は判定を裏で進め、終了時は残りを待って未通知・Hydrus再処理に接続する。
  - バックグラウンド判定でイベントを検出した場合、`event_tweets` 保存、ステータス加算、Discord通知、Hydrus event-only import まで進める。
- `main.py`
  - 起動時の未判定ツイートもバックグラウンド判定へ投入するようにした。
- `README.md` / `memory-bank/techContext.md`
  - LLMフォールバック順とイベント判定のバックグラウンド処理方針を更新。
- テスト:
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py src\services\account_processor.py main.py` : OK
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py tests\unit\test_twitter_resume_safety.py -q` : 7 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 74 passed

## 2026-05-01 イベント判定LLMフォールバック順の課金抑制
- `config.yaml`
  - 既定の `models` を `gemini-cli` → `codex-cli` に変更。
  - `gemini-2.5-flash` / `gpt-5-nano` は既定順から外し、APIキー課金を避ける。
  - `codex_cli` セクションを追加。`codex exec --model gpt-5.2` を既定にし、`--ephemeral` / `--ignore-rules` / `read-only` で分類用途に限定。
- `src/event_detector.py`
  - Codex CLI呼び出し `_analyze_with_codex_cli()` を追加。
  - Gemini CLI失敗時に `codex-cli` モデルへフォールバック可能にした。
  - `--output-last-message` の一時ファイルを使い、Codex CLIのログではなく最終JSONを優先して読む。
  - Codex CLIはstdinが開いていると入力待ちになるため、`stdin=subprocess.DEVNULL` で起動する。
- `requirements.txt`
  - `src/event_detector.py` の `from google import genai` に対応する `google-genai` を明示依存へ追加。
- `README.md` / `memory-bank/techContext.md` のLLMフォールバック説明を更新。
- テスト: `tests/unit/test_event_detector_cli_fallback.py` と `tests/mock_codex_cli.py` を追加。
- 検証:
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_event_detector_cli_fallback.py -q` : 1 passed
  - `venv\Scripts\python.exe -X utf8 -m py_compile src\event_detector.py tests\mock_codex_cli.py tests\unit\test_event_detector_cli_fallback.py` : OK
  - `venv\Scripts\python.exe -X utf8 tests\test_cli_integration.py` : OK
  - `venv\Scripts\python.exe -X utf8 tests\test_markdown.py` : OK
  - `codex exec --model gpt-5.2 ... "OK-GPT-5.2"` : OK
  - `EventDetector._analyze_with_codex_cli()` 経由の実Codex CLI疎通 : OK（JSON応答取得）
  - `--ask-for-approval` / `-a` は現行CLIで未対応のため設定から除外。

## 2026-04-25 CSV git同期機能
- `src/csv_git_sync.py` を追加。
  - 対象CSVは `monitored_accounts.csv` / `deleted_accounts.csv`。
  - 差分がある場合だけ対象ファイルを commit し、`pull --rebase --autostash` 後に push する。
  - `data/csv_git_sync.lock` で多重実行を防止し、古いロックは `lock_stale_seconds` 経過後に除去する。
- `main.py` に同期フックを追加。
  - Discord ingest と display_name補完後に `"startup"` 同期をバックグラウンド起動。
  - 30日超過アカウントを `deleted_accounts.csv` へ移動した後に `"archive"` 同期を予約。
  - 起動時同期中にアーカイブ同期が要求された場合は pending として保持し、終了後に再同期する。
- `config.yaml` に `csv_git_sync` を追加して有効化。
- テスト: `tests/unit/test_csv_git_sync.py` 追加。差分なし、差分あり、ロック中スキップを検証。
- 検証: `venv\Scripts\python.exe -X utf8 run_tests.py --quick` で 71 passed。

## 2026-04-24 ツイートイベント判定診断ツールの作成
- `scripts/util/judge_tweet.py` 新規作成
  - 任意のツイート本文を引数として受け取り、`EventDetector` のロジックで判定結果（JSON）を表示する
  - `config.yaml` のモデル設定を自動的に使用し、`--model` オプションでモデルの個別指定も可能
  - 判定理由（reason）に加えて、正規表現による抽出情報（サークル名、スペース番号など）も表示
- 検証: ニコニコ超会議の参加告知ツイートで実行し、期待通り `is_event_related: true` と判定されることを確認

## 2026-04-08 Hydrus重複削除タグ汚染リセットスクリプト
- `scripts/hydrus/reset_tags_by_url.py` 新規作成
  - 入力: `--hash <sha256>` + `--url <pixiv_artwork_url>`
  - Step 1: gallery-dlでPixivメタデータ再取得（タイトル、作者、タグ、x_restrict等）
  - Step 2: Hydrusで指定ハッシュ + URL検索で同artwork全ファイル特定
  - Step 3: `_generate_pixiv_tags()` + `pixiv_user:{user_id}` + CSV rank/custom_tags で正しいタグ生成
  - Step 4: 全既存タグ削除（全サービス対象）→ 新タグ追加 → URL関連付け → ノート更新
  - `--dry-run` で差分確認、`-y` で確認スキップ
  - Windows UTF-8出力対応済み
- テスト: artwork 93375065（7ページ）でdry-run成功。指定ファイルの58汚染タグ→13正しいタグに

## 2026-04-07 Hydrusタグ大規模移行 + cleanup自動化
- `scripts/fix/migrate_creator_to_twitter_user.py` 新規作成: 全Twitter画像のcreator:{username}→twitter_user:{username}一括移行
  - 対象: 959アカウント、441,725ファイル移行完了、エラー0件
  - 504件はcreator:タグがなくなるためcreator:{display_name}を追加（display_name空時は他画像から最頻値推定）
- `scripts/fix/fix_renamed_hydrus_tags.py` 新規作成: リネーム済み12アカウントのHydrusタグ修正（3172ファイル、エラー0）
- `scripts/hydrus/cleanup_creator_tags.py` 改善: URL自動判別フェーズ（Phase 1.5）追加
  - `fetch_all_creator_files()`で`known_urls`も取得するよう拡張
  - `auto_resolve_by_url()`: Twitter URLが1つだけ & creator:タグ一致で自動twitter_user:移動
- テスト: 64 passed, 3 failed（既存のasync/backupテスト、無関係）

## 2026-04-07 Twitter ID変更（スクリーンネーム変更）追跡機能
- 問題: Twitterアカウントがusernameを変更すると`user_by_login()`がNoneを返し「削除済み」と誤判定される
- 調査: twscrapeの`user_by_id(uid)`メソッド、ツイートURLリダイレクト、X API v2を比較検討。twscrape user_by_idが最適と判断（無料、Cloudflareリスクなし）
- 実装（Part 1: リカバリスクリプト）:
  - `scripts/fix/recover_renamed_accounts.py`: flagged_accounts.json+deleted_accounts.csvのTwitterアカウントをtwscrape tweet_details()で調査。RENAMED/DELETED/STILL_EXISTS/NO_TWEETSに分類。--applyで自動修正（CSV更新・フラグ解除・deleted→monitored復元）
- 実装（Part 2: クローラー恒久対策）:
  - `monitored_accounts.csv`: `twitter_id`カラム追加（既存1847行は空値、段階的に埋まる）
  - `main.py`: `_load_monitored_accounts_from_csv()`にtwitter_id読み込み追加
  - `src/twitter_monitor.py`: `check_account_reachable(username, twitter_id=None)` — user_by_login失敗時にuser_by_id()フォールバック、`_detected_renames`/`_resolved_twitter_ids`キャッシュ、`get_and_clear_detected_renames()`/`get_resolved_twitter_id()`メソッド追加
  - `src/services/account_processor.py`: process_account()でリネーム検出→CSV自動更新、`_try_save_twitter_id()`で通常処理後のtwitter_id書き戻し、`_update_account_in_csv()`汎用CSV更新メソッド追加
  - `src/services/discord_account_ingest.py`: ヘッダー/データ行にtwitter_idカラム追加
- テスト: 64 passed, 4 failed（既存のasync設定/backup progressテスト、無関係）

## 2026-04-07 FANBOXインポート時刻を現在時刻ベースに変更
- 問題: FANBOXインポート時に`_set_fanbox_import_times`がインポート時刻を投稿日時（work_date）に上書きしていたため、今日インポートした画像が過去の日時に設定され最新順で表示されなかった
- 修正: `src/hydrus_client.py`の`_set_fanbox_import_times`のベースを`time.time()`（現在時刻）に変更。同一投稿内の画像は1秒ずつオフセットで順序保証。1枚のみの投稿はスキップ
- `work_data`引数を削除し、`imported`リストのみ受け取るシンプルな設計に変更

## 2026-04-06 Poipiku DB保存エラー修正（Invalid isoformat string: ''）
- 問題: 4/5以降、全Poipiku作品がDB保存時に`Invalid isoformat string: ''`で失敗。ダウンロードは成功するが0件保存・0件Hydrusインポートが続いていた。
- 原因: `poipiku_extractor.py`が`date = ''`をハードコード（Poipikuページに日時情報なし）だが、`database.py`の`save_poipiku_works`(2712行)・`save_single_poipiku_log_only_work`(2749行)が空文字を`datetime.fromisoformat('')`に渡してクラッシュ。Pixivには同様のフォールバックがあったがPoipikuには欠けていた。
- 修正: `database.py`の両メソッドに空date時の`datetime.now()`フォールバックを追加
- テスト: 64 passed（既存の2 failedはbackup progressテストで無関係）

## 2026-04-05 creator: タグ重複問題の修正
- 問題: 1枚の画像に複数の`creator:`タグが付く（Twitterがusername+display_name両方をcreator:に追加、CSV名前変更、クロスプラットフォーム統合）
- `src/hydrus_client.py`: Twitter `_generate_tags`でusernameの`creator:`追加を`twitter_user:`に変更（1880-1883行）
- `scripts/hydrus/cleanup_creator_tags.py`: 既存重複creator:タグの対話式整理ツールを新規作成
  - Hydrus APIでcreator:*持ちファイル検索→同一組み合わせのグループ化→ユーザー選択→不要タグ削除/移動
  - dry-run（デフォルト）/--apply/--exportモード
  - 日本語名を優先する自動推薦機能、ASCII-onlyタグのtwitter_user:への自動移動
- タグ名前空間方針: `creator:`=表示名専用、`{platform}_user:`=プラットフォーム固有ID/ハンドル

## 2026-04-05 FANBOXインポート時��自動設定
- `src/hydrus_client.py`: `import_fanbox_images`完了後に`_set_fanbox_import_times`を呼び出し、work_date + ファイル順(1秒刻み)のタイムスタンプをHydrusのインポート時刻として自動設定
- Pixiv等で既にインポート済みの重複画像でも、FANBOXの投稿順序がHydrus上で正しく反映される
- 既存の`scripts/fix/fanbox_thumbnail_order.py`は事後修正用として残存
- テスト: 64 passed, 2 failed（既存のbackup progressテスト不安定性、無関係）

## 2026-04-05 deleted_accounts.csv 誤登録修正・twscrapeタイムアウト保護
- 問題: deleted_accounts.csvに登録された66アカウント中、大半が実際にはアクセス可能だった
- 原因1（Pixiv 26件）: manual_confirmedで手動登録されていたが、全てgallery-dlでアクセス可能を確認。手動チェック時の誤判定。
- 原因2（Twitter）: twscrape 0.17.0が`IndexError: list index out of range`で全アカウントをロックアウト→`user_by_login`がNone返却→「アカウント削除」と誤判定
- 対応:
  - 再チェックスクリプトで全66アカウントを検証（Pixiv: gallery-dl、Twitter: 直接GraphQLリクエスト）
  - 62アカウントをmonitored_accounts.csvに復元、3件（gomgodkk25/sagami_sagari2(Suspended)/h_s_i02(Suspended)）のみ削除維持
  - `src/twitter_monitor.py`: `check_account_reachable`/`_check_if_private_account`/`has_new_tweets_quick_check`に`asyncio.wait_for(timeout=60)`を追加
  - タイムアウト時は到達可能扱い（`_account_reachable`をFalseにしない）で誤フラグを防止
- テスト: 構文チェック通過（既存テスト失敗はpytest-asyncio未導入による無関係の問題）

## 2026-04-03 Privatter incomplete download修正
- 原因: 修正前のrunで作られたリトライキューに`../../img/blank.gif`を含む古い`media`リストが残存。修正後のrunでリトライワークが優先使用され、expected > actualでincomplete判定。
- `src/privatter_extractor.py`: `_expected_counts`属性追加。`_download_work_images`でページ再取得時のURL数を記録。
- `src/services/account_processor.py`: `_download_artwork_media`でextractorの`_expected_counts`をwork["file_count"]に反映。
- DB: `artwork_retry_queue`からprivatter:ebachi11を削除。
- テスト: 64 passed（既存の4 failedはPrivatter無関係）

## 2026-04-03 Privatterクローラー追加
- `src/privatter_extractor.py`: 新規作成（requests+BS4カスタムスクレイパー）
  - Cookie認証（`cookies/privatter.net_cookies.txt`）
  - ユーザーTL（`/u/{username}`）から画像投稿（`/i/{id}`）を収集
  - R-18判定不可→全投稿 `sensitive=True`
  - 投稿日時なし→IDの昇順ソートで古い順インポート
- `src/database.py`: PrivatterWork + PrivatterLogOnlyWork モデル追加
  - PLATFORM_MODELS/PLATFORM_IDENTITY_FIELDS/_ensure_hydrus_columns に追加
  - filter/save/update メソッド追加
- `src/hydrus_client.py`: import_privatter_images + _generate_privatter_tags 追加
- `src/services/account_processor.py`: ARTWORK_PLATFORM_SPECS に privatter 追加
  - コンストラクタ/ルーティング/_check_reachability/_record_unreachable 追加
- `main.py`: import/初期化/受け渡し/clear_reachability_cache/display_name解決 追加
- `config.yaml`: privatter セクション追加（enabled: true）
- `src/services/discord_account_ingest.py`: _extract_privatter_username 追加
- テスト: 64 passed, 2 failed（既存backup progressテストの不安定性、Privatter無関係）

## 2026-04-02 rate limit無限リトライ修正・情報管理整備
- `src/subprocess_utils.py`: rate limit検出時にstderrをログ出力するよう改善
- 全extractor（pixiv/kemono/fantia/nijie/skeb/misskey/gelbooru/fanbox/bluesky）に `rate_limit_retries=0` を追加（Twitter/gallery_dl_extractor.pyは既修正済み）
- `monitored_accounts.csv`: FANBOXアカウント7件追加・rankの修正
- `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `memory-bank/` を整備
- テスト結果: 既存テスト影響なし（rate_limit_retries=0は追加引数のみ）

## 2026-03-16 WSL2/Linux固有コードの削除
- `main.py`: L36-41の `pysqlite3` パッチブロックを削除
- `setup.sh`: ファイル全体を削除（Linux/WSL専用スクリプト）
- `setup.bat`: L43-48のWSL用venv検出・警告ブロックを削除
- `README.md`: Linux/WSLセットアップ手順を削除し、Windows専用に整理
- テスト: `run_tests.py --quick` 66 passed

## 2026-03-14 Pixiv artwork URLからのユーザーID解決対応
- `src/services/discord_account_ingest.py`
  - `_extract_pixiv_username()` に `/artworks/{artwork_id}` パターンと ロケールプレフィックス（`/en/`等）の対応を追加。
  - `_resolve_pixiv_artwork_accounts()` で gallery-dl 経由の artwork→user_id 非同期解決を追加。
  - コンストラクタに `pixiv_extractor` パラメータを追加。
- `src/pixiv_extractor.py`
  - `fetch_user_works_by_artwork_id()` を追加。
- `main.py`
  - `DiscordAccountIngestor` 初期化に `pixiv_extractor` を受け渡し。
- テスト
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_misskey_instances.py -q` : 8 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 66 passed


## 2026-03-14 Hydrus R-18 タグ確認ロジック修正
- `src/hydrus_client.py`
  - `all_tag_service_keys` が platform 個別サービス設定時でも legacy の `my tags` (`local tags`) を含むよう修正。
  - `_extract_display_tags_from_metadata()` を追加し、Hydrus の新形式 `metadata["tags"][service_key]["display_tags"]["0"]` と旧形式 `service_keys_to_statuses_to_display_tags` の両方からタグを抽出できるようにした。
  - `_check_file_exists_with_metadata()` / `_get_file_tags()` を新ヘルパー経由に切り替え、Twitter/Pixiv 等の local tags を見落とさないようにした。
- `tests/unit/test_sensitive_tagging.py`
  - mixed tag service 設定時でも legacy local tags を確認対象に残すテストを追加。
  - Hydrus 新旧メタデータ形式の両対応テストを追加。
- 実データ検証
  - 修正前: `venv\Scripts\python.exe -X utf8 scripts/fix/sync_twitter_account_sensitive.py --dry-run --username oreizmmiporin` → `files_already_tagged: 0`, `files_tagged: 1071`
  - 修正後: 同コマンド → `files_already_tagged: 1071`, `files_tagged: 0`
  - 対象ファイル `c7d6dbf1891ab132...` の Hydrus 生メタデータにも `rating:r-18` を含む EventMonitor タグが存在することを確認。
- テスト
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_sensitive_tagging.py -q` : 25 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 62 passed

## 騾ｲ謐励し繝槭Μ繝ｼ・・026-03-13謨ｴ逅・沿・・
### A. 逶ｴ霑代〒螳御ｺ・ｸ医∩・域怏蜉ｹ・・- artwork邉ｻ繧偵轡L蠕後☆縺蝉ｿ晏ｭ・蜿冶ｾｼ縲阪・騾先ｬ｡蜃ｦ逅・∈蠕ｩ譌ｧ・亥・莉ｶDL螳御ｺ・ｾ・■繧定ｧ｣豸茨ｼ峨・- `artwork_retry_queue` 繧貞ｰ主・縺励．L荳榊ｮ悟・/菫晏ｭ伜､ｱ謨嶺ｽ懷刀縺ｮ蜀崎ｩｦ陦悟ｰ守ｷ壹ｒ蟶ｸ險ｭ縲・- Kemono蜿悶ｊ縺薙⊂縺怜屓蜿守畑 `scripts/fix/backfill_kemono_retry_queue.py` 繧定ｿｽ蜉縲・- backup騾ｲ謐礼ｮ｡逅・ｒ `active_runs/recent_runs` 繝吶・繧ｹ縺ｸ謾ｹ蝟・＠縲・聞譎る俣蜃ｦ逅・ｸｭ縺ｮ逕溷ｭ倡｢ｺ隱阪ｒ蜿ｯ閭ｽ蛹悶・- rate-limit蠕・ｩ溽峩蠕後・隱､繧ｿ繧､繝繧｢繧ｦ繝医ｒ髦ｲ縺蝉ｿｮ豁｣繧貞渚譏縲・- Twitter resume螳牙・蛹厄ｼ域悴螳御ｺ・愛螳・譛ｪ騾夂衍蜀埼・譛ｪ螳御ｺ・ydrus蜀埼幕・峨ｒ蜿肴丐縲・- Pixiv/Kemono 繧・`蜈ｨ莉ｶDL髢句ｧ・+ work螳御ｺ・＃縺ｨ縺ｮ蜊ｳ菫晏ｭ倥・蜊ｳHydrus import` 縺ｫ螟画峩縲・- `tests/unit/test_artwork_incremental.py` 繧偵％縺ｮ迺ｰ蠅・〒螳溯｡悟庄閭ｽ縺ｪ蠖｢縺ｸ謨ｴ蛯吶＠縲∝・莉ｶ荳諡ｬ髢句ｧ九・蜊ｳ蜃ｦ逅・・retry 繧堤｢ｺ隱阪・- `run_tests.py` 縺ｮ pytest 蜻ｼ縺ｳ蜃ｺ縺励ｒ `sys.executable -m pytest` 縺ｫ邨ｱ荳縺励∽ｻｮ諠ｳ迺ｰ蠅・→蛻･ Python 繧定ｸ上・蝠城｡後ｒ隗｣豸医・- `run_tests.py --quick` 縺ｮ `pytest-xdist` 萓晏ｭ倥ｒ螟悶＠縲∵悴蟆主・譎ゅ・逶ｴ蛻怜ｮ溯｡後∈繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縺吶ｋ繧医≧菫ｮ豁｣縲・- GitHub Actions 縺ｮ unit/integration/slow 螳溯｡梧擅莉ｶ繧貞ｮ滓・縺ｫ蜷医ｏ縺帙∝ｭ伜惠縺励↑縺・integration 繝・ぅ繝ｬ繧ｯ繝医Μ繧・slow 繝・せ繝域悴螳夂ｾｩ縺ｧ關ｽ縺｡縺ｪ縺・ｈ縺・紛逅・・
### B. 譌｢遏･隱ｲ鬘鯉ｼ域悴螳御ｺ・ｼ・1. **螟夜Κ騾｣謳ｺ繝・せ繝医・荳崎ｶｳ・磯ｫ假ｼ・*
   - EventDetector / DiscordNotifier / backup騾｣謳ｺ縺ｮ繝ｦ繝九ャ繝医ユ繧ｹ繝医′荳崎ｶｳ縲・2. **繝舌ャ繧ｯ繧｢繝・・險ｭ險医・驕狗畑譁ｹ驥晏崋螳夲ｼ井ｸｭ・・*
   - DB菫晏ｭ倬・ｺ上→螟ｱ謨苓ｨｱ螳ｹ縺ｮ險ｭ險医ヨ繝ｬ繝ｼ繝峨が繝輔′譛ｪ遒ｺ螳壹・3. **memory-bank縺ｮ諠・ｱ魄ｮ蠎ｦ邂｡逅・ｼ井ｸｭ・・*
   - 驕主悉繝ｭ繧ｰ縺ｨ迴ｾ迥ｶ縺梧ｷｷ蝨ｨ縺励∝愛譁ｭ繧ｳ繧ｹ繝医′鬮倥＞縲・4. **lint / security 蟆守ｷ壹・謨ｴ逅・ｼ井ｸｭ・・*
   - `run_tests.py` 縺ｨ CI 繝ｯ繝ｼ繧ｯ繝輔Ο繝ｼ縺ｧ蛻ｩ逕ｨ繝・・繝ｫ繧・､ｱ謨玲擅莉ｶ縺ｮ邨ｱ荳縺後∪縺荳榊香蛻・・
---

## Kemono/Pixiv: 螟ｧ驥乗悴蜿門ｾ怜ｯｾ遲悶・螳溯｣・ｵ先棡

### 隕∫ｴ・- artwork蜃ｦ逅・・縲～1菴懷刀縺壹▽DL` 縺九ｉ `蜈ｨ莉ｶDL髢句ｧ・+ work螳御ｺ・う繝吶Φ繝磯ｧ・虚` 縺ｫ遘ｻ陦梧ｸ医∩縲・- extractor 縺御ｽ懷刀螳御ｺ・ｒ讀懃衍縺励◆譎らせ縺ｧ縲．B菫晏ｭ・/ Hydrus import / retry queue 蜿肴丐繧帝ｲ繧√ｋ縲・
### 繝｡繝ｪ繝・ヨ
- 蛻晏屓繧ｯ繝ｭ繝ｼ繝ｫ繧・backlog 螟ｧ驥乗凾縺ｧ繧・gallery-dl 繧・蝗槭〒襍ｷ蜍輔〒縺阪ｋ縲・- 譛蛻昴・菴懷刀螳御ｺ・凾轤ｹ縺九ｉ菫晏ｭ倥・Hydrus import 繧帝幕蟋九〒縺阪ｋ縲・- log-only 繧ょ酔縺倡ｵ瑚ｷｯ縺ｧ謾ｹ蝟・＆繧後ｋ縲・
### 繝ｪ繧ｹ繧ｯ/豕ｨ諢冗せ
- extractor 蛛ｴ縺ｮ螳御ｺ・愛螳壹・縲梧悄蠕・ヵ繧｡繧､繝ｫ謨ｰ蛻ｰ驕・+ 荳螳壽凾髢薙し繧､繧ｺ螟牙喧縺ｪ縺励阪↓萓晏ｭ倥☆繧九・- gallery-dl 蛛ｴ縺ｮ蜃ｺ蜉帛ｽ｢蠑上ｄ繝輔ぃ繧､繝ｫ蜻ｽ蜷崎ｦ丞援縺悟､峨ｏ繧九→縲∝ｮ御ｺ・､懃衍繝ｭ繧ｸ繝・け縺ｮ霑ｽ蠕薙′蠢・ｦ√・- 繧ｰ繝ｭ繝ｼ繝舌Ν Python 縺ｧ縺ｯ萓晏ｭ倅ｸ崎ｶｳ縺ｮ縺ｾ縺ｾ螟ｱ謨励＠蠕励ｋ縺溘ａ縲∵､懆ｨｼ譎ゅ・ `venv\Scripts\python.exe` 繧剃ｽｿ縺・燕謠舌ｒ蜈ｱ譛峨＠縺ｦ縺翫￥蠢・ｦ√′縺ゅｋ縲・
### 讀懆ｨｼ邨先棡
1. `pytest tests/unit/test_artwork_incremental.py -q` : 7 passed
2. `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 55 passed
3. `venv\Scripts\python.exe -X utf8 run_tests.py` : unit 騾夐℃縲（ntegration 縺ｯ蟇ｾ雎｡縺ｪ縺励〒 skip
4. `venv\Scripts\python.exe -X utf8 run_tests.py --integration` : 蟇ｾ雎｡縺ｪ縺励〒 skip
5. `venv\Scripts\python.exe -X utf8 run_tests.py --slow` : 蟇ｾ雎｡縺ｪ縺励〒 skip

---

## 驕狗畑繝｡繝｢・域紛逅・Ν繝ｼ繝ｫ・・
- 縺薙・繝輔ぃ繧､繝ｫ縺ｯ縲檎樟迥ｶ譛牙柑縺ｪ騾ｲ謐励→譛ｪ螳瑚ｪｲ鬘後阪↓髯仙ｮ壹☆繧九・
- 隱ｿ譟ｻ繝ｭ繧ｰ縺ｮ隧ｳ邏ｰ譎らｳｻ蛻励・蠢・ｦ∵怙蟆城剞縺ｮ縺ｿ谿九＠縲・㍾隍・・螳ｹ縺ｯ霑ｽ險倥○縺壽峩譁ｰ縺ｧ鄂ｮ謠帙☆繧九・
- 螳御ｺ・ｸ医∩縺九▽驕狗畑蛻､譁ｭ縺ｫ荳崎ｦ√↑隧ｳ邏ｰ縺ｯ縲∝挨繝ｭ繧ｰ/Issue縺ｸ騾驕ｿ縺励※閧･螟ｧ蛹悶ｒ髦ｲ縺舌・

---

## 2026-03-13 Twitter incremental gap fix
- `src/twitter_monitor.py`
  - quick check を最新2件固定から設定値ベースの recent window scan に変更
  - 増分取得に `incremental_overlap_hours` の重なり期間を導入（デフォルト48時間）
  - 最初の既知ツイート即停止を廃止し、既知ツイート連続件数で停止する方式へ変更
- `tests/unit/test_twitter_gap_recovery.py`
  - 先頭2件が既知でも3件目の欠損を検出できることを追加検証
  - 最新保存ツイートの直後にある欠損ツイートを回収できることを追加検証
- 検証結果
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_twitter_gap_recovery.py -q` : 2 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 57 passed

## 2026-03-13 Twitter focused recovery / Hydrus import time
- 直取得で回収
  - `https://x.com/S7_D82/status/2031592287034290469`
  - `https://x.com/29herase/status/2032093421167656965`
- DB確認
  - `2031592287034290469` : `hydrus_expected_count=3`, `hydrus_imported_count=3`
  - `2032093421167656965` : `hydrus_expected_count=2`, `hydrus_imported_count=2`
  - `2032135476484981128` : 既存レコードあり、ローカル画像も存在
- `scripts/fix/import_times_twitter.py`
  - `--created-on YYYY-MM-DD` を追加
  - URL検索より先に `local_media` 順のハッシュ解決を行うよう変更
- 実行結果
  - `venv\Scripts\python.exe -X utf8 scripts\fix\import_times_twitter.py --dry-run --created-on 2026-03-13 --limit 8 --reset` : OK
  - `venv\Scripts\python.exe -X utf8 scripts\fix\import_times_twitter.py --created-on 2026-03-13 --reset` : 161 tweets, 186 files found, 184 updated, 3 not found, 2 errors
  - 2 errors は Hydrus 側に import time が存在しないファイルで、API が新規追加ではなく既存編集のみ許可しているため 400 を返したもの

## 2026-03-13 Kemono/Pixiv artwork を逐次処理へ戻す
- 調査結果
  - `7715ad5` で Pixiv/Kemono artwork が streaming 化されていた
  - 1週間前に安定していた `ffb6aab` / `6ae20c0` では retry queue はありつつ、処理は 1件ずつの逐次実行だった
  - `fanbox/43115256` の件は retry queue 未再開ではなく、large backlog を 1 回の streaming gallery-dl に載せて timeout していた
- 実装
  - `src/services/account_processor.py`
    - Pixiv/Kemono artwork の streaming 分岐を削除し、逐次経路だけを残した
    - これに伴い `_supports_artwork_streaming()` と streaming 専用メソッド群を物理削除
  - `src/kemono_extractor.py`
    - `stream_download_media_for_works()` と streaming 専用 helper を削除
    - streaming 専用で残っていた `max_batch_size` 読み込みも削除
  - `src/pixiv_extractor.py`
    - `stream_download_media_for_works()` と streaming 専用 helper を削除
    - streaming 専用で残っていた `max_batch_size` 読み込みも削除
  - `src/subprocess_utils.py`
    - `run_with_idle_timeout()` の rate-limit retry を既定で無制限に変更
    - rate-limit と判定できる場合のみ待機して再試行、非 rate-limit では従来どおり即終了
    - 使われなくなった `run_with_idle_timeout_stream()` を削除
  - `src/rate_limit_utils.py`
    - `Retry-After` や本文の待機秒数が取れない rate-limit は、指数バックオフではなく固定 60 秒待機へ変更
    - `request_with_rate_limit_retry()` の既定 retry 上限を外し、rate-limit の間は固定間隔で再試行するよう統一
  - `tests/unit/test_artwork_incremental.py`
    - streaming 前提テストを逐次処理の回帰テストへ差し替え
    - DummyExtractor に残っていた streaming stub も削除
  - `tests/unit/test_rate_limit_handling.py`
    - 無制限 retry の回帰テストを追加
    - 明示秒数なし rate-limit が固定 60 秒待機になることを追加確認
- 速度差メモ
  - `subprocess_utils.py` 自体に gallery-dl を 1 本へ制限するグローバルロックはなかった
  - `main.py` の platform 内アカウント処理は以前から直列で、今回の速度差の主因ではない
  - 主因は `7715ad5` の「全 retry backlog を 1 本の streaming gallery-dl にまとめる」変更と判断
- 検証
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_artwork_incremental.py -q` : 6 passed
  - `venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_rate_limit_handling.py -q` : 9 passed
  - `venv\Scripts\python.exe -X utf8 run_tests.py --quick` : 59 passed

## 2026-03-14 Kemono クローラー速度ログ調査
- 対象ログは `logs/app_20260314_171638.log`。2026-03-14 22:52:12 JST 時点で進行中の `fanbox/43115256` バックログ処理を解析した。
- `Downloading media for 1 kemono works in a single run` 開始時刻と `Moved ... kemono media files` 完了時刻の差を DL 時間とし、保存済み work 単位ファイルサイズ合計から実効速度を算出した。
- 集計結果
  - 既知サイズ 102 件: 11.29 GiB / 5.02 時間 = 0.64 MiB/s（約 5.4 Mbps）
  - 成功 import 69 件: 9.45 GiB / 2.85 時間 = 0.94 MiB/s（約 7.9 Mbps）
  - 直近 20 件: 加重平均 1.10 MiB/s、中央値 0.53 MiB/s
- 代表ケース
  - `fanbox_7986037`: timeout ありでも 171.5 MiB を 394.9 秒で 0.43 MiB/s
  - `fanbox_7975749`: 1204.9 MiB を 513.2 秒で 2.35 MiB/s
  - `fanbox_7966561`: 992.3 MiB を 911.3 秒で 1.09 MiB/s
- 極端に遅い値は 0.15 MiB 前後の incomplete 1-file 投稿が中心で、kemono CDN の転送性能そのものより待ち時間・partial 終了の影響が大きい。
## 2026-03-14 Kemono 壁時計ベース再集計
- `logs/app_20260314_171638.log` の `fanbox/43115256` backlog を壁時計ベースで再集計し、`107/420 works`、`1380 files moved`、`5時間57分52秒` 経過時点で `15.56秒/ファイル`、`200.67秒/作品`、`231.84ファイル/時` を確認。
- 直近の 107 件目は `23:05:50.899` 開始から `23:16:05.594` 時点まで `10分14.7秒` 経過で、`76 files moved` ベースの暫定ペースは `8.09秒/ファイル`。
- 前回回答の `MiB/s` は帯域値であり、ユーザー意図の「処理全体の遅さ」を表していなかったため、今後は Kemono 進捗確認時に壁時計ベースの `秒/ファイル` も併記する。
