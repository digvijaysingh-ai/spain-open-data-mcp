"""Estimate: a value with a low and high bound, and interval arithmetic done once, correctly.

Every engine quantity is an Estimate. Combining two of them pairs the ends that give the
widest honest range:

    a + b   = (a.low + b.low,  a.base + b.base,  a.high + b.high)
    a × b   = (a.low × b.low,  ...,              a.high × b.high)     all values >= 0
    a ÷ b   = (a.low ÷ b.high, a.base ÷ b.base,  a.high ÷ b.low)      the crossed ends

Quantities in the engine are never negative (people, euros, businesses), which keeps the
rules above exact; constructing a negative Estimate raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Estimate:
    low: float
    base: float
    high: float

    def __post_init__(self) -> None:
        values = (self.low, self.base, self.high)
        if any(math.isnan(v) or math.isinf(v) for v in values):
            raise ValueError(f"Estimate values must be finite: {values}")
        if self.low < 0:
            raise ValueError(f"Estimate must not be negative: {values}")
        if not self.low <= self.base <= self.high:
            raise ValueError(f"Estimate must satisfy low <= base <= high: {values}")

    @classmethod
    def exact(cls, value: float) -> Estimate:
        return cls(value, value, value)

    @classmethod
    def around(cls, base: float, rel: float, cap: float = 0.5) -> Estimate:
        """base ± rel × base, with the relative spread capped at `cap`."""
        r = min(abs(rel), cap)
        return cls(max(base * (1 - r), 0.0), base, base * (1 + r))

    def __add__(self, other: Estimate) -> Estimate:
        return Estimate(self.low + other.low, self.base + other.base, self.high + other.high)

    def __mul__(self, other: Estimate | float) -> Estimate:
        o = other if isinstance(other, Estimate) else Estimate.exact(other)
        return Estimate(self.low * o.low, self.base * o.base, self.high * o.high)

    __rmul__ = __mul__

    def __truediv__(self, other: Estimate | float) -> Estimate:
        o = other if isinstance(other, Estimate) else Estimate.exact(other)
        if o.low <= 0:
            raise ZeroDivisionError("dividing by an Estimate whose low end is 0")
        return Estimate(self.low / o.high, self.base / o.base, self.high / o.low)

    def plus(self, value: float) -> Estimate:
        """Add a fixed amount (e.g. the pseudo-business in the score)."""
        return Estimate(self.low + value, self.base + value, self.high + value)

    def clamp_low(self, floor: float) -> Estimate:
        """Raise the low end to `floor`, never above base (e.g. supply is at least what is mapped)."""
        low = min(self.base, max(self.low, floor))
        return Estimate(low, self.base, self.high)

    @property
    def spread(self) -> float:
        """(high - low) / base: how wide the range is relative to the base."""
        return (self.high - self.low) / self.base if self.base else 0.0

    def rounded(self, digits: int = 0) -> Estimate:
        return Estimate(round(self.low, digits), round(self.base, digits), round(self.high, digits))
