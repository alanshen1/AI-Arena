"""Run one OpenAI Responses API request for Arena's subprocess runner."""

import argparse
import json
import os
import sys
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESPONSES_URL = "https://api.openai.com/v1/responses"
MESSAGES = {
    "zh": {
        "key": "请先设置 ARENA_OPENAI_API_KEY，再重启应用。",
        "auth": "OpenAI API 拒绝了凭证。请检查 Key、项目权限和模型访问权限。",
        "quota": "OpenAI API 暂时无法接收请求。请检查 API 项目的额度、余额和速率限制。",
        "network": "OpenAI API 连接中断或超时。请检查网络后重试。",
        "stream": "OpenAI API 的回答未完整返回。",
    },
    "en": {
        "key": "Set ARENA_OPENAI_API_KEY for Arena, then restart the app.",
        "auth": "OpenAI API rejected the credentials or project permissions. Check the key and model access.",
        "quota": "OpenAI API is limiting requests. Check the project's balance, quota, and rate limits.",
        "network": "The OpenAI API connection failed or timed out. Check the network and retry.",
        "stream": "The OpenAI API response did not finish.",
    },
}


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
    messages = MESSAGES[args.lang]
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
    parser.add_argument("--lang", choices=tuple(MESSAGES), default="zh")
    parser.add_argument("--instructions", default="")
    sys.exit(run(parser.parse_args()))
