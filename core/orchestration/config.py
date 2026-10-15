"""Vastar runtime component."""
import math
from dataclasses import dataclass, field

from core.budget_topup.config import TopupConfig, load_topup


@dataclass(frozen=True)
class OrchestrationConfig:
    enabled: bool = False
    max_workers: int = 2
    max_depth: int = 1
    schema_version: int = 1
    max_cost_yuan: float = 10
    max_active_seconds: float = 7200
    budget_policy_version: int = 1
    budget_topup: TopupConfig = field(default_factory=TopupConfig)

    def __post_init__(self):
        if type(self.budget_topup) is not TopupConfig:
            raise ValueError('budget_topup_config_invalid')
        if type(self.budget_policy_version) is not int or self.budget_policy_version != 1:
            raise ValueError('worker_budget_policy_version_unsupported')
        for key in ('max_cost_yuan', 'max_active_seconds'):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'worker_{key}_invalid')
        if type(self.enabled) is not bool:
            raise ValueError('worker_enabled_must_be_boolean')
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('worker_config_version_unsupported')
        for key, cap in (('max_workers', 2), ('max_depth', 1)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f'worker_{key}_outside_pilot')


def load_orchestration(raw: dict) -> OrchestrationConfig:
    block = raw.get('orchestration', {})
    if type(block) is not dict or set(block) - set(OrchestrationConfig.__dataclass_fields__):
        raise ValueError('worker_config_invalid')
    if 'budget_topup' in block:
        raise ValueError('budget_topup_must_be_top_level')
    return OrchestrationConfig(**block, budget_topup=load_topup(raw))
