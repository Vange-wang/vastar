"""Separate browser route preserves existing executor and dispatcher size margins."""
from core.tools.browser_tools import NAMES


async def dispatch(context, spec, args, session_id, run_id, operation_id, cancel, emitter):
    if spec.name == 'browser_workflow':
        from core.skills.browser_workflow import execute
        return await execute(context, args, session_id, run_id, cancel, emitter)
    if spec.name in {'browser_click', 'browser_fill'}:
        return await context.browser_service.actions.execute(spec, args, session_id, run_id, operation_id, cancel, emitter)
    if spec.name == 'browser_reconcile':
        from core.tools.browser_reconcile import reconcile
        return await reconcile(context, args, session_id, run_id, cancel)
    if spec.name == 'read_artifact' and context.browser_service is not None:
        return context.browser_service.artifact_owner(args, session_id)
    if spec.name in NAMES:
        return await context.browser_service.execute(spec, args, session_id, run_id, operation_id, cancel, emitter)
    return None
