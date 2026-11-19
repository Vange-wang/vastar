"""A42: one observable, budgeted final-answer recovery; never replay tools."""
from dataclasses import replace

from core.errors import ErrorCategory
from core.harness.preflight import estimate_components
from core.run_state import AttemptOutcome, RunState

NOTICE = '本次预算被思考占满，未能产出正文。请提高本次正文恢复预算后重试；本轮未完成。'


def exhausted(outcome):
    return outcome.finish_reason == 'length' and outcome.has_reasoning and not outcome.has_content


def recovery_request(s, spec, ctx, policy):
    if (policy is None or not policy._enabled or not policy.empty_content_enabled or
            any(a.endswith('-ec1') for a in s.result.attempt_ids)):
        return None
    if (spec.name, spec.model) not in {('deepseek', 'deepseek-flash'), ('glm', 'glm-5.3-flash')}:
        return None
    request = policy._build_request(s, spec, ctx, '')
    # No empty assistant prefix; no reasoning replay or tools on this final-answer call.
    request.messages = request.messages[:-1]
    params = dict(request.params)
    original = int(params.get('max_tokens', 0))
    if original <= 0:
        return None
    if spec.name == 'deepseek':
        params.update(thinking={'type': 'disabled'}, max_tokens=min(original, policy.empty_content_max_tokens))
    else:
        if original >= policy.empty_content_max_tokens:
            return None
        params.update(thinking={'type': 'enabled'}, max_tokens=policy.empty_content_max_tokens)
    request = replace(request, params=params, meta={**request.meta, 'empty_content_recovery': True})
    estimate = estimate_components(s, request)
    if estimate['input_tokens'] + params['max_tokens'] > policy.empty_content_total_tokens:
        s.warnings.append('空正文恢复中止：本次输入与输出预留超出恢复总 token 上限。')
        return None
    note = policy._check_prefix_budget(s, spec, request)
    if note:
        s.warnings.append(note)
        return None
    return request


async def recover_empty(s, spec, outcome, ctx, runner, policy):
    if not exhausted(outcome):
        return False
    request = recovery_request(s, spec, ctx, policy)
    if request is not None and not s.request.cancel_event.is_set():
        s.warnings.append('思考耗尽预算且正文为空：仅补发一次无工具终答请求；参数与用量单独留痕。')
        await s.sm.transition(RunState.CONTINUING, 'empty_content_recovery')
        attempt_id = f'{s.request.run_id}-ec1'
        recovered = AttemptOutcome()
        err = await runner.stream_attempt(s, spec, request, attempt_id,
                                         len(s.result.attempt_ids) + 1, recovered,
                                         extra_payload={'kind': 'empty_content_recovery', 'max_retries': 1,
                                                        'total_token_cap': policy.empty_content_total_tokens})
        if err is not None:
            if err.category == ErrorCategory.CANCELLED:
                await runner.cancel_path(s)
                return True
            await runner.record_error(s, err, attempt_id)
            if recovered.has_content:
                await runner.terminate(s, RunState.INCOMPLETE, 'empty_content_recovery_failed')
                return True
        elif recovered.has_content:
            # This recovery has its own finite budget; do not re-enter unlimited continuation.
            complete = recovered.finish_reason == 'stop' and not recovered.tool_calls
            await runner.terminate(s, RunState.COMPLETED if complete else RunState.INCOMPLETE,
                                   'stop' if complete else 'output_limit')
            return True
    if s.request.cancel_event.is_set():
        await runner.cancel_path(s)
        return True
    s.result.partial = True
    await runner.terminate(s, RunState.INCOMPLETE, 'reasoning_budget_exhausted')
    return True
