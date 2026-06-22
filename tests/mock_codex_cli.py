import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("subcommand", nargs="?")
    parser.add_argument("--skip-git-repo-check", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--ignore-rules", action="store_true")
    parser.add_argument("--sandbox")
    parser.add_argument("--ask-for-approval")
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("-m", "--model")
    parser.add_argument("--output-schema")
    parser.add_argument("-o", "--output-last-message")
    parser.add_argument("prompt", nargs="?")
    args, unknown = parser.parse_known_args()
    prompt_arg = args.prompt
    if prompt_arg is None and "-" in unknown:
        prompt_arg = "-"
    prompt = sys.stdin.read() if prompt_arg == "-" else (prompt_arg or "")
    if args.model == os.environ.get("MOCK_CODEX_RATE_LIMIT_MODEL"):
        print("429 rate limit exceeded", file=sys.stderr)
        sys.exit(1)

    effort = None
    for config_item in args.config:
        if config_item.startswith("model_reasoning_effort="):
            effort = config_item.split("=", 1)[1].strip('"')

    response = {
        "is_event_related": True,
        "confidence": 0.95,
        "event_type": "Comic Market",
        "event_date": None,
        "participation_type": "Circle",
        "reason": (
            f"Mock Codex CLI response. Model: {args.model}. "
            f"Effort: {effort}. "
            f"Prompt contains tweet: {'ツイート本文:' in prompt}. "
            f"Schema: {bool(args.output_schema)}"
        ),
    }

    result = json.dumps(response, ensure_ascii=False)
    if args.output_last_message:
        with open(args.output_last_message, "w", encoding="utf-8") as f:
            f.write(result)
    else:
        print(result)


if __name__ == "__main__":
    main()
