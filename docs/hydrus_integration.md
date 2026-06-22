# Hydrus Client連携ガイド

EventMonitorで検出したイベント関連ツイートの画像を、自動的にHydrus Clientにインポートする機能です。

## 設定方法

### 1. Hydrus Client側の設定

1. Hydrus Clientを起動
2. メニューから `services > manage services` を開く
3. `local` タブの `client api` を選択
4. APIポート（デフォルト: 45869）を確認
5. `add` をクリックして新しいアクセス許可を作成
   - 名前: `EventMonitor` など
   - 必要な権限:
     - ✓ import files
     - ✓ edit file tags
     - ✓ search for and fetch files（Creator Searchスクリプト使用時は必須）
     - ✓ manage pages（Creator Searchスクリプト使用時は必須）
6. 生成された64文字のアクセスキーをコピー
7. （推奨）`services > manage services > local > local tag services` でソースごとのタグサービスを追加
   - 例: `twitter tags`、`pixiv tags`
   - これにより各ソース由来のタグが分離され、重複統合後もタグの出所を保持できる

### 2. EventMonitor側の設定

`config.yaml` を編集:

```yaml
# Hydrus Client連携設定
hydrus:
  # 連携を有効にするか
  enabled: true  # falseからtrueに変更

  # Hydrus Client APIのURL
  api_url: "http://127.0.0.1:45869"  # 必要に応じて変更

  # APIアクセスキー（Client APIで発行したキー）
  access_key: "ここに64文字のアクセスキーを貼り付け"

  # プラットフォーム別タグサービス振り分け（推奨）
  # Hydrus側で事前にローカルタグサービスを作成すること
  # サービス名はHydrus GUIの表示名と完全一致させる
  tag_services:
    twitter: "twitter tags"
    pixiv: "pixiv tags"
```

> **注**: `tag_services`を設定しない場合、従来通りすべてのタグが"local tags"サービスに送信されます。

### 3. 動作確認

設定が完了したら、既存のツイートデータを使って疎通確認を行います。乾式モードでHydrusへ接続できるかを確認するには、以下のスクリプトを利用してください。

```bash
python scripts/hydrus/reimport.py --limit 1
```

大量データを送る前に小さな `--limit` 値でテストすることを推奨します。

## 自動インポートの仕組み

### インポート対象
- イベント関連と判定されたツイートの画像のみ
- 既にHydrusに存在する画像はスキップ（SHA256ハッシュで判定）

### 自動付与されるタグ

#### 基本タグ
- `source:twitter` - ソース
- `tweet_id:1234567890123456789` - 数値のみのツイートID
- `imported_by:eventmonitor` - インポート元

#### アーティスト情報
- `creator:[表示名]` - ツイートしたアーティスト名（display_name）
- `creator:[ユーザー名]` - ツイートしたアーティスト名（username、display_nameと異なる場合）

#### コンテンツ情報
- `title:[ツイート本文]` - ツイートの本文内容

#### イベント情報
- `event:[イベント名]` - 検出されたイベント名（コミケC103など）
- `date:[YYYY-MM-DD]` - ツイート日付（デフォルトでは無効）

#### 詳細情報
- ツイートURLは「known URLs」として関連付け（タグではなくURLメタデータとして保存）
- `keyword:[キーワード]` - 検出されたキーワード（参加、ブースなど）

### タグ例

実際にインポートされる際のタグ例:

```
source:twitter
imported_by:eventmonitor
creator:テストアーティスト
creator:test_artist
title:【C103 2日目参加】東ホール A-123aでお待ちしております！新刊は〇〇本です。
event:コミケC103
keyword:参加
keyword:ブース
keyword:新刊
```

注: ツイートURLは「known URLs」として関連付けられます。
`tag_services`が設定されている場合、Twitterのタグは`twitter tags`サービスに、Pixivのタグは`pixiv tags`サービスにそれぞれ送信されます。

## カスタマイズ

### インポート設定

```yaml
import_settings:
  # イベント関連ツイートのみインポートするか
  event_tweets_only: true
  
  # 既存ファイルのスキップ（SHA256ハッシュで判定）
  skip_existing: true
```

### タグ設定

