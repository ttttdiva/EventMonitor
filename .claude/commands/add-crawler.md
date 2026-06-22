# 新規クローラー追加ガイド

新しいプラットフォームのクローラーをEventMonitorに追加する際の完全チェックリスト。
ユーザーに「どのプラットフォームを追加するか」を確認してから、以下のステップを順に実施する。

---

## 必須確認事項（実装前）

1. **プラットフォーム名**: CSV `platform` 列に使う識別子（小文字英字）。既存: `pixiv`, `kemono`, `tinami`, `poipiku`, `fantia`, `nijie`, `skeb`, `misskey`, `gelbooru`, `fanbox`, `discord`
2. **認証方式**: Cookie（Fantia,Nijie,Skeb,Misskey,FANBOX等） / OAuth refresh-token（Pixiv） / APIキー（Gelbooru、任意） / 不要（Kemono）
3. **gallery-dl対応**: gallery-dlで取得可能か？ → 不可なら requests+BeautifulSoup カスタムスクレイパー
4. **投稿日時の取得可否**: APIやHTMLから実際の投稿日時を取得できるか？（後述のインポート順序に直結）

---

## Step 1: Extractor クラス作成

**ファイル**: `src/{platform}_extractor.py`

### 必須メソッド

```python
class {Platform}Extractor:
    def __init__(self, config: dict)
    def fetch_user_works(self, user_id: str, limit: int = None) -> List[Dict[str, Any]]
    def download_media_for_works(self, user_id: str, work_ids: List[str]) -> Dict[str, List[str]]
    def check_account_reachable(self, user_id: str) -> bool
    def clear_reachability_cache(self) -> None
```

### 推奨メソッド（任意）

```python
    def resolve_display_name(self, user_id: str) -> Optional[str]
```

※ 大半のクローラー（Pixiv, Fantia, Nijie, Skeb, Misskey, Gelbooru, FANBOX）で実装済み。
main.py の `_resolve_missing_display_names` で呼ばれるため、可能な限り実装すること。

### 必須属性

- `self.batch_size`: config から取得（デフォルト50）
- `self._account_reachable: Dict[str, bool]`: アカウント到達可能性キャッシュ

### fetch_user_works の返却dict仕様

各作品は以下のキーを持つdictで返す:

| キー | 型 | 必須 | 説明 |
|------|------|------|------|
| `id` | str | YES | 作品ID（DBの主キー） |
| `username` | str | YES | プラットフォーム上のユーザーID/ハンドル（`{platform}_user:` タグに使用。フィールド名はプラットフォームに合わせてよい: `handle`, `creator_id`, `fanclub_id`, `artist_id` 等） |
| `date` | str(ISO) | YES | 投稿日時（取得不可ならフォールバック、後述） |
| `url` | str | YES | 作品ページURL |
| `media` | List[str] | YES | メディアURL一覧（空リスト可） |
| `tags` | List[str] | NO | タグ一覧 |
| `sensitive` | bool | YES | R-18判定（後述） |
| `text` | str | NO | 作品タイトル/説明 |
| `display_name` | str | NO | クリエイター名（表示名。`creator:` タグに使用） |
| `source` | str | YES | プラットフォーム識別子 |
| `platform` | str | YES | プラットフォーム識別子 |

---

## Step 2: インポート順序の保証（最重要）

> **これは毎回クローラー追加時に発生する問題。必ず対応すること。**

Webサイトのページネーションは通常「新しい順」で返す。
Hydrusインポート時はファイルのインポート日時がそのまま時系列になるため、
**古い順（時系列順）でインポートする必要がある。**

### 判定フロー

```
投稿日時をAPIまたはHTMLから取得できるか？
├── YES → account_processor で sorted(new_works, key=lambda x: x.get('date', ''))
│         ※IDも副キーに追加推奨: key=lambda x: (x.get('date', ''), int(x.get('id', 0)))
└── NO  → 投稿IDが数値連番か？
    ├── YES → sorted(new_works, key=lambda x: int(x.get('id', 0)))
    │         ※dateフィールドはdatetime.now()フォールバックでソート不能のため
    └── NO  → _collect_post_ids の結果を reverse() してから処理
              （ページネーションが新→古なら、逆転で古→新になる）
```

