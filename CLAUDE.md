# Agent Guide


<!-- agent-absolute-gates:v1 -->
## 絶対ゲート

- merge/release では、存在する場合は必ず `scripts/check_mobile_release_gate.ps1` を先に実行し、`RELEASE_REQUIRED=True` なら APK build / GitHub Release upload / `latest.json` 更新を完了条件に含める。

- 長い本文より先にこのゲートを適用する。迷ったら本文解釈で省略せず、このゲートに従う。
- 作業開始時と完了前に `git status --short --branch` と対象差分を確認し、ユーザー変更を巻き込まない。
- `mobile/` 配下に差分がある merge/release では、ユーザーが明示的に `releaseなし` / `APK不要` / `upload不要` と言わない限り、APK build、GitHub Release upload、`latest.json` 更新まで必須。
- `debugなし` は実機・エミュレーター等の手動デバッグだけを省略する指定。typecheck、build、release、upload、metadata更新は省略しない。
- `docs/` や scripts に公開・ビルド手順がある場合は必ず従う。docs を理由に手順を省略しない。
- 完了報告には mobile changed / release required / build / upload / metadata / debug の結果を明記する。

## Codex運用の強制ルール

- Skill、`CLAUDE.md`、`memory-bank/` は UTF-8 として読む。PowerShell で文字化けする場合は `Get-Content -Encoding UTF8` を使う。読めない指示を推測で無視して作業を続けない。
- `/work` では、原則として作業前に base branch から作業ブランチを作る。`main` / `master` / base branch に直接 commit / push しない。ただし、ユーザーが今回の依頼で「mainブランチでそのままやっていい」などを明示した場合は、base branch 上での編集・commit・push を許可する。
- 「push」は通常は作業ブランチの push を意味し、ユーザーが base branch への直接 push を明示した場合だけ base branch への push として扱う。
- `CLAUDE.md` の一般的な push 指示より、`.agents/skills/work/SKILL.md` のブランチルールを優先する。
- `/merge` では、対象ブランチ/PRと base branch を確定し、必須検証と必要なビルドを実行してから統合する。
- リリース対象プロジェクトの `/merge` では、`memory-bank/` と `docs/` のリリース方針に従い、GitHub Release / `latest.json` 更新まで扱う。ユーザーが「releaseなし」と明示した場合だけ省略する。
返答・ログ・コミットメッセージは必ず日本語で書く。

## 基本方針

- 作業開始時は必ず `memory-bank/` を読む。詳細仕様が必要な場合だけ `docs/` を参照する。
- ユーザー指示は原則 `/ask` `/work` `/debug` `/merge` に分類する。
- 質問・調査・レビューは `/ask`、ファイル変更を伴う作業は `/work`、実装・修正後の動作確認は `/debug`、統合は `/merge` を使う。
- WebUIやブラウザUIの画面確認は `/debug`、Tauri desktop 固有の確認は `/desktop-debug`、Expo/React Native mobile の確認は `/mobile-debug` を使う。
- 既存差分を勝手に戻さない。破壊的操作は事前承認を得る。
- 文字化けと断定せず、必要ならエンコード指定やバイト列で確認する。
- テスト用に起動したサーバー・プロセスは、確認後に停止する。バックグラウンドで放置しない。
- 変更を行った場合は、/work の作業ブランチで commit と push まで進める。既存の無関係な差分は巻き込まない。

## リポジトリ種別

Python daemon / crawler / Discord notifier

- Python daemon/CLI: `main.py --daemon`、`src/`、`scripts/` が中心。Python実行は `venv/Scripts/python.exe` を使う。
- Twitter/X Cookie、監視CSV、収集DB、ログ、`.coverage` は作業対象外ならコミットしない。
- 通知・HuggingFaceバックアップ・Hydrus連携は外部環境依存なので、変更時は設定とdry-run/安全なサンプルで確認する。

## Memory Bank

Memory Bank はAI向け一次情報。次セッションは `memory-bank/` を最初に読む前提で作業する。

- 作業開始時: `projectbrief.md`、`productContext.md`、`systemPatterns.md`、`techContext.md`、`activeContext.md`、`progress.md` を確認する。
- 作業終了時: 実装や運用判断が変わった場合は `activeContext.md` と `progress.md` を更新する。
- 仕様・前提・方針が変わった場合は、active/progressだけで済ませず、該当する恒久ファイルも更新する。
- `memory-bank/debug-policy.md` は `/work` 後から `/merge` 前までのデバッグ実行方針。`mode` は `auto` / `always` / `off` のいずれか。
- `memory-bank/template-initialization.md` が残っている場合はテンプレート初期化未完了の印。内容を読み、初期化完了後に削除する。

## docs

- `docs/` はユーザー向けの詳細仕様、セットアップ、運用手順を置く場所。
- AI向けの設計判断や引き継ぎ情報は `memory-bank/` に置く。
- 設計書は `memory-bank/design-*.md` を優先する。ユーザーが読む必要のある手順だけ `docs/` に置く。
- Mobile 自動更新を採用する場合だけ `docs/mobile-auto-update-standard.md` を仕様として残す。採用しない場合は作らない・残さない。
- GitHub Release に公開するプロジェクトでは、公開用 Public リポジトリ、`latest.json`、asset名を `memory-bank/techContext.md` に記録する。

## Git

- コミット前に `git status --short --branch` を確認する。
- コミットメッセージは日本語で簡潔に書く。
- 「pushしろ」と言われた場合、未コミットの変更があれば、base branch ではないことと対象差分を確認して `add`→`commit`→`push` を実行する。
- バイナリ、ログ、`.env`、トークン、Cookie、生成物は明示された作業対象でない限りコミットしない。