"""ReActWorker.run(TaskEnvelope) -> TaskResult; optional event sink mirrors runtime events."""
import asyncio
from uuid import uuid4

from core.orchestration.permissions import build_scoped_tools
from core.orchestration.validation import terminal, validate_result, with_runtime_status
from core.orchestration.worker_runtime import build_runtime


class ReActWorker:
    """One-shot host admission. Scheduler owns construction and the reservation lifecycle."""

    def __init__(self, envelope, *, provider, parent_tools, store, allowed_refs,
                 effective, revalidate, settle, accept=None, emit=None):
        self._envelope = envelope
        self._provider = provider
        self._parent_tools = parent_tools
        self._store = store
        self._allowed_refs = allowed_refs
        self._effective = effective
        self._revalidate = revalidate
        self._settle = settle
        self._accept = accept
        self._emit = emit
        self._used = False
        self.identity = {name: uuid4().hex for name in ('agent_id', 'session_id', 'run_id')}

    def cancel_before_start(self, envelope):
        """Supervisor owns an admitted coroutine that was cancelled before its first turn."""
        if self._used or envelope != self._envelope:
            return None
        self._used = True
        self._settle(None, unstarted=True)
        return terminal(envelope, 'cancelled', 'worker_cancelled_before_start')

    async def run(self, envelope):
        if self._used or envelope != self._envelope:
            raise ValueError('worker_admission_mismatch_or_consumed')
        self._used = True
        runtime_result = None
        tools = None
        chunks = []
        try:
            self._revalidate()
            tools = build_scoped_tools(self._parent_tools, self._effective, self._revalidate)
            context = self._store.render(envelope, self._allowed_refs, self._effective.memory_scope)
            runtime, request = build_runtime(envelope, self._provider, context, tools,
                                             self.identity, self._revalidate)

            async def sink(event):
                if hasattr(self._revalidate, 'quota'):
                    self._revalidate.quota.event(event)
                if event.type == 'content.delta':
                    chunks.append(event.payload.get('text', ''))
                if self._emit is not None:
                    await self._emit(event)

            runtime_result = await runtime.run(request, sink)
            pending = bool(tools.operations.active)
            await tools.operations.close()
            self._revalidate()
            usage = {'status': runtime_result.usage_status, 'tokens': runtime_result.usage_totals,
                     'calls': runtime_result.model_calls, 'cost': runtime_result.cost_summary,
                     'budget': runtime_result.budget_summary}
            if pending:
                return terminal(envelope, 'incomplete', 'worker_operations_required_drain',
                                usage, ''.join(chunks))
            result = validate_result(''.join(chunks), envelope, self._effective, self._store,
                                     self._allowed_refs, self._accept, usage)
            from core.budget_topup.worker import annotate
            return annotate(with_runtime_status(result, runtime_result), runtime_result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return terminal(envelope, 'failed', f'worker_host_failure:{type(exc).__name__}',
                            summary=''.join(chunks))
        finally:
            try:
                if tools is not None:
                    await tools.operations.close()
            finally:
                self._settle(runtime_result)
