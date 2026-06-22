import sys
import os
import asyncio
import logging

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
from event_detector import EventDetector

async def test_markdown_stripping():
    logging.basicConfig(level=logging.DEBUG)
    
    config = {
        'event_detection': {
            'enabled': True,
            'keywords': ['test'],
            'exclude_keywords': [],
            'openai_temperature': 0.3
        },
        'llm_providers': {
            'gemini_cli': {
                'command': 'python tests/mock_markdown_cli.py',
                'args': ['-o', 'json'],
                'timeout': 5,
                'env_vars': {}
            }
        },
        'llm_routes': [
            {
                'name': 'mock-markdown',
                'provider': 'gemini_cli',
                'model': 'mock-model'
            }
        ]
    }
    
    detector = EventDetector(config)
    tweet = {'id': '1', 'text': 'test'}
    
    results = await detector.detect_event_tweets([tweet])
    
    if results and results[0]['event_analysis']['reason'] == "Markdown test":
        print("SUCCESS: Markdown code block was stripped and JSON parsed.")
    else:
        print("FAILURE: Markdown stripping failed.")

if __name__ == "__main__":
    asyncio.run(test_markdown_stripping())
