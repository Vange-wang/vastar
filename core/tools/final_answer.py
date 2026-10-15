"""Exact tool history and terminal instruction; A30 retires proactive policy pause."""
from dataclasses import dataclass

from core.llm.base import FrameKind

FINAL_INSTRUCTION = (
    '[Vastar 收尾指令] 本轮查证已结束。现在直接给出最终回答，不再发起任何工具调用或新的查证，'
    '不要输出工具调用标记，也不要只说下一步计划。只汇总本轮已经核实的事实；'
    '代码判断附已读取的路径和行号。未读取、无法核实或未完成的部分须明确说明无法完成及原因，'
    '保留已完成部分，不猜测、不伪造结论、不把计划当实现。')


@dataclass(frozen=True)
class FinalAnswerPolicy:
    enabled: bool = False
    reserve_tool_rounds: int = 1
    max_read_only_rounds: int = 6
    min_remaining_calls: int = 1
    remaining_token_ratio: float = 0.15
    min_remaining_seconds: float = 20.0

    @classmethod
    def from_config(cls, values):
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError('tools.final_answer: unknown policy fields or invalid object')
        policy = cls(**values)
        if type(policy.enabled) is not bool:
            raise ValueError('tools.final_answer.enabled must be bool')
        for key in ('reserve_tool_rounds', 'max_read_only_rounds', 'min_remaining_calls'):
            value = getattr(policy, key)
            if type(value) is not int or value < 0:
                raise ValueError(f'tools.final_answer.{key} must be nonnegative integer')
        import math
        for key in ('remaining_token_ratio', 'min_remaining_seconds'):
            value = getattr(policy, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'tools.final_answer.{key} must be finite nonnegative number')
        if policy.remaining_token_ratio > 1:
            raise ValueError('tools.final_answer.remaining_token_ratio must be <= 1')
        return policy


def tool_message(outcome, calls):
    frames = outcome.latest_frames if outcome.latest_frames is not None else outcome.frames
    message = {'role': 'assistant', 'content': ''.join(
        f.get('text', '') for f in frames if f['kind'] == FrameKind.CONTENT) or None,
        'tool_calls': [dict(tc) for tc in calls]}
    reasoning = ''.join(f.get('text', '') for f in frames if f['kind'] == FrameKind.REASONING)
    if reasoning:
        message['reasoning_content'] = reasoning
    return message


def final_messages(messages):
    boundary = next((i for i, m in enumerate(messages) if m.get('role') != 'system'),
                    len(messages))
    return [*messages[:boundary], {'role': 'system', 'content': FINAL_INSTRUCTION},
            *messages[boundary:]]


def record_read_round(s, tools, calls):
    read_only = bool(calls) and all(
        (spec := tools.registry.get((tc.get('function') or {}).get('name', '')))
        is not None and spec.effect == 'read' for tc in calls)
    s.read_only_rounds = s.read_only_rounds + 1 if read_only else 0


def final_reason(s, tools):
    """A30废止主动策略暂停；旧调用接口留空，不能由旧enabled配置恢复。"""
    return ''
