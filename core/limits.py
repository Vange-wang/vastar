"""Explicit null means no local ceiling; zero retains its finite meaning."""
import math


def optional_limit(value, *, integer=False):
    if value is None:
        return None
    if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
            or integer and type(value) is not int):
        raise ValueError('limit must be null or a non-negative finite number')
    return int(value) if integer else float(value)


def cap_timeout(value, ceiling):
    value = optional_limit(value)
    return ceiling if value is None else min(value, ceiling)
