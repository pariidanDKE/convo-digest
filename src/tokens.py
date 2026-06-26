#!/usr/bin/env python3
"""Token estimation + size tiering.

The token backend is **swappable** behind the `TokenCounter` protocol so the
tiering logic never changes:

  - `CharHeuristicCounter` — pure-stdlib estimate (chars / chars-per-token). No
    dependencies; deliberately conservative (overestimates) so a near-cap convo
    tiers to the sampler rather than overflowing a whole summarizer. The zero-setup
    default that lets the digest run with no `pip install`.
  - `TiktokenCounter` — local, no API key, but needs the `tiktoken` package.
    tiktoken o200k_base x ~1.15 uplift to approximate Claude's (heavier) tokenizer.
    More accurate; used automatically when tiktoken is importable.

`default_counter()` returns tiktoken when it imports, else the char heuristic — so
the digest works out of the box and transparently sharpens if tiktoken is present.
Force one with `$CCD_TOKEN_COUNTER=char|tiktoken`. Nothing else moves.

Tiering decides whether a stripped conversation is summarized **whole** or handed
to the **sampler** — purely on token count vs a cap (see SPEC.md §4.2/§4.6).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from transcript import Transcript

# Cap default; calibration sets this per user at runtime (SPEC §4.6). Set under
# Haiku's 200K window with headroom for the prompt + generated summary.
DEFAULT_CAP_TOKENS = 150_000
# Claude tokenizes ~10-15% heavier than o200k (measured); uplift the local estimate.
DEFAULT_UPLIFT = 1.15
# Char heuristic divisor. Calibrated against 172 real transcripts (tiktoken+uplift):
# chars/token ranged 2.22 (densest, code/JSON-heavy) to 4.31 (prose), median 3.54.
# We pick 2.8 — below the median on purpose so the estimate runs HIGH: it stays
# conservative on ~98% of convos AND keeps even worst-case-dense convos at the cap
# on the SAMPLE side (a 200K-tok convo at 2.22 cpt = 444K chars / 2.8 = 159K > cap),
# so we never feed an over-cap convo to a whole summarizer. Over-tiering prose near
# the cap is the harmless direction (it just gets downsampled). See SPEC §4.6.
DEFAULT_CHARS_PER_TOKEN = 2.8


@runtime_checkable
class TokenCounter(Protocol):
    """Estimate the number of **Claude** tokens in a string."""

    name: str

    def count(self, text: str) -> int: ...


class TiktokenCounter:
    """Local, no-API-key estimator: tiktoken o200k_base x uplift."""

    def __init__(self, encoding: str = "o200k_base", uplift: float = DEFAULT_UPLIFT):
        import tiktoken  # lazy: only needed for this backend
        self._enc = tiktoken.get_encoding(encoding)
        self.uplift = uplift
        self.name = f"tiktoken:{encoding} x{uplift}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        raw = len(self._enc.encode(text, disallowed_special=()))
        return math.ceil(raw * self.uplift)


class CharHeuristicCounter:
    """Pure-stdlib token estimate: ceil(len(text) / chars_per_token). No deps.

    `chars_per_token` is set BELOW o200k's typical ~4.0 chars/token so the estimate
    runs HIGH on purpose. Overestimating only pushes a borderline conversation into
    the sampler (safe — extra downsampling); it can never let an over-cap convo be
    fed WHOLE to a summarizer (unsafe — context overflow). See SPEC §4.6.
    """

    def __init__(self, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN):
        self.chars_per_token = chars_per_token
        self.name = f"char-heuristic:~{chars_per_token}cpt"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return math.ceil(len(text) / self.chars_per_token)


def default_counter() -> TokenCounter:
    """tiktoken if importable (more accurate), else the dependency-free char
    heuristic. Force one with `$CCD_TOKEN_COUNTER=char|tiktoken`."""
    import os
    choice = os.environ.get("CCD_TOKEN_COUNTER", "").strip().lower()
    if choice == "char":
        return CharHeuristicCounter()
    try:
        return TiktokenCounter()
    except Exception:
        if choice == "tiktoken":
            raise  # explicitly requested but unavailable — surface it, don't degrade
        return CharHeuristicCounter()


@dataclass
class Tiering:
    tokens: int          # estimated Claude tokens of the stripped conversation
    cap: int
    tier: str            # "whole" | "sample"
    counter: str         # which backend produced the count


def stripped_text(tr: "Transcript") -> str:
    """The text we'd actually feed: stripped user+assistant exchanges, in order."""
    parts = []
    for e in tr.exchanges:
        if e.user_text:
            parts.append(e.user_text)
        if e.assistant_text:
            parts.append(e.assistant_text)
    return "\n".join(parts)


def tier_transcript(
    tr: "Transcript",
    counter: Optional[TokenCounter] = None,
    cap: int = DEFAULT_CAP_TOKENS,
) -> Tiering:
    """Decide whole vs sample for a parsed transcript."""
    counter = counter or default_counter()
    n = counter.count(stripped_text(tr))
    return Tiering(tokens=n, cap=cap, tier=("whole" if n <= cap else "sample"),
                   counter=counter.name)


def _main(argv: list[str]) -> int:
    import os
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import transcript as T
    if not argv:
        print("usage: tokens.py <transcript.jsonl> [cap_tokens]")
        return 2
    cap = int(argv[1]) if len(argv) > 1 else DEFAULT_CAP_TOKENS
    tr = T.parse_transcript(argv[0])
    if not tr.exchanges:
        print(f"{os.path.basename(argv[0])}: no real exchanges (sidechain-only/empty) — skip")
        return 0
    res = tier_transcript(tr, cap=cap)
    print(f"file    : {os.path.basename(tr.path)}")
    print(f"counter : {res.counter}")
    print(f"tokens  : {res.tokens:,}   cap: {res.cap:,}")
    print(f"-> tier : {res.tier.upper()}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
