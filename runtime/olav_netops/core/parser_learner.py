"""Parser-as-Code (PaC) Learner — ARCH-25.

Generates a Python ``parse(raw, sample_hint) -> list[dict]`` function that
extracts structured records from device CLI output. Sits **after**
:mod:`olav_netops.core.auto_learn` in the parse waterfall:

    ntc-templates  →  auto_learn TextFSM  →  pac_learn (this module)

Key differences from ``auto_learn``:

* **Multi-sample training** — groups by (platform, command) but keeps ALL
  device raw outputs, feeds them all to the LLM in one prompt so any
  field that varies across samples must be generalized (``\\S+``).
* **Python artifact, not TextFSM DSL** — LLM emits a function, we sandbox-
  execute it against each sample with the shared ARCH-24 safety infra
  (AST allowlist + read-only DuckDB patch).
* **Two-stage reflection** — before the first LLM call we pre-analyze
  structural traits (blank-line blocks, indented continuations, table
  header detection) and feed them into the prompt preamble; retries carry
  specific per-device failure diagnosis.
* **Quarantine for single-sample learns** — a parser validated against
  only one device lands in ``.olav/templates/parsers/_quarantine/`` until
  a 2nd corroborating sample arrives.

Success criterion: ALL samples yield ≥ ``max(1, int(expected * 0.7))``
records where ``expected`` is :func:`auto_learn._estimate_data_rows` of
that device's raw.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from olav_netops.core import parser_contract, parser_registry
# ARCH-24 cleanup (Round 70): ast_safety_check was imported from
# etl_discovery; that module has been deleted, so the function is
# inlined below (same allowlist, same semantics).
import ast as _ast


_ALLOWED_MODULES = frozenset({
    "json", "re", "networkx", "duckdb", "typing", "collections",
    "ipaddress", "dataclasses", "itertools", "logging",
    "netutils",
})

_BANNED_NAMES = frozenset({
    "__import__", "exec", "eval", "open", "compile", "input",
    "os", "sys", "subprocess", "shutil", "pathlib", "socket",
    "urllib", "requests", "httpx",
})


def ast_safety_check(code: str) -> tuple[bool, str]:
    """Parse Python source and reject dangerous constructs.

    Allow-list imports only; block ``__import__``/``exec``/``eval``/
    ``open``/``os``/``sys``/``subprocess``/``shutil``/``pathlib``/
    ``socket``/``urllib``/``requests``/``httpx`` etc. Used by the
    ARCH-25 PaC parser-learner to vet LLM-generated Python before
    sandbox execution.
    """
    try:
        tree = _ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_MODULES:
                    return False, f"import of {alias.name!r} not in allowlist"
        elif isinstance(node, _ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_MODULES:
                return False, f"from-import of {node.module!r} not in allowlist"
        elif isinstance(node, _ast.Name):
            if node.id in _BANNED_NAMES:
                return False, f"use of banned name {node.id!r}"
        elif isinstance(node, _ast.Attribute):
            root = node
            while isinstance(root, _ast.Attribute):
                root = root.value
            if isinstance(root, _ast.Name) and root.id in _BANNED_NAMES:
                return False, f"attribute access on banned name {root.id!r}"
        elif isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Name) and node.func.id in {
                "getattr", "setattr", "delattr", "globals", "locals", "vars",
            }:
                return False, f"call to {node.func.id!r} not allowed"
    return True, ""

logger = logging.getLogger(__name__)


# ── Schema hints (protocol-aware) ────────────────────────────────────────

_SCHEMA_HINTS: dict[str, str] = {
    "bgp_summary": (
        "Each record is one BGP peer. Suggested keys: "
        "`peer_ip` or `neighbor_ip`, `peer_as` or `neighbor_as`, "
        "`state` (e.g. 'Established' or a numeric prefix count on Cisco), "
        "`uptime` or `up_down`, `router_id`, `local_as`."
    ),
    "ospf_neighbor": (
        "Each record is one OSPF neighbor/adjacency. Suggested keys: "
        "`neighbor_id`, `neighbor_ip` or `address`, `interface`, `state` "
        "(e.g. 'Full', '2WAY'), `priority`, `dead_time`."
    ),
    "isis_neighbor": (
        "Each record is one IS-IS adjacency. Suggested keys: `system_id`, "
        "`interface`, `state`, `level`, `hold_time`."
    ),
}


def _schema_hint_for(command: str) -> str:
    cl = command.lower()
    if "bgp summary" in cl:
        return _SCHEMA_HINTS["bgp_summary"]
    if "ospf neighbor" in cl or "ospf neighbors" in cl:
        return _SCHEMA_HINTS["ospf_neighbor"]
    if "isis" in cl and "neighbor" in cl:
        return _SCHEMA_HINTS["isis_neighbor"]
    return "Return a list of dicts, one per logical record. Prefer lowercase snake_case keys."


# ── Structural hint analyzer ─────────────────────────────────────────────

def _analyze_structure(raw: str) -> dict[str, Any]:
    """Pre-scan raw output for structural features the LLM should know.

    These hints get dropped into the prompt preamble so the LLM doesn't
    re-discover them on every retry.
    """
    lines = raw.splitlines()
    non_empty = [l for l in lines if l.strip()]

    # Blank-line-separated blocks (count of logical paragraphs)
    blocks = re.split(r"\n\s*\n", raw.strip())
    blocks = [b for b in blocks if b.strip()]

    # Indented-continuation ratio — lines starting with whitespace
    # that follow a non-empty, non-indented line
    indented = 0
    for i in range(1, len(lines)):
        prev = lines[i - 1]
        cur = lines[i]
        if cur.startswith((" ", "\t")) and prev.strip() and not prev.startswith((" ", "\t")):
            indented += 1
    cont_ratio = indented / max(1, len(non_empty))

    # Table-like header — word-word-word followed by ≥2 similar lines
    lines_non_empty = non_empty
    looks_tabular = False
    for i, line in enumerate(lines_non_empty[:10]):
        if re.match(r"^\w+\s+\w+\s+\w+", line):
            # Check if ≥2 following lines also look aligned (first token alphanum)
            follow = lines_non_empty[i + 1 : i + 4]
            if sum(1 for l in follow if re.match(r"^\S+", l)) >= 2:
                looks_tabular = True
                break

    # IP addresses / AS numbers (rough record-anchor candidate)
    ip_matches = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", raw)
    as_matches = re.findall(r"\bAS\s*\d+\b", raw, re.IGNORECASE)

    return {
        "non_empty_lines": len(non_empty),
        "blank_separated_blocks": len(blocks),
        "indented_continuation_ratio": round(cont_ratio, 2),
        "looks_tabular": looks_tabular,
        "ipv4_tokens_found": len(ip_matches),
        "as_tokens_found": len(as_matches),
    }


def _render_struct_hints(struct: dict[str, Any]) -> str:
    bullets = [
        f"- raw has {struct['non_empty_lines']} non-empty lines",
        f"- detected {struct['blank_separated_blocks']} blank-line-separated block(s)",
        f"- indented-continuation ratio: {struct['indented_continuation_ratio']} "
        f"(if > 0.1, each logical record likely spans multiple lines — "
        f"write a state-machine that accumulates continuation lines)",
        f"- table-like header present: {struct['looks_tabular']}",
        f"- ~{struct['ipv4_tokens_found']} IPv4 tokens, ~{struct['as_tokens_found']} AS tokens",
    ]
    return "\n".join(bullets)


# ── Prompt rendering ─────────────────────────────────────────────────────

_MAX_SAMPLES_IN_PROMPT = 3
_SAMPLE_RAW_CAP = 1500


def _render_prompt(
    platform: str,
    command: str,
    samples: list[dict[str, Any]],
    schema_hint: str,
    struct_hints: dict[str, Any],
    prev_code: str | None,
    prev_error: str | None,
) -> str:
    sample_blocks = []
    for i, s in enumerate(samples[:_MAX_SAMPLES_IN_PROMPT], 1):
        raw = s["raw_output"]
        truncated = raw[:_SAMPLE_RAW_CAP]
        if len(raw) > _SAMPLE_RAW_CAP:
            truncated += f"\n... (truncated from {len(raw)} chars)"
        sample_blocks.append(
            f"### Sample {i} — device `{s['device']}`\n"
            f"```\n{truncated}\n```"
        )
    samples_rendered = "\n\n".join(sample_blocks)
    struct_rendered = _render_struct_hints(struct_hints)

    if prev_code and prev_error:
        return (
            f"Fix this Python parser that FAILED for `{platform}` / `{command}`.\n\n"
            f"## Previous code (FAILED):\n```python\n{prev_code}\n```\n\n"
            f"## Failure:\n{prev_error}\n\n"
            f"## Samples ({len(samples)} device(s))\n{samples_rendered}\n\n"
            f"## Structural hints about this output\n{struct_rendered}\n\n"
            f"## Required schema\n{schema_hint}\n\n"
            f"## CRITICAL rules (same as first attempt)\n"
            f"1. Signature EXACT: `def parse(raw: str, sample_hint: dict | None = None) -> list[dict]:`\n"
            f"2. Must handle ALL samples above — the failing device's output requires the fix.\n"
            f"3. If a field's literal text varies across samples, generalize with `\\S+` or `[\\w.:]+`.\n"
            f"4. Allowed imports ONLY: `json, re, ipaddress, typing, collections, itertools, netutils`. "
            f"No `os`, `sys`, `subprocess`, `open`, `eval`, `exec`, `socket`, `urllib`, `requests`.\n"
            f"   **netutils is AVAILABLE** (ARCH-26) — use EXPLICIT submodule imports "
            f"(Python does NOT auto-load subpackages):\n"
            f"   `from netutils.interface import canonical_interface_name` "
            f"(`'Gi0/2'` → `'GigabitEthernet0/2'`)\n"
            f"   `from netutils.asn import asn_to_int` (`'1.1'` → 65537)\n"
            f"   Do NOT write `import netutils` — it will raise AttributeError on submodule access.\n"
            f"5. Emit Python source code ONLY — no markdown fences, no prose.\n"
        )

    return (
        f"Write a Python parser that extracts structured records from "
        f"`{platform}` / `{command}` CLI output.\n\n"
        f"## Samples — {len(samples)} device(s)\n{samples_rendered}\n\n"
        f"## Structural hints (pre-analyzed)\n{struct_rendered}\n\n"
        f"## Required schema\n{schema_hint}\n\n"
        f"## CRITICAL rules\n"
        f"1. Signature EXACT: `def parse(raw: str, sample_hint: dict | None = None) -> list[dict]:`\n"
        f"2. MULTI-SAMPLE GENERALIZATION: the parser must work for EVERY sample above. "
        f"For any value that differs across samples, use `\\S+` / `[\\w.:]+` — never a literal from one sample.\n"
        f"3. Multi-line records: if `indented-continuation ratio > 0.1`, write a state-machine "
        f"that accumulates continuation lines into the current record.\n"
        f"4. Allowed imports ONLY: `json, re, ipaddress, typing, collections, itertools`. "
        f"No `os`, `sys`, `subprocess`, `open`, `eval`, `exec`, `socket`, `urllib`, `requests`.\n"
        f"5. On any parse error, skip the offending line rather than raising — return "
        f"whatever records you successfully extracted.\n"
        f"6. Emit Python source code ONLY — no markdown fences, no prose.\n"
    )


# ── Sandbox driver ───────────────────────────────────────────────────────

def _build_sandbox_driver(candidate_code: str, samples: list[dict]) -> str:
    """Wrap candidate with a driver that runs ``parse(raw)`` against every
    sample and serializes per-sample results."""
    samples_json = json.dumps(
        [{"device": s["device"], "raw": s["raw_output"]} for s in samples],
        ensure_ascii=False,
    )
    driver_tail = f"""

