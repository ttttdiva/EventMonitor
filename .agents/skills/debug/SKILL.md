---
name: debug
description: 実装・修正後の重いデバッグ検証。WebUI、Tauri desktop、Expo mobile、Python処理を実画面・実行環境寄りに確認する。
---

<!-- agent-absolute-gates:v1 -->
## 絶対ゲート

- merge/release では、存在する場合は必ず `scripts/check_mobile_release_gate.ps1` を先に実行し、`RELEASE_REQUIRED=True` なら APK build / GitHub Release upload / `latest.json` 更新を完了条件に含める。

- 長い本文より先にこのゲートを適用する。迷ったら本文解釈で省略せず、このゲートに従う。
- 作業開始時と完了前に `git status --short --branch` と対象差分を確認し、ユーザー変更を巻き込まない。
- `mobile/` 配下に差分がある merge/release では、ユーザーが明示的に `releaseなし` / `APK不要` / `upload不要` と言わない限り、APK build、GitHub Release upload、`latest.json` 更新まで必須。
- `debugなし` は実機・エミュレーター等の手動デバッグだけを省略する指定。typecheck、build、release、upload、metadata更新は省略しない。
- `docs/` や scripts に公開・ビルド手順がある場合は必ず従う。docs を理由に手順を省略しない。
- 完了報告には mobile changed / release required / build / upload / metadata / debug の結果を明記する。


# debug

実装・修正後に呼び出す重いデバッグ検証モード。通常の typecheck、test、lint、対象スクリプト実行、必要な build は `/work` や `/merge` でも行う。このスキルでは、スクリーンショット、ブラウザ目視、実機、エミュレーター、Tauri/Expo 実起動など、実行環境・画面確認寄りの検証を扱う。

**「ビルドが通った」「HTTP 200が返った」だけではUIデバッグ完了にしない。画面を持つ変更はスクリーンショットを撮り、内容を目視確認する。**

## ルール

- 変更した画面は必ず実画面で確認する。
- 変更していない画面は、必要に応じてDOM・ログ・主要コマンドで低コストに確認する。
- 問題が見つかったら、修正して同じ確認を再実行する。
- 起動した開発サーバーや一時プロセスは、作業終了時に停止方法を確認する。
- テンプレート初期化後は、そのプロジェクトの `memory-bank/techContext.md` と `README.md` の起動手順を優先する。
- `/work` / `/merge` から呼ぶ場合は、`memory-bank/debug-policy.md` が `auto` または `on` のときだけ実行する。ユーザーが明示的に `/debug` を指示した場合は、その指示を優先する。

## 1. 変更範囲の特定

```bash
git diff --name-only HEAD
```

未コミット差分がない場合は直近コミットとの差分を見る:

```bash
git diff --name-only HEAD~1
```

目安:

- `packages/web/**` → WebUI確認
- `packages/desktop/**` → Tauri desktop確認
- `packages/mobile/**` → mobile確認。詳細は `/mobile-debug`
- `packages/shared/**` → 依存先のWebUI、desktop、mobileを確認
- `src/**`, `scripts/**` → Python処理、CLI、バッチ、連携処理を確認
- `.github/**`, `package.json`, `pnpm-lock.yaml` → CI・依存・ビルド確認

## 2. 共通チェック

リポジトリ全体で成立する場合:

```bash
pnpm install
pnpm typecheck
```

Pythonコードを変更した場合:

```bash
python -m compileall src scripts
```

テストが存在する場合は、変更範囲に近いものから実行する:

```bash
pytest
```

## 3. WebUI確認

`packages/web` を採用しているプロジェクトで実行する。

```bash
pnpm --filter web dev
```

Next.js の表示URLを確認し、通常は `http://127.0.0.1:3000` をブラウザで開く。ポートが変わった場合はサーバーログのURLを使う。

確認項目:

- [ ] 変更したページが表示される
- [ ] スケルトン、空画面、エラー画面のまま止まっていない
- [ ] 日本語が文字化けしていない
- [ ] ボタン、フォーム、ナビゲーションが崩れていない
- [ ] コンソールにエラーが出ていない
- [ ] 操作が必要な機能はクリック・入力後の状態まで確認した

変更していない主要画面は、DOMスナップショットやコンソールエラー中心に確認し、異常時だけスクリーンショットを追加する。

## 4. Tauri Desktop確認

`packages/desktop` を採用しているプロジェクトで実行する。詳細な手順が必要な場合は `/desktop-debug` を使う。

まずWebビュー相当の画面をViteで確認する:

```bash
pnpm --filter desktop dev
```

通常は `http://127.0.0.1:5173` を開き、変更した画面をスクリーンショットで確認する。

Tauri API、ファイルアクセス、ウィンドウ挙動、Rust側を変更した場合はネイティブ起動も確認する:

```bash
pnpm --filter desktop tauri:dev
```

ビルド確認:

```bash
pnpm --filter desktop build
```

`packages/desktop/src-tauri/**` を変更した場合は、必要に応じてTauriビルドも行う:

```bash
pnpm --filter desktop tauri:build -- --no-bundle
```

## 5. Mobile確認

`packages/mobile` を採用しているプロジェクトでは `/mobile-debug` を使う。

最低限の静的確認:

```bash
pnpm --filter mobile typecheck
```

UIや画面遷移を変更した場合は、AndroidエミュレーターとADBでスクリーンショットを取得し、タップ・スワイプ操作まで確認する。

## 6. Python / Script確認

`src/**` や `scripts/**` を変更した場合は、対象コマンドを実データまたは安全なサンプルで実行する。

確認項目:

- [ ] 正常系が完了する
- [ ] 失敗時に理解できるエラーを返す
- [ ] 入出力ファイルの場所がREADMEやmemory-bankと一致する
- [ ] Windowsのパス、空白を含むパスで壊れない
- [ ] バッチを変更した場合はPowerShell/cmdのどちらを前提にするか明確

## 7. 問題発見時のループ

1. スクリーンショット、ログ、コマンド出力から問題を特定する。
2. 最小限の修正を入れる。
3. 開発サーバーのHMRまたは再起動で反映する。
4. 同じ手順で再確認する。
5. OKになるまで繰り返す。

## 8. 完了報告

完了時は以下を残す:

- 確認した対象
- 実行したコマンド
- スクリーンショット確認の有無
- 未確認事項と理由
