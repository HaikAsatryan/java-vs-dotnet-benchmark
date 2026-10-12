"""Deterministic PCG64 (numpy-free), used by the vegeta target generator.

Why a hand-rolled PCG64 and not `random.Random` or numpy:
  - spec/workload.yaml `rng.algorithm: PCG64` and requires a deterministic
    stream given `target_generator_seed`. This implementation is self-contained
    so the frozen algorithm cannot drift with a numpy upgrade.
  - `random.Random` is a Mersenne Twister, not PCG64, so it would not satisfy
    the frozen algorithm name nor be reproducible against any PCG64 reference.

This is the canonical PCG64 variant `pcg_setseq_128_xsl_rr_64`: a 128-bit LCG
state with the XSL-RR 128->64 output permutation. Constants are the published
PCG reference values (the same ones numpy's PCG64 uses):
  - multiplier  = 0x2360ed051fc65da44385df649fccf645
  - default inc = 0x5851f42d4c957f2d14057b7ef767814f (odd; stream selector)

Seeding here is INTENTIONALLY SIMPLE AND DOCUMENTED rather than numpy's
SeedSequence expansion: the 64-bit `seed` is placed in the low 64 bits of the
128-bit state and the increment is the published default-odd constant. This
yields a fixed, reproducible stream for a given seed. Because the *seeder*
(Go) uses ChaCha8 and a different keyspace-touch path, byte-for-byte
cross-language stream identity is NOT claimed here; what IS guaranteed is
within-Python determinism (the manifest echoes the seed). Hot-set identity is
achieved at the distribution level (same finite inverse-CDF Zipf over the same
ranks), not by matching raw PRNG words. See bench/NOTES.md, PRNG section.
"""

from __future__ import annotations

import hashlib

_MASK64 = (1 << 64) - 1
_MASK128 = (1 << 128) - 1

_PCG_MULT_128 = 0x2360ED051FC65DA44385DF649FCCF645
_PCG_DEFAULT_INC_128 = 0x5851F42D4C957F2D14057B7EF767814F


class PCG64:
    """pcg_setseq_128_xsl_rr_64. Produces uniform 64-bit words."""

    __slots__ = ("_state", "_inc")

    def __init__(self, seed: int, inc: int | None = None) -> None:
        # inc must be odd; the published default is odd. Lower bit forced set.
        self._inc = ((_PCG_DEFAULT_INC_128 if inc is None else inc) & _MASK128) | 1
        # Standard PCG seeding sequence: step, add seed, step.
        self._state = 0
        self._step()
        self._state = (self._state + (seed & _MASK128)) & _MASK128
        self._step()

    @property
    def inc(self) -> int:
        """The 128-bit odd increment selecting this generator's stream.

        Exposed so a per-probe draw can RECORD which stream produced it
        (`vegeta.DrawnTargets.inc`): the seed alone no longer identifies a
        request sequence once `stream_for` derives one stream per probe phase.
        """
        return self._inc

    def _step(self) -> None:
        self._state = (self._state * _PCG_MULT_128 + self._inc) & _MASK128

    def next_u64(self) -> int:
        """Advance state, return the XSL-RR 128->64 permuted output."""
        state = self._state
        self._step()
        # XSL-RR: xor high/low 64, then rotate by the top 6 bits of state.
        rot = state >> 122  # top 6 bits
        xored = ((state >> 64) ^ (state & _MASK64)) & _MASK64
        return _rotr64(xored, rot)

    def random(self) -> float:
        """Uniform float in [0, 1) using the top 53 bits (IEEE-754 exact)."""
        return (self.next_u64() >> 11) * (1.0 / (1 << 53))

    def randint(self, lo: int, hi: int) -> int:
        """Uniform integer in [lo, hi] inclusive, via Lemire-style rejection.

        Unbiased: rejects the small leftover band so every value in the range
        is equally likely. Determinism is preserved (rejections consume draws).
        """
        if hi < lo:
            raise ValueError("randint requires lo <= hi")
        span = hi - lo + 1
        if span == 1:
            return lo
        # Rejection threshold: discard the low band so [threshold, 2**64) is an
        # exact multiple of span, removing modulo bias. threshold = 2**64 % span.
        threshold = (_MASK64 + 1 - span) % span
        while True:
            r = self.next_u64()
            if r >= threshold:
                return lo + (r % span)

    def choice_index(self, cumulative: list[float]) -> int:
        """Index into a precomputed cumulative-weight table (last == 1.0)."""
        u = self.random()
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if u < cumulative[mid]:
                hi = mid
            else:
                lo = mid + 1
        return lo


def _rotr64(value: int, rot: int) -> int:
    rot &= 63
    if rot == 0:
        return value & _MASK64
    return ((value >> rot) | (value << (64 - rot))) & _MASK64


# Field separator for `stream_for`'s label digest. Chosen outside the character
# set every caller passes (run ids, cell ids, stage names, decimal integers), so
# ("a", "bc") and ("ab", "c") cannot digest to the same stream.
_LABEL_SEP = b"\x1f"


def stream_for(seed: int, *labels: str) -> int:
    """A 128-bit odd `inc` selecting an independent PCG64 stream for `labels`.

    `PCG64.__init__` has always taken this argument and nothing ever passed it,
    so every targets file in round 1 came off ONE stream at position zero --
    which is why the warmup attack, the head-of-window discard and the measured
    attack all replayed the same prefix of the same file (R1-A7). A stream per
    (run, cell, stage, rate, block, phase) is what makes those three sequences
    independent while leaving them reproducible from the ledger.

    The seed is digested WITH the labels as well as being passed to the state,
    so two runs at different seeds differ by stream and not merely by position
    in one stream.

    Odd is a correctness requirement, not a convention: an even increment
    collapses the LCG's period. The low bit is forced set, exactly as
    `PCG64.__init__` does for the published default.
    """
    h = hashlib.blake2b(
        _LABEL_SEP.join([str(seed).encode("utf-8"),
                         *(label.encode("utf-8") for label in labels)]),
        digest_size=16,
    )
    return int.from_bytes(h.digest(), "big") | 1
