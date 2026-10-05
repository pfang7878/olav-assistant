#!/usr/bin/env python3
"""prepare_learn — analyse raw CLI output and return the parser-drafting prompt.

Step 1 of 2. The learner pipeline has a hole in the middle where a model writes
the parser; in OLAV that hole is filled by an `LLMFactory` round-trip, and here
it is filled by the model already reading this output. So this script stops at
the prompt and hands it back.

    echo '{"platform":"cisco_ios","command":"show ip interface brief",
           "samples":[{"device":"R2","raw_output":"..."}]}' \
      | python scripts/prepare_learn.py

Returns ``{prompt, hints, sample_count}``. Write the parser the prompt asks for —
including its ``# OLAV_DSL: textfsm`` first line — then pass it to
``finish_learn.py``, which validates it in a sandbox and freezes it.

Thin wrapper: the analysis and prompt construction live in ``_lib/`` and are the
same code OLAV runs.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "_lib"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prepare_learn(
    platform: str,
    command: str,
    samples: list[dict],
) -> dict:
    if not samples:
        return {"status": "error", "error": "samples must hold at least one "
                                            "{device, raw_output} entry"}

    analyze = _load("analyze_structure")
    draft = _load("draft_parser")

    first_raw = samples[0].get("raw_output") or ""
    hints = analyze.analyze_structure(first_raw)
    prompt = draft.build_prompt(platform, command, samples, analyze.render_hints(hints))

    return {
        "status": "ok",
        "platform": platform,
        "command": command,
        "sample_count": len(samples),
        "hints": hints,
        "prompt": prompt,
        "next": (
            "Write the parser this prompt asks for, then call finish_learn.py "
            "with {platform, command, parser_response, samples}. The response "
            "must begin with the '# OLAV_DSL: textfsm' marker line."
        ),
    }


if __name__ == "__main__":
    args = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(prepare_learn(**args), default=str))
