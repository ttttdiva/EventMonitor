import os
import logging
import asyncio
from typing import List, Dict, Any, Optional
import json
import re
import time
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta

import openai
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv


class EventDetector:
    CODEX_RATE_LIMIT_ERROR_MARKERS = (
        "rate limit",
        "rate_limit",
        "ratelimit",
        "429",
        "too many requests",
        "quota_exhausted",
        "quota exceeded",
        "usage limit",
    )

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("EventMonitor.EventDetector")
        self.enabled = config['event_detection'].get('enabled', True)
        self.keywords = config['event_detection']['keywords']
        self.exclude_keywords = config['event_detection']['exclude_keywords']
        self.openai_temperature = config['event_detection'].get('openai_temperature', 0.3)
        self.llm_providers = config['llm_providers']
        self.llm_routes = config['llm_routes']
        
        # LLMクライアントの初期化（有効な場合のみ）
        if self.enabled:
            self._initialize_llm_clients()
        else:
            self.logger.info("Event detection is disabled. Running as crawler only.")
        
        # Gemini用のレート制限
        self.gemini_last_request_time = {}
        self.gemini_request_count = {}
        self.gemini_quota_reset_time = datetime.now()
        self.gemini_cli_quota_until: Optional[datetime] = None
        self.gemini_cli_quota_last_log: Optional[datetime] = None
        
    def _initialize_llm_clients(self):
        """LLMクライアントを初期化"""
        # OpenAI
        openai_env = self.llm_providers.get('openai_api', {}).get('env_vars', {})
        openai_key = openai_env.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
        if openai_key:
            openai.api_key = openai_key
            self.openai_client = openai.OpenAI(api_key=openai_key)
        else:
            self.openai_client = None
            
        # Google Gemini
        gemini_env = self.llm_providers.get('gemini_api', {}).get('env_vars', {})
        google_key = gemini_env.get('GOOGLE_API_KEY') or os.getenv('GOOGLE_API_KEY')
        if google_key:
            self.gemini_client = genai.Client(api_key=google_key)
            self.gemini_models = {}
        else:
            self.gemini_client = None
            self.gemini_models = {}

    def _openai_model_supports_temperature(self, model_name: str) -> bool:
        """一部のGPT-5系モデルはtemperature指定を拒否するため判定"""
        lower_name = model_name.lower()
        # GPT-5ファミリー（mini/nanoなど）は温度を1固定で要求される
        if lower_name.startswith('gpt-5'):
            return False
        return True

    def _quick_keyword_check(self, tweet_text: str) -> tuple[bool, list]:
        """キーワードによる簡易チェック"""
        text_lower = tweet_text.lower()
        
        # 除外キーワードチェック
        for exclude_kw in self.exclude_keywords:
            if exclude_kw.lower() in text_lower:
                self.logger.debug(f"Tweet excluded due to keyword: {exclude_kw}")
                return False, []
        
        # 含むべきキーワードチェック
        matched_keywords = []
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                matched_keywords.append(keyword)
                
        return len(matched_keywords) > 0, matched_keywords
    
    def _check_gemini_rate_limit(self, model_name: str) -> bool:
        """Geminiのレート制限をチェック"""
        now = datetime.now()
        
        # クォータリセット時間の確認（1分ごと）
        if now - self.gemini_quota_reset_time >= timedelta(minutes=1):
            self.gemini_request_count = {}
            self.gemini_quota_reset_time = now
        
        # モデル別のリクエスト数を確認（Free Tierは15 requests/minute）
        request_count = self.gemini_request_count.get(model_name, 0)
        if request_count >= 15:
            wait_time = 60 - (now - self.gemini_quota_reset_time).total_seconds()
            if wait_time > 0:
                self.logger.warning(f"Gemini rate limit reached for {model_name}. Waiting {wait_time:.1f} seconds")
                return False
        
        return True
    
    def _update_gemini_request_count(self, model_name: str):
        """Geminiのリクエスト数を更新"""
        self.gemini_request_count[model_name] = self.gemini_request_count.get(model_name, 0) + 1

    def _gemini_cli_quota_active(self) -> bool:
        if not self.gemini_cli_quota_until:
            return False

        now = datetime.now()
        if now >= self.gemini_cli_quota_until:
            self.gemini_cli_quota_until = None
            self.gemini_cli_quota_last_log = None
            return False

        if (
            self.gemini_cli_quota_last_log is None
            or now - self.gemini_cli_quota_last_log >= timedelta(minutes=1)
        ):
            self.logger.warning(
                "Skipping gemini-cli until %s due to Gemini quota exhaustion",
                self.gemini_cli_quota_until.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self.gemini_cli_quota_last_log = now

        return True

    def _parse_gemini_cli_quota_delay(self, stderr_text: str) -> Optional[timedelta]:
        retry_match = re.search(r"retryDelayMs:\s*([0-9.]+)", stderr_text)
        if retry_match:
            try:
                return timedelta(milliseconds=float(retry_match.group(1)))
            except ValueError:
                pass

        reset_match = re.search(
            r"quota will reset after\s+([0-9hms\s.]+)",
            stderr_text,
            flags=re.IGNORECASE,
        )
        if not reset_match:
            return None

        seconds = 0.0
        for amount, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([hms])", reset_match.group(1)):
            value = float(amount)
            if unit == "h":
                seconds += value * 3600
            elif unit == "m":
                seconds += value * 60
            elif unit == "s":
                seconds += value

        if seconds <= 0:
            return None

        return timedelta(seconds=seconds)

    def _handle_gemini_cli_quota_error(self, stderr_text: str) -> bool:
        if "QUOTA_EXHAUSTED" not in stderr_text and "TerminalQuotaError" not in stderr_text:
            return False

        delay = self._parse_gemini_cli_quota_delay(stderr_text) or timedelta(hours=1)
        self.gemini_cli_quota_until = datetime.now() + delay
        self.gemini_cli_quota_last_log = datetime.now()
        self.logger.warning(
            "Gemini CLI quota exhausted; skipping gemini-cli until %s and using fallback models",
            self.gemini_cli_quota_until.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return True

    @classmethod
    def _is_codex_cli_rate_limit_error(cls, text: str) -> bool:
        normalized = text.lower()
        return any(marker in normalized for marker in cls.CODEX_RATE_LIMIT_ERROR_MARKERS)
    
    def _route_label(self, route: Dict[str, Any]) -> str:
        return route.get('name') or f"{route.get('provider')}:{route.get('model')}"

    def _provider_config(self, route: Dict[str, Any]) -> Dict[str, Any]:
        provider_name = route['provider']
        provider_config = self.llm_providers.get(provider_name)
        if not provider_config:
            raise ValueError(f"LLM provider not configured: {provider_name}")
        return provider_config

    def _route_timeout(self, route: Dict[str, Any], provider_config: Dict[str, Any], default: int) -> int:
        return int(route.get('timeout', provider_config.get('timeout', default)))

    def _route_env(self, route: Dict[str, Any], provider_config: Dict[str, Any]) -> Dict[str, str]:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env.update(provider_config.get('env_vars', {}))
        env.update(route.get('env_vars', {}))
        return env

    def _resolve_command(self, command: str) -> List[str]:
        cmd_args = shlex.split(command)
        resolved_cmd = shutil.which(cmd_args[0])
        if resolved_cmd:
            cmd_args[0] = resolved_cmd
        return cmd_args

    async def _analyze_with_gemini_cli(self, prompt: str, route: Dict[str, Any]) -> Optional[str]:
        """Gemini CLIを使用して分析を実行"""
        if self._gemini_cli_quota_active():
            return None

        provider_config = self._provider_config(route)
        command = provider_config.get('command', 'gemini')
        timeout = self._route_timeout(route, provider_config, 180)
        model = route.get('model')
        env = self._route_env(route, provider_config)
        
        try:
            cmd_args = self._resolve_command(command)
            if model:
                cmd_args.extend(['--model', model])
            cmd_args.extend(provider_config.get('args', ['-o', 'json']))
            cmd_args.extend(route.get('args', []))
            cmd_args.append('-')

            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode('utf-8')),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                # プロセスが終了するのを待つ
                await process.wait()
                self.logger.error(f"CLI execution timed out after {timeout} seconds")
                return None
            
            if process.returncode != 0:
                stderr_text = stderr.decode(errors='replace')
                if self._handle_gemini_cli_quota_error(stderr_text):
                    return None

                self.logger.error(f"CLI command failed with return code {process.returncode}")
                self.logger.error(f"Stderr: {stderr_text}")
                return None
            
            return stdout.decode().strip()
            
        except FileNotFoundError:
            self.logger.error(f"CLI command not found: {command}")
            return None
        except Exception as e:
            self.logger.error(f"CLI execution failed: {e}")
            return None

    async def _analyze_with_codex_cli(self, prompt: str, route: Dict[str, Any]) -> Optional[str]:
        """Codex CLIを使用して分析を実行"""
        provider_config = self._provider_config(route)
        command = provider_config.get('command', 'codex')
        timeout = self._route_timeout(route, provider_config, 180)
        codex_model = route['model']
        effort = route.get('effort') or route.get('reasoning_effort')
        extra_args = provider_config.get(
            'args',
            [
                'exec',
                '--skip-git-repo-check',
                '--ephemeral',
                '--ignore-rules',
                '--sandbox', 'read-only',
            ]
        )
        env = self._route_env(route, provider_config)
        output_path = None
        schema_path = None

        try:
            base_cmd_args = self._resolve_command(command)
            base_cmd_args.extend(extra_args)
            base_cmd_args.extend(route.get('args', []))

            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as output_file:
                output_path = output_file.name
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                delete=False,
                suffix='.schema.json',
            ) as schema_file:
                schema_path = schema_file.name
                json.dump(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "is_event_related": {"type": "boolean"},
                            "confidence": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                            },
                            "event_type": {"type": ["string", "null"]},
                            "event_date": {"type": ["string", "null"]},
                            "participation_type": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "is_event_related",
                            "confidence",
                            "event_type",
                            "event_date",
                            "participation_type",
                            "reason",
                        ],
                    },
                    schema_file,
                    ensure_ascii=False,
                )

            codex_prompt = (
                f"{prompt}\n\n"
                "追加指示:\n"
                "- 判定対象は上の「ツイート本文:」直下の文字列です。\n"
                "- ツイート本文が空や不明確な場合でも、質問や会話文を一切返さず、必ず規定のJSONフォーマット（is_event_related=false）で答えてください。\n"
                "- 判定理由などのテキストを含め、出力全体を一つのJSONオブジェクトのみにしてください。挨拶や説明は不要です。\n"
                "- ファイル編集、コマンド実行、検索は行わず、JSONオブジェクトだけを返してください。"
            )
            cmd_args = list(base_cmd_args)
            cmd_args.extend(['--model', codex_model])
            if effort:
                cmd_args.extend(['-c', f'model_reasoning_effort="{effort}"'])

            cmd_args.extend([
                '--output-schema',
                schema_path,
                '--output-last-message',
                output_path,
                '-',
            ])

            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(codex_prompt.encode('utf-8')),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self.logger.error(f"Codex CLI execution timed out after {timeout} seconds")
                return None

            stdout_text = stdout.decode(errors='replace')
            stderr_text = stderr.decode(errors='replace')
            if process.returncode != 0:
                combined_error = f"{stderr_text}\n{stdout_text}"
                if self._is_codex_cli_rate_limit_error(combined_error):
                    self.logger.warning(
                        "Codex CLI route %s hit a rate limit; trying next LLM route",
                        self._route_label(route),
                    )
                else:
                    self.logger.error(f"Codex CLI command failed with return code {process.returncode}")
                    self.logger.error(f"Stderr: {stderr_text}")
                return None

            if output_path and os.path.exists(output_path):
                with open(output_path, 'r', encoding='utf-8') as f:
                    result_text = f.read().strip()
                if result_text:
                    return result_text

            return stdout_text.strip()

        except FileNotFoundError:
            self.logger.error(f"Codex CLI command not found: {command}")
            return None
        except Exception as e:
            self.logger.error(f"Codex CLI execution failed with {self._route_label(route)}: {e}")
            return None
        finally:
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            if schema_path and os.path.exists(schema_path):
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

    async def _analyze_with_llm(self, tweet: Dict[str, Any], route: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """LLMを使ってツイートを分析"""
        provider = route['provider']
        model_name = route.get('model', '')
        route_label = self._route_label(route)
        prompt = f"""以下のツイートがイベント（コミケ、コミティア、例大祭、オンリーイベントなど）への参加告知や関連情報かどうか判定してください。

ツイート本文:
{tweet['text']}

判定基準（これらの要素が含まれる場合のみイベント関連と判定）:
1. イベントへの参加予告・告知（「参加します」「出展します」など未来形の表現）
2. スペース番号やブース情報の告知（例：東A-12a、西れ-01b）
3. 新刊・頒布物の告知（イベントでの頒布を明示している場合）
4. イベント当日の実況（設営完了、在庫情報、列形成など）
5. イベント関連の委託情報（特定のイベントへの委託）
6. 自分の作品を通販に出品した告知（「通販開始しました」「BOOTHに登録しました」など）

必ず除外する内容:
1. 他人の作品の購入報告（「買いました」「購入しました」「ポチった」など）
2. イベント終了後の感想・報告（「参加しました」「楽しかった」など過去形）
3. 商業作品（漫画、アニメ、ゲーム等）への単なる反応・感想
4. 「参加」という単語があっても、イベント以外への参加（企画、配信、祭りの感想など）
5. 他人の通販商品へのリンクや宣伝のRT
6. イベントと無関係な日常ツイート

重要な判定ポイント:
- 自分の作品の告知か、他人の作品への反応かを区別する
- 「通販開始」「販売開始」などは自分の作品なら検知対象
- 「購入しました」「買いました」は除外対象
- 時制に注意：未来のイベントへの参加表明を優先する
- 「参加」という単語だけで判定せず、文脈を正確に理解する

判定結果をJSON形式で返してください:
{{
    "is_event_related": true/false,
    "confidence": 0.0-1.0,
    "event_type": "イベントの種類（コミケ、コミティアなど）",
    "event_date": "推定されるイベント日付（わからない場合はnull）",
    "participation_type": "参加形態（サークル参加/一般参加/委託/不明）",
    "reason": "判定理由の簡潔な説明"
}}"""
        
        try:
            if provider == 'openai_api' and self.openai_client:
                # OpenAI API
                request_kwargs = {
                    'model': model_name,
                    'messages': [
                        {"role": "system", "content": "あなたはイベント参加情報を正確に判定するアシスタントです。"},
                        {"role": "user", "content": prompt}
                    ],
                    'response_format': {"type": "json_object"}
                }

                if self._openai_model_supports_temperature(model_name):
                    request_kwargs['temperature'] = self.openai_temperature
                else:
                    self.logger.debug(
                        f"Model {model_name} enforces default temperature; skipping custom value"
                    )

                response = await asyncio.to_thread(
                    self.openai_client.chat.completions.create,
                    **request_kwargs
                )
                result_text = response.choices[0].message.content
                
            elif provider == 'gemini_cli':
                # Gemini CLI
                result_text = await self._analyze_with_gemini_cli(prompt, route)
                if not result_text:
                    return None

                # gemini -o json の出力をパース
                try:
                    # 先頭の「Loaded cached credentials.」などのメタデータを除去して JSON 部分を抽出
                    json_match = re.search(r'(\{.*\})', result_text, re.DOTALL)
                    if json_match:
                        cli_json = json.loads(json_match.group(1))
                        if isinstance(cli_json, dict) and 'response' in cli_json:
                            result_text = cli_json['response']
                except Exception as e:
                    self.logger.debug(f"Failed to parse gemini-cli wrapper JSON: {e}")
                    # 失敗した場合は元のテキストで続行（後続の抽出ロジックに任せる）

            elif provider == 'codex_cli':
                # Codex CLI
                result_text = await self._analyze_with_codex_cli(prompt, route)
                if not result_text:
                    return None

            elif provider == 'gemini_api' and self.gemini_client is not None:
                # Gemini API

                # レート制限チェック
                if not self._check_gemini_rate_limit(model_name):
                    # レート制限に達している場合はスキップ
                    return None

                try:
                    response = await asyncio.to_thread(
                        self.gemini_client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            temperature=0.3,
                            response_mime_type="application/json"
                        )
                    )
                    result_text = response.text
                    
                    # リクエスト数を更新
                    self._update_gemini_request_count(model_name)
                    
                except Exception as e:
                    self.logger.error(f"Failed to create or use Gemini model {model_name}: {e}")
                    
                    # 429エラー（レート制限）の場合はリクエスト数を最大値に設定
                    if "429" in str(e) or "quota" in str(e).lower():
                        self.gemini_request_count[model_name] = 15
                    
                    return None
                
            else:
                self.logger.warning(f"LLM route {route_label} not available")
                return None
            
            # マークダウンのコードブロックを除去
            if "```" in result_text:
                # ```json ... ``` または ``` ... ``` の中身を抽出
                match = re.search(r'```(?:json)?\s*(.*?)```', result_text, re.DOTALL)
                if match:
                    result_text = match.group(1).strip()
            
            # JSONパース
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError:
                # 念のため、余分な文字が含まれている場合の再クリーニング（ { ... } を抽出）
                match = re.search(r'(\{.*\})', result_text, re.DOTALL)
                if match:
                    try:
                        result = json.loads(match.group(1))
                    except:
                        self.logger.error(f"Failed to parse JSON from {route_label}: {result_text[:100]}...")
                        return None
                else:
                    self.logger.error(f"Invalid JSON format from {route_label}: {result_text[:100]}...")
                    return None
            
            # リストが返された場合は最初の要素を取得
            if isinstance(result, list):
                if len(result) > 0:
                    result = result[0]
                else:
                    self.logger.error(f"Empty list returned from {route_label}")
                    return None
            
            # 必須フィールドの確認
            if not isinstance(result, dict):
                self.logger.error(f"Invalid response format from {route_label}: {type(result)}")
                return None
            
            return result
            
        except Exception as e:
            self.logger.error(f"LLM analysis failed with {route_label}: {e}")
            return None
    
    async def detect_event_tweets(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """イベント関連ツイートを検出"""
        # イベント検出が無効な場合は空リストを返す
        if not self.enabled:
            self.logger.info("Event detection is disabled. Returning empty list.")
            return []
        
        event_tweets = []
        
        for tweet in tweets:
            # まずキーワードチェック
            has_keywords, matched_keywords = self._quick_keyword_check(tweet['text'])
            if not has_keywords:
                continue
            
            # LLMで詳細分析（フォールバック付き）
            analysis_result = None
            for route in self.llm_routes:
                self.logger.debug(f"Analyzing tweet {tweet['id']} with {self._route_label(route)}")
                analysis_result = await self._analyze_with_llm(tweet, route)
                if analysis_result:
                    break
            
            if not analysis_result:
                self.logger.warning(f"All LLM models failed for tweet {tweet['id']}")
                # LLMが全て失敗した場合、キーワードマッチのみで判定
                analysis_result = {
                    'is_event_related': True,
                    'confidence': 0.5,
                    'reason': 'Keyword match only (LLM unavailable)'
                }
            
            # イベント関連と判定された場合
            if analysis_result.get('is_event_related', False) and analysis_result.get('confidence', 0) >= 0.6:
                # 分析結果をツイートデータに追加
                tweet['event_analysis'] = analysis_result
                
                # Hydrusタグ用の情報を追加
                event_info = {
                    'detected_keywords': matched_keywords,
                    'detected_events': [],
                    'event_type': analysis_result.get('event_type'),
                    'event_date': analysis_result.get('event_date'),
                    'participation_type': analysis_result.get('participation_type')
                }
                
                # イベント名を抽出
                if event_info['event_type']:
                    event_info['detected_events'].append(event_info['event_type'])
                
                # スペース番号やサークル名も抽出
                extracted_info = self.extract_event_info(tweet)
                event_info.update(extracted_info)
                
                tweet['event_info'] = event_info
                event_tweets.append(tweet)
                self.logger.info(f"Event tweet detected: {tweet['id']} - {analysis_result['reason']}")
        
        self.logger.info(f"Detected {len(event_tweets)} event-related tweets out of {len(tweets)} total")
        return event_tweets
    
    def extract_event_info(self, tweet: Dict[str, Any]) -> Dict[str, Any]:
        """ツイートからイベント情報を抽出"""
        text = tweet['text']
        
        info = {
            'space_number': None,
            'circle_name': None
        }
        
        # スペース番号の抽出（例: "東A-12a", "西れ-01b"）
        space_pattern = r'[東西南北][A-Zあ-ん\d]+-?\d+[ab]?'
        space_match = re.search(space_pattern, text)
        if space_match:
            info['space_number'] = space_match.group()
        
        # サークル名の抽出（「」内の文字列）
        circle_pattern = r'「([^」]+)」'
        circle_matches = re.findall(circle_pattern, text)
        if circle_matches:
            # 最初に見つかったものをサークル名とする
            info['circle_name'] = circle_matches[0]
        
        return info
