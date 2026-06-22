# projectbrief.md

## プロジェクト概要
EventMonitorは、Twitter/Xの指定アカウントのツイートを定期取得し、イベント参加告知などをAIで検出してDiscord通知・Hydrus連携・バックアップを行う自動監視システム。

## 目的とスコープ
- イベント参加告知の見逃し防止（コミケ・コミティア等の同人イベント）
- 画像/動画の取得と保全（ローカル保持 + HuggingFaceバックアップ）
- イベント判定と通知の自動化（LLM: GPT-5/Gemini）
- Hydrusへのメディアインポート（タグ付き自動管理）
- Discordステータスダッシュボード + AoiTalk連携

## 成果物/ゴール
- 安定した定期監視（デーモン運用 or 単発実行）
- イベント検出とDiscord通知が継続稼働
- メディアとDBの一貫性保持（バックアップ含む）
- 1800件超のアカウント監視運用（Twitter/Pixiv/Kemono/FANBOX/Fantia/Nijie/Skeb/Misskey/Gelbooru/TINAMI/Poipiku/Bluesky/Privatter/bilibili対応）

## 非ゴール
- UIフロントエンドの提供
- ブラウザ拡張機能

## 参照
- README.md（概要/セットアップ/主要機能）
- docs/*（設計/フロー/テスト/運用ルール）
- docs/dev_guidelines.md（開発ルール）
