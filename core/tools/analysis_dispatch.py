"""Extension routing keeps the shared special dispatcher within its existing size."""
from core.skills.browser_workflow import guard as workflow_guard
from core.skills.code_change import guard as code_guard
from core.skills.maintenance import guard as maintenance_guard
from core.tools.browser_tools import guard as browser_guard
from core.tools.report_tools import guard as report_guard
from core.tools.sheet_tools import guard as sheet_guard


def guard(context, spec):
    return maintenance_guard(context, spec) or code_guard(context, spec) or report_guard(context, spec) or sheet_guard(context, spec) or browser_guard(context, spec) or workflow_guard(context, spec)


async def dispatch(context, spec, args, session_id, run_id, operation_id, cancel, emitter):
    if spec.name == 'code_change':
        from core.skills.code_change import execute
        return await execute(context, args, session_id, run_id, cancel, emitter)
    from core.tools.providers import dispatch as provider_dispatch
    provider = await provider_dispatch(context, spec, args, session_id, run_id, operation_id, cancel, emitter)
    if provider is not None:
        return provider
    if spec.name in {'sheet_capabilities', 'analyze_spreadsheet'}:
        return await context.sheet_service.execute(spec, args, session_id, run_id, operation_id, cancel, emitter)
    from core.tools.browser_dispatch import dispatch as browser_dispatch
    return await browser_dispatch(context, spec, args, session_id, run_id, operation_id, cancel, emitter)
