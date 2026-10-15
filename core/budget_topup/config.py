"""Vastar runtime component."""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TopupConfig:
    enabled: bool = True
    ratio: float = 0.5
    legacy_max_estimated_tokens: int = 100000
    legacy_max_cost_yuan: float = 10
    legacy_max_active_seconds: float = 7200

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('budget_topup_enabled_invalid')
        for key in ('ratio', 'legacy_max_cost_yuan', 'legacy_max_active_seconds'):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('budget_topup_' + key + '_invalid')
        if self.ratio > 1 or type(self.legacy_max_estimated_tokens) is not int or self.legacy_max_estimated_tokens < 1:
            raise ValueError('budget_topup_limits_invalid')


def load_topup(raw):
    block = raw.get('budget_topup', {})
    if type(block) is not dict or set(block) - set(TopupConfig.__dataclass_fields__):
        raise ValueError('budget_topup_config_invalid')
    return TopupConfig(**block)
