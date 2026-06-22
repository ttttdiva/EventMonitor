import sys
import json
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--prompt', type=str, help='Prompt text')
    parser.add_argument('--model', type=str, help='Model name')
    args, unknown = parser.parse_known_args()
    
    # Mock response with Markdown code blocks
    response = {
        "is_event_related": True,
        "confidence": 0.9,
        "event_type": "Mock Event",
        "reason": "Markdown test"
    }
    json_str = json.dumps(response, ensure_ascii=False)
    
    print("Here is the result:")
    print("```json")
    print(json_str)
    print("```")

if __name__ == "__main__":
    main()
