import sys
import os
import asyncio
import logging

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from event_detector import EventDetector

async def test_cli_integration():
    # Setup logging
    logging.basicConfig(level=logging.DEBUG)
    
    # Mock config
    config = {
        'event_detection': {
            'enabled': True,
            'keywords': ['test'],
            'exclude_keywords': [],
            'openai_temperature': 0.3
        },
        'llm_providers': {
            'gemini_cli': {
                'command': 'python tests/mock_gemini_cli.py',
                'args': ['-o', 'json'],
                'timeout': 5,
                'env_vars': {}
            }
        },
        'llm_routes': [
            {
                'name': 'gemini-cli-test',
                'provider': 'gemini_cli',
                'model': 'config-defined-model'
            }
        ]
    }
    
    detector = EventDetector(config)
    
    # Mock tweet
    tweet = {
        'id': '123456789',
        'text': 'Test tweet with keyword test'
    }
    
    print(f"Testing with command: {config['llm_providers']['gemini_cli']['command']}")
    print(f"Configured model: {config['llm_routes'][0]['model']}")
    print("Running detection...")
    
    # We need to bypass quick_keyword_check or make it pass
    # Since we are mocking the tweet to contain 'test' and adding 'test' to keywords, it should pass.
    
    results = await detector.detect_event_tweets([tweet])
    
    if results:
        print("\nSuccess! Detected event:")
        print(results[0]['event_analysis'])
        
        # Verify model was passed correctly (check reason string in mock response)
        reason = results[0]['event_analysis']['reason']
        if "Model: config-defined-model" in reason:
             print("\nVerification Passed: Model argument was correctly passed from config.")
        else:
             print(f"\nVerification FAILED: Model argument missing or incorrect in reason: {reason}")

    else:
        print("\nFailed to detect event.")

if __name__ == "__main__":
    asyncio.run(test_cli_integration())
