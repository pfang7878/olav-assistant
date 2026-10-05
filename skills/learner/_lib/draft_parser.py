"""Draft a parser via a single LLM call; LLM picks TextFSM or Python.

The prompt tells the LLM to emit a marker line (`# OLAV_DSL: textfsm`
or `# OLAV_DSL: python`) followed by the parser source. This replaces
the R70 cascade where auto_learn tried TextFSM first and pac_learn
tried Python as a fallback — one LLM call per command instead of two.
"""

from __future__ import annotations

import logging
import re
import textwrap
from typing import Any

logger = logging.getLogger(__name__)

_DSL_MARKER_RE = re.compile(r"^#\s*OLAV_DSL:\s*(textfsm|python)\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Drop ```...``` code fences if the LLM emitted them.

    Handles:
      * Full-wrap: ```...```
      * Leading fence only: ```\n<body>
      * Trailing fence only: <body>\n```
      * Language-tagged fences: ```python, ```textfsm, etc.
    """
    text = text.strip()
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _render_samples_block(samples: list[dict[str, Any]], max_samples: int = 5,
                          max_chars_per_sample: int = 4000) -> str:
    """Render up to ``max_samples`` sample outputs for the prompt."""
    parts: list[str] = []
    # Pick first + last + middle if many samples
    chosen = samples[:max_samples]
    for i, s in enumerate(chosen, 1):
        dev = s.get("device", f"sample{i}")
        raw = (s.get("raw_output") or "")[:max_chars_per_sample]
        parts.append(f"### Sample {i} (device={dev})\n```\n{raw}\n```")
    if len(samples) > max_samples:
        parts.append(f"### (+{len(samples) - max_samples} more samples omitted for brevity)")
    return "\n\n".join(parts)


def build_prompt(
    platform: str,
    command: str,
    samples: list[dict[str, Any]],
    structural_hints: str,
    prev_code: str | None = None,
    prev_error: str | None = None,
) -> str:
    """Build the single-shot prompt asking LLM to emit a parser."""
    expected_per_sample = [len((s.get("raw_output") or "").splitlines()) for s in samples]

    base = textwrap.dedent(f"""\
    Generate a parser for this network device CLI output.

    Platform: {platform}
    Command : {command}
    Samples : {len(samples)} from {len(set(s.get('device') for s in samples))} device(s)

    ## Structural signals (pre-scan)
    {structural_hints}

    ## Your task

    Emit ONE parser that successfully parses ALL {len(samples)} samples
    into a list of dicts with meaningful fields. Pick the DSL based on
    the input structure:

    - **TextFSM** when the output is an aligned-column table,
      one record per line. Simpler, portable, no sandbox.
    - **Python** when the output has blank-line-separated blocks,
      multi-line records with indented continuation, nested sections,
      or anything TextFSM's line-oriented model would struggle with.

    Start your response with EXACTLY one marker line:

        # OLAV_DSL: textfsm

    or

        # OLAV_DSL: python

    Then emit ONLY the parser source. No prose, no markdown fences, no
    explanation. Your entire response after the marker must be valid
    TextFSM template syntax or valid Python 3 code.

    ## TextFSM syntax reference (required when you pick textfsm)

    A valid TextFSM template has exactly this shape:

        Value NAME (<regex>)
        Value NAME2 (<regex>)
        Value List SOMETHING (<regex>)      # List-valued field
        Value Filldown CURRENT (<regex>)    # Persists across records
        Value Required KEY (<regex>)        # Record only emits if this matched

        Start
          ^${{NAME}}\\s+${{NAME2}} -> Record
          ^. -> Continue

    CRITICAL:
    - The keyword is **Value** (NOT `Variable`, NOT `Field`).
    - Variable references in patterns use `${{NAME}}` (case-sensitive —
      must match the declared `Value NAME` exactly in UPPER-SNAKE-CASE).
    - A `Value` regex captures ONLY the field itself, not surrounding
      context. RIGHT: `Value ROUTER_ID (\\S+)`. WRONG: `Value ROUTER_ID
      (BGP router identifier (\\S+))` — the surrounding text belongs in
      the Start rule, not the value regex.
    - Every state rule MUST start with `^` at column 0 inside the state
      block, indented by 2+ spaces.
    - Action suffixes: `-> Record` emits a record; `-> Continue` keeps
      scanning; `-> Error` aborts. Default is `-> Continue`.
    - DO NOT use `$` inside your regex patterns — TextFSM interprets `$`
      as the start of a variable reference. For blank-line matching use
      just `^\\s*` (no trailing `$`) or omit the rule entirely — TextFSM
      ignores unmatched lines by default when no action fires.
    - Blank lines are tolerated ONLY between Value declarations and
      the Start state, or between states. Do NOT put blank lines
      inside a state's rule block.
    - Use `Required` for the field that anchors a record; rules that
      match without it stay pending.
    - Field names in the parsed dict come out lowercase (TextFSM
      convention). Design your schema with that in mind.
    - Do NOT wrap your response in markdown code fences (```…```).
      Emit raw TextFSM source only.

    ## Python parser reference (required when you pick python)

    Define a top-level `def parse(raw: str) -> list[dict]`. Allowed
    imports: json, re, typing, collections, ipaddress, dataclasses,
    itertools, logging, netutils. FORBIDDEN: os, sys, subprocess,
    shutil, pathlib, socket, urllib, requests, httpx, open, eval,
    exec, compile, __import__, input, getattr, setattr, globals,
    locals, vars.

    ## Samples

    {_render_samples_block(samples)}
    """)

    if prev_code and prev_error:
        base += textwrap.dedent(f"""\

        ## Previous attempt failed

        The previous attempt produced this parser:

        ```
        {prev_code[:2000]}
        ```

        It failed with: {prev_error[:500]}

        Fix the issue and re-emit the complete parser. Don't apologize;
        just produce corrected source.
        """)

    return base


def parse_response(text: str) -> tuple[str | None, str]:
    """Extract the DSL marker and the source body from an LLM response.

    Returns ``(dsl, body)``:
      * ``dsl`` ∈ {"textfsm", "python", None}
      * ``body``: the source after the marker (or the whole response
        stripped of fences if no marker found)
    """
    clean = _strip_fences(text)
    m = _DSL_MARKER_RE.search(clean)
    if not m:
        # Heuristic fallback: if body contains `def parse(` treat as python,
        # if body contains `Value ` at start of a line treat as textfsm.
        if re.search(r"^def parse\s*\(", clean, re.MULTILINE):
            return "python", clean
        if re.search(r"^Value\s+", clean, re.MULTILINE):
            return "textfsm", clean
        return None, clean
    dsl = m.group(1).lower()
    # Strip the marker line from the body so the parser source is clean.
    body = clean[:m.start()] + clean[m.end():]
    return dsl, body.strip()


def draft_parser(
    llm: Any,
    platform: str,
    command: str,
    samples: list[dict[str, Any]],
    structural_hints: str,
    prev_code: str | None = None,
    prev_error: str | None = None,
) -> tuple[str | None, str]:
    """Single LLM call → (dsl, source).

    Returns (None, "") when the LLM is unavailable or the response
    couldn't be parsed; caller should treat as failure + retry.
    """
    if llm is None:
        return None, ""
    prompt = build_prompt(platform, command, samples, structural_hints,
                          prev_code=prev_code, prev_error=prev_error)
    try:
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("draft_parser: LLM invoke failed: %s", exc)
        return None, ""
    return parse_response(text)
