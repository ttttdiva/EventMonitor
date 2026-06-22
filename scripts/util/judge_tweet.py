import sys
import os
import asyncio
import logging
import yaml
import json
import argparse
from datetime import datetime

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from event_detector import EventDetector

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def judge_tweet(
    text: str,
    provider: str = None,
    model: str = None,
    effort: str = None,
    route_name: str = None,
):
    # Setup logging to show only important info
    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s'
    )
    
    config = load_config()
    
    if provider or model or effort or route_name:
        if not provider or not model:
            raise ValueError("--provider and --model must be specified together for a one-off route")
        route = {
            'name': route_name or f'manual-{provider}-{model}',
            'provider': provider,
            'model': model,
        }
        if effort:
            route['effort'] = effort
        config['llm_routes'] = [route]
    
    detector = EventDetector(config)
    
    tweet = {
        'id': 'manual_check_' + datetime.now().strftime('%Y%m%d_%H%M%S'),
        'text': text
    }
    
    print(f"--- 判定対象ツイート ---")
    print(text)
    print(f"------------------------\n")
    
    results = await detector.detect_event_tweets([tweet])
    
    if results:
        analysis = results[0].get('event_analysis', {})
        print(f"--- 判定結果 ---")
        print(json.dumps(analysis, indent=4, ensure_ascii=False))
        
        # event_info も表示
        event_info = results[0].get('event_info', {})
        if event_info:
            print(f"\n--- 抽出情報 ---")
            print(json.dumps(event_info, indent=4, ensure_ascii=False))
    else:
        # キーワードチェックで落ちた場合など
        print("--- 判定結果 ---")
        print("イベントに関連しないツイート、またはキーワードチェックにより除外されました。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='ツイートのイベント関連性を判定します。')
    parser.add_argument('text', type=str, help='判定するツイート本文')
    parser.add_argument('--provider', type=str, help='使用するLLMプロバイダー名 (例: codex_cli, gemini_cli, gemini_api)')
    parser.add_argument('--model', type=str, help='使用するLLMモデル名')
    parser.add_argument('--effort', type=str, help='Codex CLIのreasoning effort (例: low, medium, high, xhigh)')
    parser.add_argument('--route-name', type=str, help='一時ルート名')
    
    args = parser.parse_args()
    
    asyncio.run(judge_tweet(args.text, args.provider, args.model, args.effort, args.route_name))
