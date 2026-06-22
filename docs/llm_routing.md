# LLM Routing

EventMonitor のイベント判定は `config.yaml` の `llm_providers` と `llm_routes` で制御する。

## 設定の分担

- `llm_providers`: CLI コマンド、共通引数、環境変数、タイムアウトなど、プロバイダーの実行方法を定義する。
- `llm_routes`: 実際に試す順序を定義する。各 route は `provider`、`model`、必要なら `effort` を持つ。
- `llm_routes` は上から順に試される。CLI の quota、rate limit、timeout、JSON不正などで失敗した場合は次の route に進む。

## 現行の既定順

```yaml
llm_routes:
  - name: "codex-spark-low"
    provider: "codex_cli"
    model: "gpt-5.3-codex-spark"
    effort: "low"
  - name: "gemini-cli-3-flash"
    provider: "gemini_cli"
    model: "gemini-3-flash-preview"
  - name: "codex-5.5-low"
    provider: "codex_cli"
    model: "gpt-5.5"
    effort: "low"
  - name: "gemini-api-3-flash"
    provider: "gemini_api"
    model: "gemini-3-flash-preview"
```

## Provider

### `codex_cli`

Codex CLI を `codex exec` で呼び出す。route の `model` は `--model` に渡され、route の `effort` は `-c model_reasoning_effort="..."` に渡される。

```yaml
llm_providers:
  codex_cli:
    command: "codex"
    args:
      - "exec"
      - "--skip-git-repo-check"
      - "--ephemeral"
      - "--ignore-rules"
      - "--sandbox"
      - "read-only"
    env_vars: {}
    timeout: 180
```

### `gemini_cli`

Gemini CLI を stdin 入力で呼び出す。route の `model` は `--model` に渡される。

```yaml
llm_providers:
  gemini_cli:
    command: "gemini"
    args:
      - "-o"
      - "json"
    env_vars: {}
    timeout: 180
```

### `gemini_api`

Google Gemini API を Python SDK で呼び出す。`GOOGLE_API_KEY` が必要。

```yaml
llm_providers:
  gemini_api:
    env_vars: {}
    timeout: 180
```

### `openai_api`

OpenAI API を Python SDK で呼び出す。`OPENAI_API_KEY` が必要。現行の既定順には入れていない。

```yaml
llm_providers:
  openai_api:
    env_vars: {}
    timeout: 180
```

## Codex CLI モデルと effort

Codex CLI の利用可能モデルは環境の Codex バージョンとアカウント状態で変わる。確認は以下で行う。

```powershell
codex debug models
```

2026-05-16 時点のこの環境で `visibility=list` として確認できた Codex モデル:

| model | default effort | supported efforts |
| --- | --- | --- |
| `gpt-5.5` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.4` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.4-mini` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.3-codex` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.2` | `medium` | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.3-codex-spark` | `high` | `low`, `medium`, `high`, `xhigh` |

## 診断

任意の route で手動判定する場合:

```powershell
venv\Scripts\python.exe -X utf8 scripts\util\judge_tweet.py "コミケに参加します。スペースは東A-12aです。" --provider codex_cli --model gpt-5.5 --effort low
```

`--provider` / `--model` を指定しない場合は `config.yaml` の `llm_routes` をそのまま使う。
