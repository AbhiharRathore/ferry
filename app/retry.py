import random


def backoff_seconds(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Delay before the next try, after `attempt` failed attempts (attempt >= 1).

    Exponential growth spreads retries out while a receiver recovers; the cap
    keeps the schedule bounded. The random half ("equal jitter") stops many
    deliveries that failed together from all retrying in the same second.
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap, base * 2 ** (attempt - 1))
    r = rng or random
    return ceiling / 2 + r.uniform(0, ceiling / 2)
