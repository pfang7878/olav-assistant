#!/usr/bin/env python3
"""finish_learn — validate a drafted parser against the samples, then freeze it.

Step 2 of 2, after ``prepare_learn.py``. Takes the parser text as written (the
whole reply, marker line included — it is parsed off here, not by hand) and runs
the two checks OLAV runs before a parser is allowed to persist:

  * **validate** — the candidate parses every sample. TextFSM is executed by the
    textfsm library; a Python parser additionally passes an AST safety check and
    runs inside ``execute_in_sandbox``.
  * **freeze** — write it to ``.olav/templates/<platform>/<cmd>.textfsm``, where
    ``textfsm_parse`` Priority 1 picks it up on the next parse.

A TextFSM template goes live immediately: ``freeze_parser`` sets
``quarantined=False`` for this DSL unconditionally, and the ``_quarantine/``
tree holds only low-sample *Python* parsers. So a template learned from one
sample is live on the next parse, fitted to that one sample. Pass every sample
you have — the count travels into the header stamp and is what a later reader
has to judge it by.

    echo '{"platform":"cisco_ios","command":"show ip interface brief",
           "parser_response":"# OLAV_DSL: textfsm\\nValue ...",
           "samples":[{"device":"R2","raw_output":"..."}]}' \
      | python scripts/finish_learn.py

Validation failures return ``status: invalid`` with the diagnostic and freeze
nothing — a parser that does not parse its own samples is not a parser.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "_lib"

#: Below this a template is fitted to a single observation. It still goes live
#: (freeze_parser does not quarantine TextFSM), so this only drives the warning.
LOW_SAMPLE_THRESHOLD = 2


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def finish_learn(
    platform: str,
    command: str,
    parser_response: str,
    samples: list[dict],
    contract_version: int = 1,
) -> dict:
    if not samples:
        return {"status": "error", "error": "samples must hold at least one entry"}

    draft = _load("draft_parser")
    dsl, body = draft.parse_response(parser_response)
    if not dsl or not body:
        return {
            "status": "error",
            "error": "no '# OLAV_DSL: textfsm' marker found — pass the reply "
                     "verbatim, marker line included",
        }

    validate = _load("validate_parser")
    result = validate.validate_parser(dsl, body, samples)
    if not result.get("ok"):
        return {
            "status": "invalid",
            "dsl": dsl,
            "diagnostic": result.get("diagnostic", ""),
            "hint": "fix the parser and call finish_learn again; nothing was written",
        }

    raw_concat = "".join((s.get("raw_output") or "") for s in samples).encode()
    freeze = _load("freeze_parser")
    frozen = freeze.freeze_parser(
        dsl,
        body,
        platform,
        command,
        num_samples=len(samples),
        samples_hash=hashlib.sha256(raw_concat).hexdigest(),
        contract_version=contract_version,
    )

    return {
        "status": "ok",
        "dsl": dsl,
        "path": frozen.get("path"),
        "quarantined": frozen.get("quarantined"),
        "sample_count": len(samples),
        "parsed_rows": [len(rows) for rows in result.get("per_sample", [])],
        "note": (
            "live: textfsm_parse Priority 1 will use it on the next parse"
            + (
                f" — WARNING: fitted to {len(samples)} sample(s); TextFSM is not "
                "quarantined, so re-learn with more samples if it was a one-off"
                if len(samples) < LOW_SAMPLE_THRESHOLD
                else ""
            )
        ),
    }


if __name__ == "__main__":
    args = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(finish_learn(**args), default=str))
