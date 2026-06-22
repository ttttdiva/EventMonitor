---
name: desktop-debug
description: TauriデスクトップアプリをVite表示、Tauri dev、ビルドで確認する。
---

# desktop-debug

Tauriデスクトップアプリのデバッグ手順。`packages/desktop` を採用しているプロジェクトで使う。

## ルール

- UI変更はViteのブラウザ表示で素早く確認し、スクリーンショットを読む。
- Tauri API、Rust、ファイル操作、ウィンドウ挙動に関わる変更は `tauri:dev` でも確認する。
- リリース成果物の公開やアップロードはしない。
- テンプレート初期化後は、プロジェクト固有のアプリ名、出力exe名、ビルド手順を優先する。

## 1. 変更範囲の確認

```bash
git diff --name-only HEAD
```

目安:

- `packages/desktop/src/**` → フロントエンドUI確認
- `packages/desktop/src-tauri/**` → Tauri/Rust確認
- `packages/shared/**` → desktopから利用される範囲を確認
- `build.bat`, `scripts/build_desktop.sh` → ビルドと成果物コピー確認

## 2. 依存と型チェック

```bash
pnpm install
pnpm --filter desktop typecheck
```

## 3. ViteでUI確認

```bash
pnpm --filter desktop dev
```

通常は `http://127.0.0.1:5173` をブラウザで開く。表示URLが異なる場合はViteのログを使う。

確認項目:

- [ ] 変更した画面が表示される
- [ ] 日本語が文字化けしていない
- [ ] レイアウトが崩れていない
- [ ] ボタン、入力、一覧、ダイアログが操作できる
- [ ] コンソールエラーがない

操作が必要な変更では、クリック・入力・保存後の状態をスクリーンショットで確認する。

## 4. Tauri Native確認

以下に該当する場合はTauriとして起動する:

- `src-tauri/**` を変更した
- `@tauri-apps/api` の呼び出しを変更した
- ファイル、ダイアログ、OS連携、ウィンドウ操作を変更した
- Viteでは再現できないデスクトップ固有の挙動を扱う

```bash
pnpm --filter desktop tauri:dev
```

確認項目:

- [ ] ネイティブウィンドウが起動する
- [ ] Tauri command / invoke が成功する
- [ ] ファイルパスや権限エラーが出ていない
- [ ] 開発者コンソールにエラーがない

## 5. ビルド確認

通常のフロントエンドビルド:

```bash
pnpm --filter desktop build
```

Tauri/Rustやビルド設定を変更した場合:

```bash
pnpm --filter desktop tauri:build -- --no-bundle
```

`build.bat` や `scripts/build_desktop.sh` を変更した場合は、スクリプトを実行して成果物の場所と名前を確認する。

## 6. 問題発見時

1. スクリーンショット、コンソール、Tauriログから原因を切り分ける。
2. UI問題なら `packages/desktop/src/**`、ネイティブ問題なら `packages/desktop/src-tauri/**` を修正する。
3. HMRまたは `tauri:dev` 再起動で反映する。
4. 同じ手順で再確認する。

## 7. 完了報告

以下を報告する:

- Vite表示確認の結果
- Tauri native確認の有無
- 実行したビルドコマンド
- 未確認事項と理由
