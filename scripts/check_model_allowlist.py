#!/usr/bin/env python
"""Silent-call diagnostic for a local assistant record.

Implements plan section 4.3 ("local allowlist checks") / section 4.6
("silent-call diagnostics"): everything a bad `assistant_ref` can get wrong
without ever making a network call to a provider -- illegal mode/provider/
model combination, a missing required field, an unknown key. Run this before
wiring a new assistant record into `POST /meet-bots` to catch the "every-turn
400 = silent call" failure class the plan calls out, offline.

Usage:
    python scripts/check_model_allowlist.py --assistant-ref meet-voice-bot-v1
    python scripts/check_model_allowlist.py --assistant-ref my-bot --assistants-dir ./assistants
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from meet_voice_bot.assistant_store import AssistantNotFoundError, InvalidAssistantConfig, load_assistant  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assistant-ref", required=True, help="Assistant ref, e.g. meet-voice-bot-v1")
    parser.add_argument("--assistants-dir", default="./assistants", help="Directory containing {ref}.yaml records")
    args = parser.parse_args()

    print(f"Checking assistant_ref={args.assistant_ref!r} in {args.assistants_dir!r}")
    print("-" * 60)

    try:
        config = load_assistant(args.assistant_ref, args.assistants_dir)
    except AssistantNotFoundError as exc:
        print(f"FAIL  record not found: {exc}")
        return 1
    except InvalidAssistantConfig as exc:
        print(f"FAIL  record failed validation: {exc}")
        return 1

    print(f"PASS  record loaded and passed model_support validation (mode/provider/model, required fields, unknown keys)")
    print(f"PASS  assistant_mode={config.assistant_mode!r} is a legal mode")

    if config.assistant_mode == "cascade":
        print(f"PASS  stt={config.assistant_stt_model!r} config={config.assistant_stt_config}")
        print(f"PASS  llm={config.assistant_llm_config}")
        print(f"PASS  tts={config.assistant_tts_model!r} config={config.assistant_tts_config}")
    else:
        print(f"INFO  assistant_mode={config.assistant_mode!r} -- stt/tts fields are ignored in this mode; see model_support/allowlists.py")

    interaction = config.assistant_interaction_config or {}
    if "speaks_first" not in interaction:
        print("WARN  assistant_interaction_config.speaks_first not set -- defaults may differ between control and agent")
    if not interaction.get("input_guard_window_sec") and interaction.get("input_guard_window_sec") != 0:
        print("WARN  assistant_interaction_config.input_guard_window_sec not set")

    print("-" * 60)
    print("All offline checks passed. This does NOT confirm the provider API keys are valid or the")
    print("model IDs still exist upstream -- see scripts/replay_cascade_request.py for a live probe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
