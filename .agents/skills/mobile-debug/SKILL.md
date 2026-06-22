---
name: mobile-debug
description: Expo/React NativeモバイルアプリをAndroidエミュレーター + ADBでPC完結デバッグする。
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


# mobile-debug

Expo/React Native製モバイルアプリを、AndroidエミュレーターとADBでデバッグする。`packages/mobile` を採用しているプロジェクトで使う。

## ルール

- UI変更はエミュレーターのスクリーンショットを取得し、画像として確認する。
- タップ、スワイプ、入力が必要な変更はADB操作で実際に動かす。
- 実機やユーザー操作に依存しない。
- AVD名、SDKパス、デバイスID、アプリIDはプロジェクトの `memory-bank/techContext.md` と環境変数を優先する。

## 1. 変更範囲の確認

```bash
git diff --name-only HEAD
```

目安:

- `packages/mobile/src/**` → 画面・ロジック確認
- `packages/mobile/app.json` → Expo設定確認
- `packages/mobile/modules/**` → ネイティブモジュール確認
- `packages/shared/**` → mobileで使う範囲を確認
- `scripts/build_apk.*` → APKビルド手順確認

## 2. 依存と型チェック

```bash
pnpm install
pnpm --filter mobile typecheck
```

## 3. Android SDK / ADB確認

PowerShellで実行する例:

```powershell
$Sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA "Android\Sdk" }
$Adb = Join-Path $Sdk "platform-tools\adb.exe"
$Emulator = Join-Path $Sdk "emulator\emulator.exe"

& $Adb devices
& $Emulator -list-avds
```

`emulator-5554 device` のように `device` 状態の端末が表示されていれば起動済み。

## 4. エミュレーター起動

未起動の場合:

```powershell
$Avd = if ($env:ANDROID_AVD_NAME) { $env:ANDROID_AVD_NAME } else { (& $Emulator -list-avds | Select-Object -First 1) }
Start-Process -FilePath $Emulator -ArgumentList @("-avd", $Avd, "-no-audio", "-gpu", "swiftshader_indirect")
```

ヘッドレスでよい場合は `-no-window` を追加する。

ブート完了待ち:

```powershell
$Device = if ($env:ANDROID_SERIAL) { $env:ANDROID_SERIAL } else { "emulator-5554" }
for ($i = 0; $i -lt 24; $i++) {
    $boot = (& $Adb -s $Device shell getprop sys.boot_completed) -replace "`r", ""
    if ($boot -eq "1") { "READY"; break }
    Start-Sleep -Seconds 5
}
```

## 5. Expoアプリ起動

```bash
pnpm --filter mobile android
```

初回起動時にExpo Goや開発者メニューが出る場合がある。スクリーンショットで確認し、必要ならADBで閉じる。

## 6. スクリーンショット取得

```powershell
$Device = if ($env:ANDROID_SERIAL) { $env:ANDROID_SERIAL } else { "emulator-5554" }
$Shot = Join-Path $env:TEMP "mobile-debug.png"
& $Adb -s $Device exec-out screencap -p > $Shot
```

取得した `$Shot` を画像として開き、画面内容を確認する。

確認項目:

- [ ] 対象画面が表示されている
- [ ] ローディングや空画面のまま止まっていない
- [ ] 日本語が文字化けしていない
- [ ] レイアウトが端末幅で崩れていない
- [ ] エラー表示や赤画面が出ていない

## 7. ADB操作

```powershell
# タップ
& $Adb -s $Device shell input tap X Y

# スワイプ / スクロール
& $Adb -s $Device shell input swipe 540 1500 540 500 300

# ASCIIテキスト入力
& $Adb -s $Device shell input text "hello"

# Back / Home / Enter
& $Adb -s $Device shell input keyevent 4
& $Adb -s $Device shell input keyevent 3
& $Adb -s $Device shell input keyevent 66
```

日本語入力は `adb shell input text` だけでは扱いにくい。必要な場合はクリップボード経由やテスト用データ投入を検討する。

## 8. ログ確認

```powershell
& $Adb -s $Device logcat -d | Select-String -Pattern "react|expo|error|warn|fatal" -CaseSensitive:$false | Select-Object -Last 80
& $Adb -s $Device logcat -c
```

## 9. デバッグサイクル

1. スクリーンショットを取得して画面を確認する。
2. 必要ならADBでタップ・スワイプ・入力する。
3. ログでエラーを確認する。
4. 問題を修正する。
5. Expo HMRまたはアプリ再起動で反映を待つ。
6. 再度スクリーンショットを取得して確認する。

## 10. 終了時

Expoサーバーを停止する。エミュレーターは次回のため起動したままでもよい。

明示的に停止する場合:

```powershell
& $Adb -s $Device emu kill
```

## 制約

- エミュレーターは実機より遅い。パフォーマンス判断は慎重に行う。
- カメラ、GPS、通知など端末依存機能は追加手順が必要。
- スクリーンショットの表示サイズとADB座標は異なる場合があるため、実画像の解像度を基準に座標を決める。
