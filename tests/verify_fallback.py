import sys
import os
import asyncio
import logging
import json
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from event_detector import EventDetector

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_fallback():
    print("--- Starting Fallback Verification ---")
    
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
                'command': 'python -c "import time; time.sleep(10)"', # Simulate hanging command
                'args': ['-o', 'json'],
                'timeout': 2, # Short timeout for test
                'env_vars': {}
            },
            'mock_api': {
                'timeout': 2,
                'env_vars': {}
            }
        },
        'llm_routes': [
            {
                'name': 'gemini-cli-timeout',
                'provider': 'gemini_cli',
                'model': 'gemini-cli-model'
            },
            {
                'name': 'mock-api-model',
                'provider': 'mock_api',
                'model': 'mock-api-model'
            }
        ]
    }
    
    detector = EventDetector(config)
    
    # Mocking the _analyze_with_llm method partially to simulate API success for the second model
    # We want to use the REAL _analyze_with_llm for gemini-cli to verify the timeout logic works there,
    # but we need to intercept the second call to 'mock-api-model' to return success.
    
    original_analyze = detector._analyze_with_llm
    
    async def side_effect_analyze(tweet, route):
        route_name = route['name']
        if route_name == 'gemini-cli-timeout':
            print(f"[TEST] Calling real logic for {route_name} (expecting timeout)...")
            # This should trigger the CLI execution which will time out
            return await original_analyze(tweet, route)
        elif route_name == 'mock-api-model':
            print(f"[TEST] Fallback reached! Mocking success for {route_name}")
            return {
                "is_event_related": True,
                "confidence": 0.99,
                "event_type": "Fallback Success",
                "reason": "Fallback mechanism worked"
            }
        else:
            return None

    # Patch the method on the instance
    detector._analyze_with_llm = side_effect_analyze
    
    tweet = {
        'id': 'fallback_test_1',
        'text': 'Test tweet for fallback logic'
    }
    
    results = await detector.detect_event_tweets([tweet])
    
    if results:
        analysis = results[0].get('event_analysis', {})
        print("\n[RESULT]")
        print(f"Event Related: {analysis.get('is_event_related')}")
        print(f"Event Type: {analysis.get('event_type')}")
        print(f"Reason: {analysis.get('reason')}")
        
        if analysis.get('event_type') == "Fallback Success":
            print("\nSUCCESS: System correctly fell back to API after CLI timeout.")
        else:
            print("\nFAILURE: System detected event but not from fallback model??")
    else:
        print("\nFAILURE: No event detected (Fallback failed to execute or return result)")

if __name__ == "__main__":
    asyncio.run(test_fallback())
