---
name: merge
description: 実装済み PR / branch を検証して base branch へ統合する。対象未指定なら merge 可能な対象を順に処理し、mobile 変更は APK build / GitHub Release upload / latest.json 更新まで完了条件に含める。
---

# merge

統合モード。対象 PR または target branch を専用 merge worktree で検証し、base branch へ統合する。対象が指定されていない場合は、base branch 向けの open PR / merge 候補 branch を列挙し、順に処理する。

## 絶対ルール

- 現在の checkout を信用せず、base branch と merge 対象を最初に確定する。
- 現在 checkout の既存差分を merge 作業に巻き込まない。
- 現在 checkout が `main` / `master` / base branch でも別ブランチでも、そこでは merge しない。現在 checkout は状態確認と `fetch` の起点としてだけ使う。
- 各対象ごとに最新の `origin/<base-branch>` から専用 merge worktree と一時 merge branch を作り、そこで検証・merge・push する。
- 競合、drift、検証失敗は、既存コード、docs、scripts、履歴、差分内容を読んで解決する。
- 無関係な機能追加やリファクタをしない。
- 裸の `git push` は使わず、push 先 branch を明示する。
- `/work` の完了報告または PR 本文から、通常検証、build / env 前提、Debug policy、Heavy debug result を確認する。
- merge では、target を base に載せた最終統合状態に対する検証を行う。`/work` 済みの通常検証を読まずに無条件で重い `/debug` をやり直さない。
- `package.json` に scripts がある場合は、repo の package manager と既存手順に従って、差分範囲と統合リスクに対応する scripts を実行する。このテンプレートでは JS/TS 差分に `typecheck`、web 差分に `build:web`、desktop 差分に `build:desktop`、mobile 差分に既存 release / APK build script を実行する。これは通常検証 / 統合検証であり、スクリーンショット等の重い `/debug` ではない。
- merge worktree で build/runtime 検証に `.env`、依存、SDK、外部サービスが必要な場合は、merge worktree 側に前提を用意してから実行する。`.env*` は git worktree で自動共有されないため、必要なら元 checkout から merge worktree へコピーまたはプロジェクト標準の env 供給手順を使う。`.env*` や secrets は commit しない。
- work / merge で作成した一時 worktree の親ディレクトリも cleanup 対象に含める。対象 worktree / merge worktree を削除した後、親ディレクトリが空で、かつ今回作成した一時ディレクトリであることを絶対パスで確認できる場合は削除する。
- `git worktree remove` が `.env*`、`node_modules` の junction / symlink、build artifacts など ignored の検証前提で失敗した場合は、対象 path が今回作成した worktree 内に収まることを確認し、それらだけを削除してから再試行する。
- 重い `/debug` は、`debug-policy` が `on`、または `auto` かつ統合前確認が必要な場合だけ実行する。`/work` の Heavy debug result が `needed-before-merge` / 記録なしでリスクがある場合も、`debug-policy` が `auto` または `on` の場合だけ実行対象にする。
- `mobile/` 配下に target と base の差分がある場合、APK build、version / versionCode 確認または更新、GitHub Release upload、`latest.json` 更新または整合確認は必須。これを省略して merge 完了にしない。
- `scripts/check_mobile_release_gate.ps1` が存在する場合は最初に実行し、`RELEASE_REQUIRED=True` ならその結果を release 完了条件として扱う。
- GitHub Release upload 先 repo は、ユーザー指示、`memory-bank/`、docs、repo 設定、release scripts、`RELEASE_REPO`、Git remote、過去 release 情報から解決する。一般的な「公開リポジトリは何ですか？」とは聞き返さない。

## 開始時

```bash
git status --short --branch
git branch --show-current
git remote -v
git worktree list
git fetch origin
```

対象未指定の場合は、`gh pr list`、remote branch、既存 PR 情報から base branch 向けの未統合対象を列挙する。対象ごとに、既存 path / branch と衝突しない専用 merge worktree と一時 merge branch を作る。

```bash
git worktree add -b <merge-branch> <merge-worktree-path> origin/<base-branch>
```

