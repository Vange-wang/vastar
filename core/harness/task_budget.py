"""A30: configurable draft account proposal; only explicit acceptance grants it."""
import math

FIELDS = ('max_calls', 'max_estimated_tokens', 'max_cost_yuan', 'max_active_seconds')


def load_proposal(value):
    if value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('tools.task_budget must specify all four dimensions')
    for key, number in value.items():
        numeric = type(number) in (int, float) and math.isfinite(number) and number > 0
        if not numeric or key in FIELDS[:2] and type(number) is not int:
            raise ValueError('tools.task_budget invalid ' + key)
    return dict(value)