### account_processor のソート実装

monitor と log-only の **両方** に以下のソート処理を入れること:

```python
# ◆ 実際の投稿日時が取得できる場合
#   対象: Pixiv, Kemono, Fantia, Nijie, Misskey, Gelbooru, FANBOX
new_works = sorted(new_works, key=lambda x: (x.get('date', ''), int(x.get('id', 0))))

# ◆ 投稿日時が取得できない場合（IDでソート）
#   対象: TINAMI, Poipiku, Skeb
new_works = sorted(new_works, key=lambda x: int(x.get('id', 0)))
```

**コメントに「なぜその方法でソートするか」を必ず書く。**

---

## Step 3: センシティブコンテンツ判定（3層パターン）

全クローラー共通の必須仕様:

### Layer 1: Extractor

プラットフォーム固有の方法で `sensitive: bool` を導出する。

| プラットフォーム | 判定方法 |
|---|---|
| Twitter | `sensitive` + `sensitive_flags` |
| Pixiv | `x_restrict >= 1` |
| Kemono | 常に `False`（Kemonoにはセンシティブフラグなし） |
| TINAMI | 年齢制限ゲート検出（'18歳以上','年齢確認','R-18'） |
| Poipiku | 年齢確認ダイアログ/カテゴリ検出 |
| Fantia | `rating == "adult"` |
| Nijie | タグに "R-18" / "R-18G" を含む |
| Skeb | `nsfw == True` |
| Misskey | `cw`（content warning）が非None/非空 |
| Gelbooru | `rating` が `questionable` または `explicit` |
| FANBOX | `hasAdultContent == True` |
| **新規** | プラットフォーム固有の方法を調査・実装 |

### Layer 2: Database

- テーブルに `sensitive = Column(Boolean, default=False)` を追加
- save メソッドで `sensitive=bool(data.get('sensitive', False))`

### Layer 3: Hydrus タグ

- `sensitive=True` のとき `rating:r-18` タグを付与
- プラットフォーム固有のタグ（例: Pixivの "R-18"）もそのまま残す

---

## Step 4: Database モデル追加

**ファイル**: `src/database.py`

### 4a. テーブルモデル定義

`{Platform}Work` と `{Platform}LogOnlyWork` の2クラスを追加:

```python
class {Platform}Work(Base):
    __tablename__ = '{platform}_works'
    id = Column(String(100), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    display_name = Column(String(200))
    title = Column(Text)
    work_date = Column(DateTime, nullable=False, index=True)
    work_url = Column(String(500), nullable=False)
    tags = Column(Text)              # JSON array
    sensitive = Column(Boolean, default=False)
    media_urls = Column(Text)        # JSON array
    local_media = Column(Text)       # JSON array
    huggingface_urls = Column(Text)  # JSON array
    created_at = Column(DateTime, default=datetime.now)
    hydrus_expected_count = Column(Integer, default=0)
    hydrus_imported_count = Column(Integer, default=0)
```

LogOnlyWork は `hydrus_*` 列不要、代わりに `uploaded_to_hf = Column(Boolean, default=False)` を持つ。

### 4b. _ensure_hydrus_columns()

テーブル名リストに `'{platform}_works'` と `'{platform}_log_only_works'` を追加。

### 4c. 必須メソッド追加

- `filter_new_{platform}_works(works, user_id)`
- `filter_{platform}_log_only_works(works, user_id)`
- `save_{platform}_works(works, user_id)` → `int` (保存件数)
- `save_single_{platform}_log_only_work(work, user_id)` → `bool`
- `update_{platform}_hydrus_import_status(work_id, imported_count, expected_count)`

---

## Step 5: Hydrus 連携

**ファイル**: `src/hydrus_client.py`

