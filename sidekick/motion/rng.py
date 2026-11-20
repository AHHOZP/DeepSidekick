"""Small deterministic PRNG used by every stochastic layer.

The engine must be reproducible: the same seed and the same frame times must
produce the same motion.  ``random.Random`` would also work, but a tiny
xorshift generator keeps the state explicit and makes tests independent of
Python version details.
"""
from __future__ import annotations

import math


class Rng:
    """xorshift64* with the usual convenience samplers."""

    __slots__ = ("_state",)

    def __init__(self, seed: int = 1) -> None:
        state = int(seed) & 0xFFFFFFFFFFFFFFFF
        self._state = state or 0x9E3779B97F4A7C15

    def next_u64(self) -> int:
        x = self._state
        x ^= (x >> 12) & 0xFFFFFFFFFFFFFFFF
        x ^= (x << 25) & 0xFFFFFFFFFFFFFFFF
        x ^= (x >> 27) & 0xFFFFFFFFFFFFFFFF
        self._state = x & 0xFFFFFFFFFFFFFFFF
        return (self._state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def random(self) -> float:
        """Uniform float in ``[0, 1)``."""
        return (self.next_u64() >> 11) / float(1 << 53)

    def uniform(self, low: float, high: float) -> float:
        return float(low) + (float(high) - float(low)) * self.random()

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        # Box-Muller.  ``1 - random`` avoids log(0).
        u1 = max(1e-12, 1.0 - self.random())
        u2 = self.random()
        return float(mu) + float(sigma) * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def expovariate(self, rate: float) -> float:
        """Exponential inter-arrival time for a Poisson process."""
        rate = max(1e-9, float(rate))
        return -math.log(max(1e-12, 1.0 - self.random())) / rate

    def choice(self, items):
        seq = list(items)
        if not seq:
            raise ValueError("choice() of an empty sequence")
        return seq[min(len(seq) - 1, int(self.random() * len(seq)))]

    def chance(self, probability: float) -> bool:
        return self.random() < float(probability)
