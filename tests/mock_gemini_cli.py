import sys
import json
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--prompt', type=str, help='Prompt text')
    parser.add_argument('--model', type=str, help='Model name')
    args, unknown = parser.parse_known_args()
    
    prompt = sys.stdin.read() if '-' in unknown else args.prompt
    model = args.model
    
    # Simple mock response
    response = {
        "is_event_related": True,
        "confidence": 0.95,
        "event_type": "Comic Market",
        "event_date": "2024-08-12",
        "participation_type": "Circle",
        "reason": f"Mock CLI response via -p. Prompt len: {len(prompt) if prompt else 0}. Model: {model}"
    }
    
    print(json.dumps(response, ensure_ascii=False))

if __name__ == "__main__":
    main()
