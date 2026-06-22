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

CLAUDE.md を参照してください。
