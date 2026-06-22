---
name: work
description: リポジトリに変更を加える作業を行う。原則としてタスク専用 worktree で作業し、merge はしない。
---

# work

変更作業モード。実装、修正、整理、設定変更、ドキュメント更新など、リポジトリに差分を作る作業を扱う。

## ルール

- merge しない。
- 原則として、現在の checkout では編集せず、タスク専用 worktree と作業ブランチで作業する。
- 既存のユーザー差分を巻き込まない。作業前と完了前に `git status --short --branch` を確認する。
- `main` / `master` / base branch に直接 commit / push しない。ただし、ユーザーが base branch でそのまま作業すると明示した場合は、PR を作らず base branch へ commit / push する。
- 破壊的操作、release、upload、deploy は work の責務外とし、必要なら専用の merge / release 手順で扱う。
- 作業開始時にゴール、完了条件、通常検証、重い `/debug` の要否を先に決める。
- `/work` は通常検証と修正ループを行う。通常検証には typecheck、test、lint、対象スクリプト実行、必要な build を含めてよい。
- スクリーンショット取得、ブラウザ目視、実機、エミュレーター、Tauri/Expo の実起動などの重い `/debug` は `memory-bank/debug-policy.md` が `auto` または `on` の場合だけ実行する。`off` は重い `/debug` を止める指定であり、通常検証を省略する指定ではない。
- worktree で通常検証や build に `.env`、依存、SDK、外部サービスが必要な場合は、worktree 側に前提を用意してから実行する。`.env*` は git worktree で自動共有されないため、必要なら元 checkout から worktree へコピーまたはプロジェクト標準の env 供給手順を使う。
- `.env*` や secrets は commit しない。前提を用意できない検証は成功扱いにせず、未実行理由と merge 前に必要な検証として記録する。
- 無関係なリファクタをしない。
- 完了時は作業内容を commit し、作業ブランチを remote に push する。ローカルだけの worktree 差分を完了形にしない。
- 可能なら PR を作成する。PR を作成できない場合でも、merge が検知できる remote branch 名と PR title/body を残す。

## 開始時

作業前に確認する。

```bash
git status --short --branch
git branch --show-current
git remote -v
git worktree list
```

新規タスクでは、base branch からタスク専用 worktree と作業ブランチを作る。

```bash
git worktree add -b <work-branch> <worktree-path> <base-branch>
```

worktree 作成後、今回の完了条件に build/runtime 検証が含まれる場合は、検証前提をそろえる。

- repo の package manager に従って依存を用意する。
- `.env*` が必要なら、元 checkout の対応ファイルを worktree へコピーするか、README / `memory-bank/techContext.md` / docs に書かれた env 供給手順を使う。
- `.env.example` しかない場合は、それで足りる検証だけ実行する。secret が必要な検証は未実行として記録する。
- コピーした `.env*` が ignored であることを確認し、commit 対象に含めない。

既存 worktree / 既存作業ブランチで続行してよいのは、ユーザーが同一タスクの続きだと明示していて、差分範囲が今回作業と一致する場合だけ。

## 作業中

- 必要なファイルだけを読む。
- 最小の安全な差分で編集する。
- 最初に決めた通常検証を実行する。失敗した場合は原因を修正し、同じ検証を再実行する。
- build が完了条件の証明に必要なら `/work` で実行する。
- `debug-policy` が `auto` または `on` で重い `/debug` が必要な場合だけ、`/debug` / `/desktop-debug` / `/mobile-debug` の手順に進む。
- 仕様や手順が必要な場合だけ `docs/` を参照する。
- `memory-bank/debug-policy.md` が存在する場合だけ、デバッグ要否の判断材料として読む。

## 完了形

完了時は、作業形態に合う出口を残す。

- 作業内容を commit する。
- 作業ブランチ / worktree で作業した場合は、作業ブランチを remote に push し、可能なら PR を作成する。
- PR を作成できない場合でも、merge が検知できる remote branch 名、base branch、PR title/body を完了報告に残す。
- ユーザー指示で base branch 上に直接作業した場合は、PR を作成せず、base branch への commit / push 結果を完了報告に残す。
- push されていない worktree やローカル branch だけの状態を完了形にしない。

報告には以下を含める。

- 変更内容
- 検証結果
- 通常検証で実行したコマンド
- build / env 前提の有無と結果
- Debug policy: `off` / `auto` / `on`
- Heavy debug result: `done` / `skipped` / `needed-before-merge`
- Base branch
- Work branch
- Remote branch
- Worktree path
- PR URL、または PR 未作成の場合は PR title/body
- Commit hash
- Push result
- Base branch direct work の有無
