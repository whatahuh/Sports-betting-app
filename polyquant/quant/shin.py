"""Shin's method & Goto fallback for implied probability debiasing."""
from __future__ import annotations

from scipy.optimize import brentq


def shin_implied_probabilities(prices: list[float]) -> list[float]:
    """Solve for Shin's insider parameter z, return fair probabilities summing to 1.0."""
    n = len(prices)
    overround = sum(prices)

    if overround <= 0 or n < 2:
        return [1.0 / n] * n

    # If prices already sum to ~1.0 (efficient market), normalize directly
    if abs(overround - 1.0) < 0.01:
        return [p / overround for p in prices]

    def objective(z):
        total = 0.0
        for p in prices:
            disc = z**2 + 4 * (1 - z) * (p**2) / overround
            if disc < 0:
                return float('inf')
            q = (disc**0.5 - z) / (2 * (1 - z)) if z < 1 else p / overround
            total += q
        return total - 1.0

    try:
        z = brentq(objective, 0.0, 0.999, xtol=1e-12)
    except (ValueError, RuntimeError):
        return [p / overround for p in prices]

    result = []
    for p in prices:
        disc = z**2 + 4 * (1 - z) * (p**2) / overround
        q = (disc**0.5 - z) / (2 * (1 - z))
        result.append(max(0.0, min(1.0, q)))

    total = sum(result)
    return [q / total for q in result] if total > 0 else [1.0 / n] * n


def goto_conversion_heuristic(price: float, overround: float, n: int = 2) -> float:
    """Goto's additive fallback: q_i = p_i - (overround - 1) / n."""
    margin = (overround - 1.0) / n
    return max(0.0, min(1.0, price - margin))


def model_probability(yes_price: float, no_price: float) -> tuple[float, float]:
    """Primary entry point. Returns (true_yes_prob, true_no_prob) using Shin's method."""
    if yes_price is None or no_price is None:
        return 0.5, 0.5
    if yes_price <= 0 or no_price <= 0:
        return 0.5, 0.5
    prices = [yes_price, no_price]
    try:
        probs = shin_implied_probabilities(prices)
        return probs[0], probs[1]
    except Exception:
        overround = yes_price + no_price
        return (
            goto_conversion_heuristic(yes_price, overround),
            goto_conversion_heuristic(no_price, overround),
        )
