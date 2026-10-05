"""Process-wide embedding service for OLAV.

Single entry point for all embedding operations:
  - API mode  (default): OpenAI-compatible client singleton → OpenRouter
  - Local mode:          SentenceTransformer singleton (offline/legacy)
  - None/disabled:       returns None, callers degrade to text-only search

All components call ``embed_text()`` — never construct clients themselves.
``get_embedder()`` is retained for local-mode callers that need the raw
SentenceTransformer object (e.g. dimension detection).

LEGACY-KEEP: local mode is labelled "legacy" because API mode is the
default since v0.15, but offline deployments still need it — keep it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# ── Local-mode SentenceTransformer singleton ──────────────────────────────────
_local_embedder = None

# ── API-mode OpenAI client singleton ─────────────────────────────────────────
_api_client = None
_api_model: str | None = None

# ── In-process embedding cache (LRU, sha256-keyed) ──────────────────────────
# Same text → same vector; multi-step agent flows often re-embed identical
# user messages 5-7× per chapter (orchestrator + sub-agents + tool decisions
# all run AutoRecallMiddleware.abefore_model). The cache turns those
# repeats into 1 real API call + N memory hits.
#
# Cap controlled by ``OLAV_EMBED_CACHE_SIZE`` env var (default 1024 entries
# ≈ ~6 MB at 1536-dim float32). Set to 0 to disable.
_EMBED_CACHE_MAX = int(os.environ.get("OLAV_EMBED_CACHE_SIZE", "1024"))
_embed_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_embed_cache_stats = {"hits": 0, "misses": 0}


def _cache_get(text: str) -> "list[float] | None":
    """Return cached vector for ``text`` or ``None``. LRU bump on hit."""
    if _EMBED_CACHE_MAX <= 0:
        return None
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    vec = _embed_cache.get(key)
    if vec is not None:
        _embed_cache.move_to_end(key)
        _embed_cache_stats["hits"] += 1
        return vec
    _embed_cache_stats["misses"] += 1
    return None


def _cache_put(text: str, vec: "list[float]") -> None:
    """Store ``text → vec`` in LRU; evict oldest if at capacity."""
    if _EMBED_CACHE_MAX <= 0:
        return
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _embed_cache[key] = vec
    _embed_cache.move_to_end(key)
    while len(_embed_cache) > _EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)


def get_embed_cache_stats() -> dict[str, int]:
    """Return current cache hit/miss counters + size. For diagnostics."""
    return {**_embed_cache_stats, "size": len(_embed_cache)}


def clear_embed_cache() -> None:
    """Reset cache + stats. Useful in tests or after config switch."""
    _embed_cache.clear()
    _embed_cache_stats["hits"] = 0
    _embed_cache_stats["misses"] = 0


def local_embed_available() -> bool:
    """True when the ``[local-embed]`` extra (sentence-transformers) is importable.

    Canonical answer for "can this install do on-CPU embedding" — the first-run
    wizard and ``olav doctor`` both branch on it, and a second copy of the check
    would be free to drift from this one.

    Uses ``find_spec`` rather than a real import: importing sentence-transformers
    drags in torch (seconds of startup) to answer a yes/no question.
    """
    import importlib.util

    return importlib.util.find_spec("sentence_transformers") is not None


def get_embedder(model: str | None = None):
    """Return the process-wide SentenceTransformer singleton (local mode only).

    Configuration is read from ``EmbeddingConfig`` (`.olav/config/api.json`):

    - ``embedding.mode = "api"``  (preferred): returns ``None``; use
      ``embed_text()`` for actual embedding work — the API client is managed
      separately as ``_api_client``.
    - ``embedding.mode = "local"``: load the SentenceTransformer singleton
      specified by ``embedding.local.model`` on ``embedding.local.device``.
    - ``embedding.mode = "none"``: embedding disabled; returns ``None``.

    Returns ``None`` (never raises) on any failure so callers degrade
    gracefully.  Progress bars and verbose load reports are suppressed.
    """
    global _local_embedder
    if _local_embedder is None:
        try:
            from olav.core.config import get_embedding_config

            emb_cfg = get_embedding_config()
            mode = emb_cfg.mode  # "local" | "api" | "none"

            if mode in ("api", "none", "disabled"):
                logger.debug("Embedder skipped (mode=%s); use embed_text() for API embeddings.", mode)
                _local_embedder = False  # sentinel: do not retry
            else:
                # Local SentenceTransformer
                resolved_model = model or emb_cfg.local_model
                device = emb_cfg.device  # "cpu" | "cuda" | "mps"

                # Ships in the `[local-embed]` extra, not the default install
                # (2026-08-04) — it is the only thing pulling torch/triton into
                # the wheel.  Report the fix instead of a bare ImportError:
                # local mode is a deliberate choice, so someone who selected it
                # and got nothing needs to know it is one `pip install` away.
                try:
                    from sentence_transformers import SentenceTransformer  # type: ignore[import]
                except ImportError as _missing:
                    logger.warning(
                        "embedding.mode is 'local' but sentence-transformers is not "
                        "installed: %s.  Install it with `pip install olav[local-embed]`, "
                        "or switch to `embedding.mode = \"api\"` (the default) and point "
                        "`embedding.api.base_url` at an embedding endpoint.",
                        _missing,
                    )
                    _local_embedder = False  # sentinel: do not retry
                    return None

                for _noisy in ("sentence_transformers", "transformers", "transformers.modeling_utils"):
                    logging.getLogger(_noisy).setLevel(logging.ERROR)

                try:
                    import os as _os
                    # Suppress C-level stdout+stderr (safetensors shard reports come on fd 2)
                    _saved1 = _os.dup(1)
                    _saved2 = _os.dup(2)
                    _null = _os.open(_os.devnull, _os.O_WRONLY)
                    _os.dup2(_null, 1)
                    _os.dup2(_null, 2)
                    try:
                        _local_embedder = SentenceTransformer(
                            resolved_model, device=device
                        )
                    finally:
                        _os.dup2(_saved1, 1)
                        _os.dup2(_saved2, 2)
                        _os.close(_null)
                        _os.close(_saved1)
                        _os.close(_saved2)
                    logger.info(
                        "Shared embedder loaded (device=%s): %s", device, resolved_model
                    )
                except Exception as _load_err:
                    logger.warning(
                        "Embedder model '%s' failed to load: %s. "
                        "Run `olav init` to download, or set "
                        "`embedding.mode = \"none\"` to disable.",
                        resolved_model, _load_err,
                    )
                    _local_embedder = False  # sentinel: do not retry
        except Exception as exc:  # pragma: no cover – environment-specific
            logger.warning("Embedder unavailable (%s); memory features disabled.", exc)
            _local_embedder = False  # sentinel: do not retry
    return _local_embedder if _local_embedder else None


def _get_api_client():
    """Return the process-wide OpenAI-compatible API client singleton."""
    global _api_client, _api_model
    if _api_client is None:
        from olav.core.config import get_embedding_config

        cfg = get_embedding_config()
        import openai

        client_kwargs: dict = {"api_key": cfg.openai_api_key}
        if cfg.openai_base_url:
            client_kwargs["base_url"] = cfg.openai_base_url
        # Bound every embed call. The SDK default is a 600s timeout × 2
        # retries (~30 min), so a slow/unresponsive embed endpoint can
        # wedge `olav init` / `olav skill install` (which embed every
        # *.guide.yaml) until the CI job is killed with no log footer —
        # exactly the e2e-nightly setup hang seen 2026-06-14. A bad
        # endpoint must degrade (skip-on-failure in `_embed`), not hang.
        client_kwargs["timeout"] = float(os.environ.get("OLAV_EMBED_TIMEOUT", "30"))
        client_kwargs["max_retries"] = int(os.environ.get("OLAV_EMBED_MAX_RETRIES", "1"))
        _api_client = openai.OpenAI(**client_kwargs)
        _api_model = cfg.openai_model
        logger.debug("API embedding client initialized (model=%s)", _api_model)
    return _api_client, _api_model


_detected_dim: int | None = None

# --- endpoint circuit breaker ------------------------------------------------
# Every call is already bounded (OLAV_EMBED_TIMEOUT × retries), but the *batch*
# is not: `olav init` / `olav skill install` embed every *.guide.yaml one at a
# time, so a dead endpoint costs N × the per-call bound. That is the 2026-06-14
# CI setup hang, and it cost ~40 minutes again on 2026-08-02 when a local unit
# run blocked on it. A per-call timeout stops one call from hanging; only a
# breaker stops the run from hanging.
#
# Consecutive failures — not a wall-clock budget — because the question is "is
# the endpoint usable", not "is it fast": a slow but working endpoint must keep
# working. The cool-off makes it self-healing, so a transient blip does not
# disable embedding for the life of the process.
_embed_failures = 0
_breaker_open_until = 0.0


def _breaker_threshold() -> int:
    return int(os.environ.get("OLAV_EMBED_FAILURE_THRESHOLD", "3"))


def _breaker_cooldown() -> float:
    return float(os.environ.get("OLAV_EMBED_BREAKER_COOLDOWN", "60"))


def _breaker_is_open() -> bool:
    return time.monotonic() < _breaker_open_until


def embedding_backend_ready() -> tuple[bool, str]:
    """Can this install embed at all? Returns ``(ready, reason)``.

    For callers that *write* embeddings as a side effect of doing something
    else — priming memory during ingest, for instance. They need to know
    before they start, because "try it and see" costs a failed call per entry
    and buries the real work under warnings the operator cannot act on.

    Config first, deliberately: the two hopeless cases are answerable with no
    I/O at all. ``mode=local`` without the ``[local-embed]`` extra cannot
    embed, and ``mode=api`` with neither a ``base_url`` nor a key is not an
    unreachable endpoint — it is *no endpoint*, which is the state of every
    install that never wrote ``api.json``. Only when something is actually
    configured do we spend a probe on it.

    ``reason`` is filled in both directions so a caller can record *why* it
    skipped rather than reporting an empty result with no explanation.
    """
    from olav.core.config import get_embedding_config

    try:
        cfg = get_embedding_config()
    except Exception as exc:  # noqa: BLE001
        return False, f"embedding config unavailable: {exc}"

    mode = (cfg.mode or "api").lower()
    if mode in ("none", "disabled"):
        return False, f"embedding disabled (embedding.mode={mode})"

    if mode == "local":
        if local_embed_available():
            return True, "local embedder available"
        return False, (
            "embedding.mode=local but sentence-transformers is not installed "
            "(pip install 'olav[local-embed]')"
        )

    if not cfg.openai_base_url and not cfg.openai_api_key:
        return False, (
            "no embedding backend configured — embedding.mode=api with neither "
            "a base_url nor an api key. Set one, or install the on-CPU extra "
            "(pip install 'olav[local-embed]') and set OLAV_EMBEDDING_MODE=local"
        )

    if _breaker_is_open():
        return False, "embedding endpoint is in breaker cool-off after repeated failures"

    if detect_embedding_dim() is None:
        return False, "embedding endpoint configured but did not answer a probe"
    return True, "api embedding endpoint answered"


def _is_unreachable(exc: BaseException) -> bool:
    """True when the endpoint did not answer at all.

    A timeout or a refused connection says the service is unusable, and one
    *recorded* failure already means two attempts — the SDK client is built
    with ``max_retries``, so it has retried internally before raising. Waiting
    for two more of those buys no information and costs the caller minutes.
    Other errors (4xx, a malformed response) may be specific to one input, so
    those still need the consecutive-failure evidence.

    Matched on the class name rather than by importing openai/httpx: this
    module must not grow import-time dependencies on either.
    """
    names = {type(e).__name__ for e in (exc, exc.__cause__, exc.__context__) if e}
    return any(
        "Timeout" in n or "ConnectError" in n or "ConnectionError" in n
        or "APIConnection" in n
        for n in names
    )


def _breaker_record_failure(exc: BaseException | None = None) -> None:
    global _embed_failures, _breaker_open_until
    _embed_failures += 1
    if exc is not None and _is_unreachable(exc):
        _embed_failures = max(_embed_failures, _breaker_threshold())
    if _embed_failures >= _breaker_threshold() and not _breaker_is_open():
        _breaker_open_until = time.monotonic() + _breaker_cooldown()
        logger.warning(
            "embed endpoint unusable after %d consecutive failures — skipping "
            "embeds for %.0fs. Callers degrade (entries are skipped, not "
            "blocked); to embed without a service install the on-CPU extra "
            "(`pip install olav[local-embed]`) and set OLAV_EMBEDDING_MODE=local.",
            _embed_failures, _breaker_cooldown(),
        )


def _breaker_record_success() -> None:
    global _embed_failures, _breaker_open_until
    _embed_failures = 0
    _breaker_open_until = 0.0


def reset_embed_breaker() -> None:
    """Clear breaker state. For tests and for callers that just fixed config."""
    _breaker_record_success()


def detect_embedding_dim() -> "int | None":
    """Detect the actual embedding dimension by running a probe.

    Returns ``None`` when the dimension cannot be determined — the backend is
    unreachable and there is no local embedder to ask. **Do not substitute a
    default.** Until 2026-08-04 this returned a hardcoded ``512`` ("safe default
    for bge-small-zh-v1.5"), which produced a false data-corruption alarm: a
    transient embed failure made the store compare a stored 768-dim table
    against an invented 512 and refuse to start with

        Embedding dim mismatch on table 'memory': stored=768, embedder=512.
        Refusing to start to avoid silent data loss.

    — pointing the operator at their config when nothing was wrong with it
    (observed on gitea CI run #338, intermittently: the embed endpoint flapped).
    The invented value also stopped being plausible once api mode became the
    default and sentence-transformers moved to the `[local-embed]` extra, so
    bge-small-zh is no longer any kind of default.

    A successful result is cached for the process lifetime; **a failure is not**,
    so a caller after a transient outage can still get the real answer. Callers
    must handle ``None`` — see ``core/memory``'s dim resolution, which adopts the
    stored table's width rather than guessing.
    """
    global _detected_dim
    if _detected_dim is not None:
        return _detected_dim

    vec = embed_text("dimension probe")
    if vec is not None:
        _detected_dim = len(vec)
        logger.info("Detected embedding dimension: %d", _detected_dim)
        return _detected_dim

    # Fallback: ask local embedder directly
    emb = get_embedder()
    if emb is not None:
        dim_fn = getattr(emb, "get_embedding_dimension", None) or getattr(emb, "get_sentence_embedding_dimension", None)
        if dim_fn:
            _detected_dim = int(dim_fn())
            return _detected_dim

    logger.warning(
        "Embedding dimension undetectable: the endpoint did not answer and no "
        "local embedder is available. Not guessing — callers decide how to "
        "proceed (an existing table's own width is the better source)."
    )
    return None


def embed_text(text: str) -> "list[float] | None":
    """Embed text using the configured embedding backend (api or local).

    In api mode, reuses the process-wide OpenAI-compatible client singleton.
    In local mode, delegates to the shared SentenceTransformer singleton.
    Returns None on any failure so callers degrade gracefully.

    Process-wide LRU cache (configurable via ``OLAV_EMBED_CACHE_SIZE``,
    default 1024 entries) deduplicates identical text — typical multi-
    step agent flows re-embed the same user message 5-7× per chapter,
    so the cache typically converts those into 1 real API call + N
    memory hits without changing semantics.
    """
    if not text:
        return None

    # R100/S3 (2026-04-29): cap input length to protect against
    # llama-server's per-request batch_size ceiling.  When OLAV
    # AutoCapture embeds a growing conversation (system prompt + tool
    # outputs + thinking traces), the input length grows past the
    # embed server's ``--batch-size`` setting and llama-server returns
    # 500 ``input (N tokens) is too large to process``.  Demo7 Ch8
    # observed 5791 → 8650 → 11507 token inputs failing the
    # batch=4096 / 8192 server caps respectively.  Truncating to
    # ~6000 chars (≈ 1500-2000 tokens for English / SQL / mermaid
    # mixed content) is a safe upper bound that fits the typical
    # embed-server batch_size.  Override via env
    # ``OLAV_EMBED_MAX_CHARS`` (0 = no cap).
    _max_chars_env = os.environ.get("OLAV_EMBED_MAX_CHARS")
    if _max_chars_env is not None:
        try:
            _max_chars = int(_max_chars_env)
        except ValueError:
            _max_chars = 6000
    else:
        try:
            from olav.core.config import get_embedding_config
            _raw = get_embedding_config().max_input_chars
            _max_chars = int(_raw) if isinstance(_raw, (int, float, str)) else 6000
        except Exception:
            _max_chars = 6000
    if _max_chars > 0 and len(text) > _max_chars:
        original_len = len(text)
        text = text[:_max_chars]
        logger.debug(
            "embed_text input truncated: %d → %d chars (cap=%d, set "
            "OLAV_EMBED_MAX_CHARS=0 to disable)",
            original_len, _max_chars, _max_chars,
        )

    cached = _cache_get(text)
    if cached is not None:
        return cached
    if _breaker_is_open():
        return None
    try:
        from olav.core.config import get_embedding_config

        cfg = get_embedding_config()
        if cfg.mode == "api":
            client, model = _get_api_client()
            # encoding_format="float" — the openai SDK defaults to "base64"
            # when not specified, which some OpenAI-compatible proxies
            # don't honour (they return plain float arrays anyway, but
            # pydantic validation in the SDK then drops the response as
            # malformed → "No embedding data received").  Forcing "float"
            # is the standards-compliant request and the cheapest fix.
            # Some providers (NVIDIA NIM) require an `input_type` field
            # in the request body (e.g. "query" or "passage").
            # Set OLAV_EMBEDDING_INPUT_TYPE to pass it via extra_body.
            _input_type = os.environ.get("OLAV_EMBEDDING_INPUT_TYPE")
            _extra = {"input_type": _input_type} if _input_type else None
            resp = client.embeddings.create(
                input=text, model=model, encoding_format="float",
                **({"extra_body": _extra} if _extra else {}),
            )
            vec = resp.data[0].embedding
        else:
            embedder = get_embedder()
            if embedder is None:
                return None
            vec = embedder.encode(text, normalize_embeddings=True).tolist()
        _cache_put(text, vec)
        _breaker_record_success()
        return vec
    except Exception as exc:
        logger.warning("embed_text failed: %s", exc)
        _breaker_record_failure(exc)
        return None
