import sys
import os
import asyncio
import logging
import yaml
from datetime import datetime

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from event_detector import EventDetector

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def run_live_test():
    print("Loading actual config.yaml...")
    config = load_config()
    
    config['llm_routes'] = [
        {
            'name': 'live-gemini-cli',
            'provider': 'gemini_cli',
            'model': 'gemini-3-flash-preview',
        }
    ]
    
    print(f"Configured CLI command: {config.get('llm_providers', {}).get('gemini_cli', {}).get('command')}")
    print(f"Configured CLI model: {config['llm_routes'][0]['model']}")
    print(f"Configured Timeout: {config.get('llm_providers', {}).get('gemini_cli', {}).get('timeout')}")
    
    detector = EventDetector(config)
    
    # Sample event tweet
    tweet = {
        'id': 'live_test_tweet_1',
        'text': '【告知】\nC103 土曜日 東A-01a\n「Antigravity」\n新刊『Agentic Coding』を頒布します！\nまた、既刊の『Automated Testing』も持ち込みます。\n是非お越しください！ #C103 #コミケ'
    }
    
    print(f"\nAnalyzing tweet: {tweet['text'][:30]}...")
    
    start_time = datetime.now()
    results = await detector.detect_event_tweets([tweet])
    end_time = datetime.now()
    
    print(f"\nAnalysis complete in {(end_time - start_time).total_seconds():.2f}s")
    
    if results:
        print("\n[SUCCESS] Event detected!")
        analysis = results[0].get('event_analysis', {})
        print(f"Confidence: {analysis.get('confidence')}")
        print(f"Event Type: {analysis.get('event_type')}")
        print(f"Event Date: {analysis.get('event_date')}")
        print(f"Reason: {analysis.get('reason')}")
    else:
        print("\n[FAILURE] No event detected or CLI failed.")

if __name__ == "__main__":
    asyncio.run(run_live_test())
