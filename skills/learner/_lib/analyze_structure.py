"""Pre-scan raw CLI output for structural hints.

The hints are passed to the LLM prompt preamble so the LLM can reason
about DSL choice (TextFSM vs Python) and parser structure without
having to re-analyze the output itself. This keeps the prompt focused
on code generation.

The LLM is the authoritative decider (emits `# OLAV_DSL:` marker);
these hints are advisory.
"""

from __future__ import annotations

import re
from typing import Any


def analyze_structure(raw: str) -> dict[str, Any]:
    """Return structural features of a raw CLI output sample.

    Features emitted:
      * line_count
      * non_blank_line_count
      * max_line_len
      * blank_line_blocks: count of blank-line-separated blocks
      * indented_continuation: lines starting with 2+ spaces that continue prior line
      * has_aligned_columns: table-style header + aligned rows detected
      * has_key_value_indented: "Key: value" rows indented under a header
      * looks_like_error: `% Invalid`, `Error:`, etc.
      * looks_like_config: `!`, `interface ...`, `router ...` dominant
    """
    if not raw or not raw.strip():
        return {
            "line_count": 0, "non_blank_line_count": 0,
            "max_line_len": 0, "blank_line_blocks": 0,
            "indented_continuation": 0, "has_aligned_columns": False,
            "has_key_value_indented": False,
            "looks_like_error": False, "looks_like_config": False,
        }

    lines = raw.splitlines()
    non_blank = [ln for ln in lines if ln.strip()]

    # Blank-line block count
    blank_blocks = 0
    prev_blank = True
    for ln in lines:
        cur_blank = not ln.strip()
        if prev_blank and not cur_blank:
            blank_blocks += 1
        prev_blank = cur_blank
    if len(non_blank) <= 1:
        blank_blocks = 0

    # Indented continuation
    indented_cont = sum(1 for ln in non_blank if ln.startswith("  "))

    # Key-value indented pattern: "  Key: value"
    kv_pattern = re.compile(r"^\s{2,}[A-Za-z][A-Za-z0-9 _-]*:\s*\S")
    kv_indented = sum(1 for ln in non_blank if kv_pattern.match(ln))
    has_kv_indented = kv_indented >= 3

    # Aligned columns: 3+ lines with consistent whitespace-separated columns
    has_aligned = _detect_aligned_columns(non_blank)

    # Error output
    first = non_blank[0] if non_blank else ""
    looks_error = bool(re.search(
        r"(%\s*Invalid|Error:|Unknown command|Permission denied|not found)",
        first, re.IGNORECASE,
    ))

    # Config output (running-config style)
    config_markers = sum(
        1 for ln in non_blank[:20]
        if re.match(r"^(!|interface |router |hostname |vrf |line |ip route|version )", ln)
    )
    looks_config = config_markers >= 5

    return {
        "line_count": len(lines),
        "non_blank_line_count": len(non_blank),
        "max_line_len": max((len(ln) for ln in lines), default=0),
        "blank_line_blocks": blank_blocks,
        "indented_continuation": indented_cont,
        "has_aligned_columns": has_aligned,
        "has_key_value_indented": has_kv_indented,
        "looks_like_error": looks_error,
        "looks_like_config": looks_config,
    }


def _detect_aligned_columns(lines: list[str]) -> bool:
    """Detect if 3+ consecutive non-blank lines look like a table."""
    if len(lines) < 3:
        return False
    # Find a stretch of 3+ lines with consistent column count
    for start in range(len(lines) - 2):
        windows = lines[start:start + 5]
        col_counts = [len(ln.split()) for ln in windows]
        if len(set(col_counts)) == 1 and col_counts[0] >= 3:
            return True
        # Tolerate +/-1 column variance
        if max(col_counts) - min(col_counts) <= 1 and min(col_counts) >= 3:
            return True
    return False


def render_hints(struct: dict[str, Any]) -> str:
    """Render the struct dict as a prompt preamble string."""
    parts: list[str] = []
    parts.append(f"- lines: {struct['line_count']} total, {struct['non_blank_line_count']} non-blank")
    if struct["blank_line_blocks"] >= 2:
        parts.append(f"- blank-line-separated blocks: {struct['blank_line_blocks']} → multi-block record structure")
    if struct["has_aligned_columns"]:
        parts.append("- aligned-column table detected (header + rows)")
    if struct["has_key_value_indented"]:
        parts.append("- indented Key: value rows detected")
    if struct["indented_continuation"] > 5:
        parts.append(f"- {struct['indented_continuation']} indented continuation lines")
    return "\n".join(parts) if parts else "- (no strong structural signal)"