## 手順

1. base branch と merge 対象一覧を確定する。
2. 対象を1件ずつ処理する。
3. `git fetch origin` 後、最新の `origin/<base-branch>` から専用 merge worktree と一時 merge branch を作成し、その worktree に移動する。一時 merge branch は base branch と同名にしない。
4. target と base の差分を確認し、`mobile/` 変更の有無を判定する。
5. 正しさ、回帰リスク、リポジトリ規約との整合性をレビューする。
6. `/work` の完了報告または PR 本文から、通常検証、build / env 前提、Debug policy、Heavy debug result を確認する。
7. merge worktree で必要な検証前提をそろえる。build/runtime 検証に `.env*` が必要ならコピーまたは標準手順で供給し、commit 対象に含めない。
8. `mobile/` 変更がある場合は、merge 前に release 前提を確定する。
   - build script
   - version / versionCode
   - GitHub Release tag
   - APK asset 名
   - `latest.json` の場所と更新方法
   - GitHub Release upload 先 repo
9. 同じ version / tag / asset が既に公開済みで、上書き方針が標準化されていない場合は、target branch 側で version / versionCode など必要最小限を更新し、再検証してから merge する。
10. target を一時 merge branch に merge する。競合が出た場合は、コードと意図を読んで解決し、解決後に検証する。
11. 最終統合状態で、差分範囲と統合リスクに対応する `package.json` scripts や build を実行する。
12. `debug-policy` が `on`、または `auto` かつ Heavy debug result が `needed-before-merge` / 記録なしでリスクがある / 統合前確認が必要な場合だけ、`/debug` / `/desktop-debug` / `/mobile-debug` を実行する。
13. `mobile/` 変更がある場合は APK をビルドし、GitHub Release へアップロードし、`latest.json` を更新または整合確認する。
14. 検証と必須 release が完了したら base branch へ merge し、`git push origin HEAD:<base-branch>` で push する。
15. merge / push / 必須 release が成功したら、target branch が base/default/protected/release/hotfix 系でない限り remote target branch を削除する。
16. target branch を checkout している作業 worktree がある場合は、`git status --short --branch` で clean か確認する。clean なら `git worktree remove <target-worktree-path>` で削除し、削除後に親ディレクトリが空で、かつ今回作成した一時ディレクトリだと絶対パスで確認できる場合は親ディレクトリも削除する。worktree remove が ignored の検証前提で失敗した場合は、対象 path がその worktree 内に収まることを確認し、それらだけを削除してから再試行する。未コミット差分がある、別作業中と判断できる、または対象特定に不確実性がある場合は削除せず、理由を報告する。
17. target branch が base/default/protected/release/hotfix 系でなく、どの worktree にも checkout されていなければ local target branch を削除する。worktree が残っているため削除できない場合は理由を報告する。
18. merge worktree に未コミット差分がなければ `git worktree remove <merge-worktree-path>` で削除し、削除後に親ディレクトリが空で、かつ今回作成した一時ディレクトリだと絶対パスで確認できる場合は親ディレクトリも削除する。worktree remove が ignored の検証前提で失敗した場合は、対象 path が merge worktree 内に収まることを確認し、それらだけを削除してから再試行する。
19. 不要になった一時 merge branch を削除する。
20. 複数対象がある場合は、push 後に `git fetch origin` し、次の対象は更新済みの `origin/<base-branch>` から新しい merge worktree で処理する。
21. 結果を `memory-bank/` に反映する。
## 完了報告

対象ごとに以下を含める。

- Base branch
- Target branch または PR URL
- Merge worktree path / cleanup result
- Worktree parent directory cleanup result
- Temporary merge branch / cleanup result
- Merge result
- Push result
- Target worktree cleanup result
- Target branch cleanup result
- Mobile changed
- APK build result
- Version / versionCode / tag / asset result
- GitHub Release upload result
- `latest.json` result
- Work verification summary
- Merge verification result
- Typecheck / test / build scripts result
- Debug policy / Heavy debug result
- Env / dependency prerequisite result
