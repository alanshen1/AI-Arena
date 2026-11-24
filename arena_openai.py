"""Run one OpenAI Responses API request for Arena's subprocess runner."""

import argparse
import json
import os
import sys
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESPONSES_URL = "https://api.openai.com/v1/responses"
LOCALE_DIR = Path(os.environ.get("ARENA_LOCALES", Path(__file__).resolve().parent / "locales"))


def load_messages(lang):
    """Error messages from locales/<lang>.json ("openai" section), falling back to English."""
    out = {}
    for code in ("en", lang):
        try:
            out.update(json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8")).get("openai", {}))
        except (OSError, ValueError):
            pass
    return out


def events(stream):
    data = []
    for raw_line in stream:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            body = "\n".join(data)
            data.clear()
            if body == "[DONE]":
                return
            yield json.loads(body)


def run(args):
    messages = load_messages(args.lang)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print(messages["key"], file=sys.stderr)
        return 2

    payload = {
        "model": args.model,
        "input": sys.stdin.read(),
        "reasoning": {"effort": args.effort},
        "text": {"verbosity": "high" if args.instructions else "medium"},
        "stream": True,
        "store": False,
    }
    if args.instructions:
        payload["instructions"] = args.instructions
    request = Request(RESPONSES_URL, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    request.add_unredirected_header("Authorization", "Bearer " + key)

    has_answer = False
    try:
        with urlopen(request, timeout=300) as response:
            if response.headers.get_content_type() == "text/event-stream":
                for event in events(response):
                    kind = event.get("type")
                    if kind in ("response.output_text.delta", "response.refusal.delta"):
                        delta = event["delta"]
                        has_answer |= bool(delta.strip())
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    elif kind == "response.completed":
                        if not has_answer:
                            break
                        result = event["response"]
                        print("model:", result.get("model") or args.model, file=sys.stderr)
                        if result.get("usage"):
                            print("usage:", json.dumps(result["usage"]), file=sys.stderr)
                        return 0
                    elif kind in ("response.failed", "response.incomplete", "error"):
                        break
        print(messages["stream"], file=sys.stderr)
    except HTTPError as error:
        if error.code in (401, 403):
            message = messages["auth"]
        elif error.code == 429:
            message = messages["quota"]
        else:
            message = messages["stream"]
        print(f"{message} (HTTP {error.code})", file=sys.stderr)
    except (OSError, HTTPException, URLError):
        print(messages["network"], file=sys.stderr)
    except (KeyError, TypeError, ValueError):
        print(messages["stream"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-6.2-sol")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh"), default="medium")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--instructions", default="")
    sys.exit(run(parser.parse_args()))
