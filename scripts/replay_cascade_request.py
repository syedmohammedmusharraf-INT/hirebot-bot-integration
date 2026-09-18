#!/usr/bin/env python3
"""Diagnostic CLI for the "every-turn 400 = silent call" failure class (plan section 4.3, 4.6).

Loads an assistant record, builds just its cascade LLM, and sends one text
turn through it directly -- no LiveKit room/session involved -- printing the
raw response or the raw provider error so a bad model/knob combination is
visible immediately instead of surfacing only as a silent call in a live
meeting.

Usage:
    python scripts/replay_cascade_request.py --assistant-ref meet-voice-bot-v1 --text "hello"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "meet-agent"))
sys.path.insert(0, _REPO_ROOT)

import model_factories  # noqa: E402
from model_support.assistant_config import load_assistant_config  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assistant-ref", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument(
        "--assistants-dir",
        default=os.environ.get("ASSISTANTS_DIR", os.path.join(_REPO_ROOT, "assistants")),
    )
    args = parser.parse_args()

    path = os.path.join(args.assistants_dir, f"{args.assistant_ref}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        cfg = load_assistant_config(yaml.safe_load(f))

    llm = model_factories.create_llm(cfg)

    from livekit.agents.llm import ChatContext

    chat_ctx = ChatContext()
    chat_ctx.add_message(role="user", content=args.text)

    try:
        async with llm.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                print(chunk, end="", flush=True)
        print()
    except Exception as e:  # noqa: BLE001 - deliberately broad: we want to print whatever the provider raised
        print(f"RAW PROVIDER ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