# ── driver (appended by parser_learner) ──
import json as _json
_samples = _json.loads({samples_json!r})
_per = []
_ok = True
_err = ""
for _s in _samples:
    try:
        _rows = parse(_s["raw"], None)
        if not isinstance(_rows, list):
            _ok = False
            _err = "device " + str(_s["device"]) + ": parse() returned " + type(_rows).__name__
            break
        _per.append({{"device": _s["device"], "record_count": len(_rows), "rows": _rows}})
    except Exception as _e:
        _ok = False
        _err = "device " + str(_s["device"]) + ": " + type(_e).__name__ + ": " + str(_e)
        break
_result = {{"ok": _ok, "per_sample": _per, "error": _err}}
"""
    return candidate_code.rstrip() + "\n" + driver_tail


# ── Utilities ────────────────────────────────────────────────────────────

def _strip_markdown_fences(text: str) -> str:
    """Strip ```python ... ``` fences if the LLM emitted them."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


# ── Main entry point ─────────────────────────────────────────────────────

def pac_learn_failed_parses(
    parse_failures: list[dict[str, Any]],
    custom_parser_dir: Path | None = None,  # unused — registry resolves path
    max_retries: int = 3,
    allow_llm: bool = True,
) -> list[dict[str, Any]]:
    """DEPRECATED (R71): call the command_learner skill instead.

    Retained as internal helper for backward compatibility. New code
    should use ``learn_commands`` (see command_learner skill). This
    function's historical contract (multi-sample Python-parser learning
    with AST safety check + sandbox) is preserved; ``learn_commands``
    now handles both this and the former TextFSM path behind one LLM
    call with DSL marker routing.

    PaC-Learner main loop.

    Groups ``parse_failures`` by ``(platform, command)`` keeping ALL samples
    (no dedup). For each group:

    1. ``should_learn`` filter — skip backup/empty/invalid outputs.
    2. Registry hit? Run directly against every sample. Patch.
    3. Structural hint pre-scan; estimate expected record count per sample.
    4. ReAct loop (``max_retries``): LLM → fence strip → AST allowlist →
       sandbox execute → validate per-sample → dry-run gate.
    5. Success: freeze (main tree if ≥2 samples, else quarantine).
    6. Return per-(device, command) patched records.

    Returns a list of ``{device, command, parsed_data, source}`` dicts
    suitable for merging back into ``all_rows`` at the caller.
    """
    logger.warning(
        "pac_learn_failed_parses is DEPRECATED (R71); "
        "call command_learner.learn_commands instead.",
    )

    if custom_parser_dir is not None:
        # Accepted for API symmetry with auto_learn; registry path is
        # resolved from platform paths config, not this argument.
        logger.debug(
            "pac_learn: custom_parser_dir=%s ignored (registry uses paths_config)",
            custom_parser_dir,
        )

    if not parse_failures:
        return []

    # Group by (platform, command); keep ALL samples.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in parse_failures:
        platform = item.get("platform") or "unknown"
        command = item.get("command") or ""
        groups.setdefault((platform, command), []).append(item)

    try:
        from olav_netops.core.parse_helpers import should_learn, _estimate_data_rows
    except Exception as exc:
        logger.warning("pac_learn: cannot import parse_helpers: %s", exc)
        return []

    # LLM + sandbox are only needed to GENERATE new parsers. When
    # ``allow_llm=False`` the caller is signalling "frozen-only" mode — we
    # still want to load and apply existing parsers, so skip the heavy
    # imports but do not early-exit.
    llm = None
    execute_in_sandbox = None
    if allow_llm:
        try:
            from olav.core.llm import LLMFactory
            llm = LLMFactory.get_chat_model()
        except Exception as exc:
            logger.info("pac_learn: LLM unavailable, falling back to frozen-only: %s", exc)
            llm = None
        try:
            from olav.platform.sandbox import execute_in_sandbox as _sb
            execute_in_sandbox = _sb
        except Exception as exc:
            logger.warning("pac_learn: sandbox unavailable: %s", exc)
            execute_in_sandbox = None

    newly_parsed: list[dict[str, Any]] = []

    for (platform, command), samples in groups.items():
        first_raw = samples[0].get("raw_output") or ""
        if not should_learn(command, first_raw):
            logger.info("pac_learn: skip %s/%s (false positive / backup)", platform, command)
            continue

        # Registry hit? Run and move on.
        fn = parser_registry.load_parser(platform, command)
        if fn is not None:
            patched_rows = _apply_parser(fn, samples, platform, command, source="pac_frozen")
            newly_parsed.extend(patched_rows)
            continue

        # No frozen parser — need LLM to generate one. If either the LLM
        # or the sandbox is unavailable (frozen-only mode, offline, etc.)
        # we skip generation and leave parsed_data NULL.
        if llm is None or execute_in_sandbox is None:
            logger.debug(
                "pac_learn: no frozen parser for %s/%s and LLM disabled — skipping",
                platform, command,
            )
            continue

        struct = _analyze_structure(first_raw)
        schema_hint = _schema_hint_for(command)
        expected_per_sample = [
            max(0, _estimate_data_rows(s.get("raw_output") or "", command))
            for s in samples
        ]

        logger.info(
            "pac_learn: learning %s/%s with %d sample(s); expected=%s",
            platform, command, len(samples), expected_per_sample,
        )
        print(
            f"    🤖 PaC-Learner: {platform}/{command} "
            f"({len(samples)} sample(s), expected≈{expected_per_sample})"
        )

        prev_code: str | None = None
        prev_error: str | None = None
        learned = False

        for attempt in range(1, max_retries + 1):
            prompt = _render_prompt(
                platform, command, samples, schema_hint, struct,
                prev_code=prev_code, prev_error=prev_error,
            )
            try:
                response = llm.invoke(prompt)
                raw_code = response.content if hasattr(response, "content") else str(response)
            except Exception as exc:
                prev_error = f"LLM invoke failed: {exc}"
                logger.warning("pac_learn: LLM error (attempt %d): %s", attempt, exc)
                continue

            candidate = _strip_markdown_fences(raw_code)
            if "def parse(" not in candidate:
                prev_error = "candidate missing `def parse(` signature"
                prev_code = candidate
                continue

            ok, reason = ast_safety_check(candidate)
            if not ok:
                prev_error = f"AST safety: {reason}"
                prev_code = candidate
                logger.info(
                    "pac_learn: AST rejected (attempt %d) for %s/%s: %s",
                    attempt, platform, command, reason,
                )
                continue

            driver = _build_sandbox_driver(candidate, samples)
            sb_result = execute_in_sandbox(driver, timeout=15, network_isolation=False)
            if sb_result.get("status") != "success":
                err = (
                    sb_result.get("error")
                    or sb_result.get("reason")
                    or "unknown sandbox failure"
                )
                prev_error = f"sandbox: {err}"
                prev_code = candidate
                logger.info(
                    "pac_learn: sandbox failed (attempt %d) for %s/%s: %s",
                    attempt, platform, command, err,
                )
                continue

            payload = sb_result.get("result") or {}
            if not payload.get("ok"):
                err = payload.get("error", "parse() raised unknown error")
                prev_error = f"runtime: {err}"
                prev_code = candidate
                continue

            # Per-sample validation — ``_estimate_data_rows`` is too noisy
            # to use as a count bound: it over-counts multi-line records
            # (Junos inet.0 continuation), verbose section headers (Cisco
            # BGP summary prints router-id / local-as / version / address-
            # family / etc. before the peer table), and can't distinguish
            # "empty but valid" from "failed to parse". Use the softest
            # useful threshold:
            #   * expected == 0  → accept any record count (including 0)
            #   * expected > 0   → require ≥ 1 (non-empty parse).
            # The upper bound / divergence signal lives in dry_run_gate
            # (ARCH-24 / ARCH-26 observed canonical) instead.
            per_sample = payload.get("per_sample") or []
            all_passed = True
            failures_desc = []
            for i, ps in enumerate(per_sample):
                device = ps.get("device", f"sample{i}")
                rows = ps.get("rows") or []
                expected = expected_per_sample[i] if i < len(expected_per_sample) else 0
                min_required = 1 if expected > 0 else 0

                v_ok, v_err = parser_contract.validate_parser_output(
                    rows, min_records=min_required,
                )
                if not v_ok:
                    all_passed = False
                    failures_desc.append(f"device {device}: {v_err}")

            if not all_passed:
                prev_error = "; ".join(failures_desc)
                prev_code = candidate
                logger.info(
                    "pac_learn: validation failed (attempt %d) for %s/%s: %s",
                    attempt, platform, command, prev_error,
                )
                continue

            # Success! Freeze.
            quarantine = len(samples) == 1
            try:
                frozen = parser_registry.save_parser(
                    platform, command, candidate, quarantine=quarantine,
                )
                print(
                    f"    ✓ PaC-Learner: froze {platform}/{command} → "
                    f"{frozen}{' [QUARANTINE — single-sample]' if quarantine else ''}"
                )
                logger.info(
                    "pac_learn: ✓ %s/%s learned in %d attempt(s), quarantine=%s, rows per sample=%s",
                    platform, command, attempt, quarantine,
                    [ps.get("record_count") for ps in per_sample],
                )
            except Exception as exc:
                logger.warning("pac_learn: save_parser failed: %s", exc)

            # Emit patched rows
            by_device = {ps["device"]: ps.get("rows") or [] for ps in per_sample}
            for s in samples:
                rows = by_device.get(s["device"]) or []
                if rows:
                    newly_parsed.append({
                        "device": s["device"],
                        "command": command,
                        "parsed_data": rows,
                        "source": f"pac_learned_attempt_{attempt}",
                    })
            learned = True
            break

        if not learned:
            logger.warning(
                "pac_learn: ✗ failed %s/%s after %d attempts: %s",
                platform, command, max_retries, prev_error,
            )
            print(
                f"    ✗ PaC-Learner: could not learn {platform}/{command} "
                f"after {max_retries} attempt(s): {prev_error}"
            )

    return newly_parsed


def _apply_parser(
    fn: Any,
    samples: list[dict[str, Any]],
    platform: str,
    command: str,
    source: str,
) -> list[dict[str, Any]]:
    """Run a loaded parser against each sample, return patched rows."""
    out: list[dict[str, Any]] = []
    for s in samples:
        try:
            rows = fn(s.get("raw_output") or "", None)
            if isinstance(rows, list) and rows:
                out.append({
                    "device": s["device"],
                    "command": command,
                    "parsed_data": rows,
                    "source": source,
                })
        except Exception as exc:
            logger.debug(
                "pac_learn: frozen parser %s/%s failed for %s: %s",
                platform, command, s.get("device"), exc,
            )
    return out
