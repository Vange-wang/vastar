"""B34 read-only sessions, durable external-evidence barrier and artifact freshness."""
import asyncio
import threading

from core.artifacts import ArtifactGone
from core.harness.network_policy import NetworkError
from core.tools.browser_adapter import PlaywrightAdapter
from core.tools.browser_network import BrowserBroker
from core.tools.browser_protocol import BrowserFailure
from core.tools.browser_results import publish
from core.tools.browser_session import BrowserSession, validate_args
from core.tools.network_tools import Cancellation
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain
from memory.repositories.tasks import encode


class BrowserService:
    def __init__(self, context, config, adapter=None):
        self.context, self.config = context, dict(config)
        self.adapter = adapter or PlaywrightAdapter(context, config)
        self.sessions, self.lock = {}, threading.Lock()
        context.artifacts.browser_validator = self.validate_artifact

    def capability(self):
        return {**self.adapter.capabilities(), 'enabled': self.config.get('enabled', False),
                'network': 'anonymous B29 broker; per-URL readonly grants; no direct browser network',
                'isolation': self.context.execution_sandbox.preflight()}

    async def execute(self, spec, args, session_id, run_id, operation_id, cancel, emitter):
        if not self.lock.acquire(blocking=False):
            return ToolOutcome(spec.name, 'rejected', 'browser busy', origin='browser_busy')
        control = None
        stop = threading.Event()
        try:
            validate_args(spec.name, args)
            if spec.name == 'browser_capabilities':
                return ToolOutcome(spec.name, 'executed', result=encode(self.capability()))
            if not self.config.get('enabled', False):
                raise BrowserFailure('browser_disabled')
            owner = {'project_id': self.context.artifacts._owner, 'session_id': session_id,
                     'task_id': self.context.task_service.current(session_id)['task_id']}
            self.context.process_service.require_task(owner['task_id'], run_id)
            token = Cancellation(stop, cancel, lambda: self.context.registry.is_enabled(spec.name)
                                 and self.config.get('enabled', False))
            if token.is_set():
                raise BrowserFailure('cancelled')
            if self.adapter.capabilities()['status'] != 'available':
                return ToolOutcome(spec.name, 'failed', '浏览器依赖不可用；未读取页面',
                    result=encode(self.capability()), origin='browser_unavailable', redact_result_preview=True)
            network = self.context.network_service.search.network
            broker = BrowserBroker(network, self.config.get('readonly_urls', []), token, {})
            if 'url' in args:
                broker.check(args['url'])
            session = self.sessions.get(args.get('browser_session_id'))
            self.sessions = {key: value for key, value in self.sessions.items()
                             if not getattr(value.connection, 'entry', {}).get('done', threading.Event()).is_set()}
            if args.get('browser_session_id') and session is None:
                raise BrowserFailure('browser_session_closed')
            if session:
                session.require_owner(owner)
                if args.get('tab_id'):
                    session.tab(args['tab_id'])
            elif len(self.sessions) >= 2:
                raise BrowserFailure('browser_session_limit')
            control = self.context.network_records.begin(spec, args, session_id, run_id, operation_id, network.policy)
            if isinstance(control, ToolOutcome):
                return ToolOutcome(spec.name, 'rejected', '旧浏览器操作不可重放；请重新读取当前页面', origin='browser_replay')
            broker.usage = control.facts['network']
            broker.usage.update(live=network.transport.live, actual_cost=None if network.transport.live else 0)
            if session is None:
                connection = await self.adapter.start(spec, owner['task_id'], session_id, run_id, cancel, emitter)
                session = BrowserSession(owner, connection)
                self.sessions[session.browser_session_id] = session
            outcome = await self._observe(spec, args, session, broker, token, stop)
        except asyncio.CancelledError:
            stop.set()
            outcome = ToolOutcome(spec.name, 'cancelled', 'browser cancelled', origin='cancelled')
            raise
        except (BrowserFailure, NetworkError) as exc:
            outcome = ToolOutcome(spec.name, 'cancelled' if exc.code == 'cancelled' else 'failed',
                '浏览器未完成：' + exc.code, origin=exc.code, redact_result_preview=True,
                result=encode({'status': 'unavailable' if 'unavailable' in exc.code else 'incomplete',
                               'reason_code': exc.code, 'fallback_used': False}))
        except Exception:
            outcome = ToolOutcome(spec.name, 'failed', '浏览器不可用；未生成页面内容', origin='browser_unavailable',
                                  result=encode({'status': 'unavailable', 'reason_code': 'isolated_start_failed',
                                                 'fallback_used': False}), redact_result_preview=True)
        finally:
            try:
                if control is not None and not isinstance(control, ToolOutcome):
                    outcome = self.context.network_records.finish(control, outcome)
            finally:
                self.lock.release()
        return outcome

    async def _observe(self, spec, args, session, broker, token, stop):
        command = {k: v for k, v in args.items() if k != 'browser_session_id'}
        command['op'] = 'navigate' if spec.name == 'browser_navigate' else 'capture'
        worker = asyncio.get_running_loop().run_in_executor(None, session.connection.exchange, command, broker, token)
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            await drain(worker)
            raise
        if token.is_set():
            raise BrowserFailure('cancelled')
        payload = publish(self.context.artifacts, session, result, broker)
        return self.context.network_records.result(spec.name, payload)

    def validate_artifact(self, meta):
        try:
            _, session_id, tab_id, revision = meta['source_ref'].split(':')
            session = self.sessions[session_id]
            session.tab(tab_id)
            if self.lock.locked():
                raise BrowserFailure('browser_busy')
            result = session.connection.exchange({'op': 'version', 'tab_id': tab_id}, None,
                                                  threading.Event(), timeout=10)
            if result['page_revision'] != revision:
                raise BrowserFailure('stale_page_revision')
        except Exception as exc:
            raise ArtifactGone('浏览器页面版本已失效或不可核实；请重新读取页面') from exc

    def artifact_owner(self, args, session_id):
        try:
            artifact_id, version, _ = self.context.artifacts._parse_ref(args.get('artifact_ref', ''))
            meta = self.context.artifacts._db.get_artifact(artifact_id, version)
            if not meta or not meta.get('source_ref', '').startswith('browser:'):
                return None
            browser_id = meta['source_ref'].split(':')[1]
            browser = self.sessions[browser_id]
            task = self.context.task_service.current(session_id)
            browser.require_owner({'project_id': self.context.artifacts._owner,
                                   'session_id': session_id, 'task_id': task['task_id']})
        except Exception:
            return ToolOutcome('read_artifact', 'rejected', '浏览器产物不属于当前会话/任务或已失效',
                               origin='browser_scope_denied')
        return None