### 5a. インポートメソッド

```python
async def import_{platform}_images(self, work_data, media_file_paths) -> List[Tuple[str, str]]:
```

処理フロー: `import_file()` → `associate_url()` → `_generate_{platform}_tags()` → `add_tags()` → `add_note()`

### 5b. タグ生成メソッド

```python
def _generate_{platform}_tags(self, work_data) -> List[str]:
```

必須タグ:
- `source:{platform}`（base_tags の `source:twitter` を置換）
- `{platform}_id:{work_id}`（作品ID）
- `creator:{display_name}`（creator_tag_format 準拠、表示名）
- `{platform}_user:{username}`（プラットフォーム上のユーザーID。`creator:` とは分離。フィールド名はプラットフォームに合わせる: `handle`, `creator_id`, `fanclub_id`, `artist_id` 等）
- `rating:r-18`（sensitive=True時）
- `rank:{N}`（work_data['rank']、デフォルト3）
- 作品固有タグ（work_data['tags']）
- カスタムタグ（work_data['custom_tags']）

既存クローラーのタグ一覧（参考）:
| プラットフォーム | IDタグ | ユーザータグ | ユーザーデータソース |
|---|---|---|---|
| Twitter | `tweet_id:{id}` | `twitter_user:{username}` | `work_data['username']` |
| Pixiv | `pixiv_id:{id}` | `pixiv_user:{username}` | `work_data['username']` |
| Skeb | `skeb_id:{id}` | `skeb_user:{username}` | `work_data['username']` |
| Kemono | `kemono_id:{id}` | `kemono_user:{username}` | `work_data['username']` (service/user_id形式) |
| TINAMI | `tinami_id:{id}` | `tinami_user:{username}` | `work_data['username']` |
| Poipiku | `poipiku_id:{id}` | `poipiku_user:{username}` | `work_data['username']` |
| Privatter | `privatter_id:{id}` | `privatter_user:{username}` | `work_data['username']` |
| Fantia | `fantia_id:{id}` | `fantia_user:{fanclub_id}` | `work_data['fanclub_id']` |
| Nijie | `nijie_id:{id}` | `nijie_user:{artist_id}` | `work_data['artist_id']` |
| FANBOX | `fanbox_id:{id}` | `fanbox_user:{creator_id}` | `work_data['creator_id']` |
| Bluesky | `bluesky_id:{id}` | `bluesky_user:{handle}` | `work_data['handle']` |
| Misskey | `misskey_id:{id}` | `misskey_user:{username}` | `work_data['username']` |

### 5c. expected_count ホワイトリスト

`estimate_hydrus_expected_count()` 内のファイル拡張子ホワイトリストを確認し、
新プラットフォーム固有のメディア形式があれば追加。

---

## Step 6: Account Processor 統合

**ファイル**: `src/services/account_processor.py`

### 6a. コンストラクタ

`{platform}_extractor=None` パラメータを追加し、`self.{platform}_extractor` に格納。

### 6b. process_account() ルーティング

```python
elif platform == "{platform}":
    if account.get("account_type") == "log":
        await self._process_{platform}_log_only_account(account)
    else:
        await self._process_{platform}_monitor_account(account)
```

**注意**: discord の elif より前に追加すること（discord は最後のプラットフォーム分岐）。

### 6c. 処理メソッド

- `_process_{platform}_monitor_account(account)`: メタデータ取得 → フィルタ → ソート → バッチDL → DB保存 → Hydrusインポート
- `_process_{platform}_log_only_account(account)`: メタデータ取得 → フィルタ → ソート → バッチDL → DB保存

各処理メソッド内の work ループで以下を注入すること:
```python
work["custom_tags"] = account.get("custom_tags", [])
work["rank"] = account.get("rank", 3)
```

### 6d. 補助メソッド

- `_record_unreachable_{platform}(user_id, account)`: account_status_tracker への登録
- `_check_reachability()` に elif 追加

---

## Step 7: main.py 統合

