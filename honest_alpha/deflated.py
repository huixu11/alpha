"""The Deflated Sharpe Ratio of Bailey and Lopez de Prado (2014), on daily Sharpe ratios.

The best of N tries looks good even when every try is worthless. The DSR is
the probability that a trial's true Sharpe is above the Sharpe the best of N
worthless tries would show by luck, given how many returns it was measured on
and how fat-tailed they were.
"""
from __future__ import annotations

import math
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329
_Z = NormalDist()


def expected_max_z(n: int) -> float:
    """E[max of n independent standard normals], the paper's approximation; 0 for a single try."""
    if n <= 1:
        return 0.0
    return (1 - EULER_GAMMA) * _Z.inv_cdf(1 - 1 / n) + EULER_GAMMA * _Z.inv_cdf(1 - 1 / (n * math.e))


def luck_bar(n: int, trial_srs: list[float]) -> float:
    """The daily Sharpe the best of n worthless tries is expected to reach, scaled by the tries' spread."""
    if n <= 1 or len(trial_srs) < 2:
        return 0.0
    mean = sum(trial_srs) / len(trial_srs)
    var = sum((s - mean) ** 2 for s in trial_srs) / (len(trial_srs) - 1)
    return math.sqrt(var) * expected_max_z(n)


def psr(sr: float, sr0: float, n_obs: int, skew: float, kurt: float) -> float:
    """P(true daily Sharpe > sr0) given the estimate sr from n_obs returns; kurt is raw (3 for a normal)."""
    var = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n_obs < 2 or var <= 0:
        return 0.0
    return _Z.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(var))