```yaml
tag_settings:
  # 基本タグ（必ず付与）
  base_tags:
    - "source:twitter"
    - "imported_by:eventmonitor"
    - "your_custom_tag"  # カスタムタグを追加可能

  # ツイートIDタグ
  include_tweet_id_tag: true
  tweet_id_tag_format: "tweet_id:{tweet_id}"
  
  # タグフォーマット（{name}、{date}などが置換される）
  creator_tag_format: "creator:{name}"
  event_tag_format: "event:{name}"
  date_tag_format: "date:{date}"
  
  # オプション
  include_tweet_url: true  # ツイートURLをknown URLとして関連付け
  include_title_tag: true  # ツイート本文をtitleタグとして追加
  include_date_tag: false  # 日付タグを追加（デフォルトは無効）
  include_detected_keywords: true  # 検出キーワードをタグとして追加
```

## トラブルシューティング

### "API接続に失敗しました"
- Hydrus Clientが起動していることを確認
- APIポート番号が正しいか確認（デフォルト: 45869）
- ファイアウォールでポートがブロックされていないか確認

### "アクセスキーが無効です"
- アクセスキーが正しくコピーされているか確認（64文字）
- Hydrus Client側で権限が正しく設定されているか確認

### "画像のインポートに失敗しました"
- Hydrusのインポートフォルダに書き込み権限があるか確認
- 画像ファイルが破損していないか確認

## Hydrus復旧手順（local_media欠落・再インポート）

Hydrus連携が停止したり、`local_media` が空/絶対パスになってしまった場合は以下の手順で復旧する。

1. **local_mediaの再構築/正規化**
   ```bash
   python scripts/fix/missing_local_media.py --username foo bar
   ```
   - `.env` の `HYDRUS_ACCESS_KEY` を自動で読み込む（スクリプト先頭で `load_dotenv()` 済み）。
   - 欠落があれば gallery-dl を使って再ダウンロードし、`images/...` 形式で保存。
   - 絶対パスで保存された行も相対パスに正規化される。
   - `--limit N` で処理件数を制限できる。Hydrus連携を無効にしたい場合は一時的に `hydrus.enabled=false` にする。
   - 既に完了済み（expected/imported一致）のレコードはデフォルトでスキップされる。全件再処理したい場合は `--force-all` を使う。
   - ユーザーごとに最新N件だけ処理したい場合は `--per-user-limit N` を使う。

2. **Hydrusへの再インポート**
   ```bash
   python scripts/hydrus/reimport.py --username foo bar --limit 200
   ```
   - `--include-log` を付けると log アカウントも再インポート対象に含められる。
   - 途中で止めた場合は自動で進捗ファイル(`logs/reimport_progress.json`)が更新され、次回再開できる。`--reset` で進捗消去。

3. **動作確認**
   - Hydrus クライアントで `tweet_id:...` や `imported_by:eventmonitor` を検索し、最新 ID が入っているか確認。
   - `logs/app.log` / `logs/fix_missing_local_media_*.log` / `logs/reimport_to_hydrus.log` を確認し、`Imported ... images` が出ているかチェック。

## 運用上の注意

1. **ストレージ容量**: 大量の画像をインポートする場合は、Hydrusのストレージ容量に注意
2. **重複チェック**: SHA256ハッシュで重複をチェックするため、同じ画像は二度インポートされません
3. **タグの管理**: 自動生成されるタグが多くなる場合があるので、定期的な整理を推奨
4. **復旧スクリプトの実行順**: local_media 修復 → Hydrus 再インポート → 動作確認の順に実行すると安全。全件ではなく最小限の件数・アカウントに絞って実行する。

## Creator Search スクリプト

選択中の画像から`creator:`タグを取得し、そのクリエイターの全作品を新規ページで開くユーティリティです。

### 必要な権限

通常のEventMonitor運用に加えて、以下のAPI権限が**必須**です:
- **Search for and Fetch Files** (permission 3)
- **Manage Pages** (permission 4)

既存のアクセスキーにこれらの権限がない場合は、Hydrus Clientの `services > manage services > client api` から権限を追加してください。

### 使い方

1. Hydrus Clientでサムネイルを選択
2. 以下のいずれかで実行:

```bash
# コマンドラインから直接実行
python scripts/hydrus/open_creator_page.py

# AHKホットキー（F8）を使用
# scripts/hydrus/open_creator_page.ahk をダブルクリックで常駐
```

### AHKホットキー設定

`scripts/hydrus/open_creator_page.ahk` をダブルクリックするとタスクトレイに常駐します。

- **デフォルトキー**: F8（Hydrusウィンドウがアクティブ時のみ）
- **キー変更**: AHKファイル内の `F8::` を任意のキーに変更
- **ウィンドウクラス名**: Window Spyで確認し、`HYDRUS_CLASS` 変数を更新（ファイル内にコメントで手順記載）