**ファイル**: `main.py`

### 7a. Import

```python
from src.{platform}_extractor import {Platform}Extractor
```

### 7b. 初期化（__init__ 内）

```python
self.{platform}_extractor = {Platform}Extractor(self.config) if self.config.get('{platform}', {}).get('enabled', False) else None
```

### 7c. AccountProcessor への受け渡し

コンストラクタ引数に `{platform}_extractor=self.{platform}_extractor` を追加。

### 7d. clear_reachability_cache

```python
if self.{platform}_extractor:
    self.{platform}_extractor.clear_reachability_cache()
```

### 7e. _resolve_missing_display_names（任意）

CSV の display_name が空のとき、extractor の `resolve_display_name()` で補完する処理を追加。

---

## Step 8: 設定ファイル

### config.yaml

```yaml
{platform}:
  enabled: true
  max_batch_size: 50
  # プラットフォーム固有の設定
```

### .env（認証が必要な場合）

```
{PLATFORM}_API_KEY=xxx
{PLATFORM}_COOKIE=xxx
```

---

## Step 9: Discord Ingest 対応

**ファイル**: `src/services/discord_account_ingest.py`

DiscordチャンネルにURLを投稿→自動で `monitored_accounts.csv` に追記する仕組み。
新プラットフォームのURLを認識できるよう、以下を追加する。

### 9a. URL抽出メソッド追加

```python
def _extract_{platform}_username(self, parsed) -> Optional[Tuple[str, str]]:
    """https://{platform-domain}/path/to/{user_id} → ("{user_id}", "{platform}")"""
    path = parsed.path.strip("/")
    if not path:
        return None

    parts = path.split("/")
    # プラットフォーム固有のURLパターンに合わせて実装
    # 例: /creator/profile/{user_id}, /users/{user_id} など
    ...
    return (user_id, "{platform}")
```

### 9b. `_extract_username_from_url()` にルーティング追加

```python
# --- {Platform} ---
if netloc == "{platform-domain}":
    return self._extract_{platform}_username(parsed)
```

**注意**: `return None` の前に追加すること。

### 9c. 確認事項

- URLパターンのバリエーション: www有無、パス形式の違い（作品ページ vs プロフィールページ）
- 既存プラットフォームの実装を参考にする（Pixiv: `/users/{id}`, Kemono: `/{service}/user/{id}` 等）

---

## Step 10: テスト・検証

### 必須テスト項目

1. `fetch_user_works` が正しいメタデータを返すか
2. **インポート順序が古い順になっているか**（Hydrus側で確認）
3. `sensitive` 判定が正しく動作するか
4. DB保存・重複フィルタが正しく動作するか
5. 既存プラットフォームに影響がないか（回帰テスト）
6. **Discord Ingest**: 新プラットフォームのURLをDiscordに投稿して正しくCSV追記されるか

### クイックテスト

```bash
python run_tests.py --quick
```

---

## 完了チェックリスト

- [ ] `src/{platform}_extractor.py` 作成
- [ ] インポート順序: 古い順ソートが正しく実装されている（dateまたはID連番）
- [ ] センシティブ判定: 3層パターン（Extractor→DB→Hydrusタグ）実装済み
- [ ] `src/database.py`: Work + LogOnlyWork モデル追加
- [ ] `src/database.py`: `_ensure_hydrus_columns()` にテーブル追加
- [ ] `src/database.py`: filter / save / update メソッド追加
- [ ] `src/hydrus_client.py`: import + tag生成メソッド追加（`{platform}_id:` + `{platform}_user:` タグ含む）
- [ ] `src/services/account_processor.py`: ルーティング + 処理メソッド追加
- [ ] `main.py`: import + 初期化 + 受け渡し
- [ ] `config.yaml`: プラットフォームセクション追加
- [ ] `src/services/discord_account_ingest.py`: URL抽出メソッド + ルーティング追加
- [ ] テスト通過（Discord Ingest含む）
- [ ] MEMORY.md にパターン情報を追記
