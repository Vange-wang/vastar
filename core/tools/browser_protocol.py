"""Private framed pipe protocol; raw browser output is never a public process artifact."""
import json
import threading
import time
import uuid

from core.harness.network_policy import NetworkError
from core.tools.report_transport import ReportOutput


class BrowserOutput(ReportOutput):
    def read(self, cursor=0, max_bytes=16384):
        return {'fragments': [], 'next_cursor': 0, 'total_bytes': 0,
                'private': True, 'reason': 'browser transport withheld; use validated browser tools'}


class BrowserFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BrowserConnection:
    def __init__(self, service, info):
        self.service, self.info = service, info
        self.entry = service.lookup(info['job_id'], info['owner'], info['started_at'])
        self.lock, self.sequence, self.cursor, self.buffer = threading.Lock(), 0, 0, b''
        self.work = self.entry['profile'].work
        self.scratch_check, self.ready = 0, False

    def _scratch_budget(self):
        if time.monotonic() - self.scratch_check < 1:
            return
        self.scratch_check = time.monotonic()
        pending, size, count = [self.entry['profile'].root / 'Temp' / 'browser'], 0, 0
        while pending:
            item = pending.pop()
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue  # Chromium removes transient cache entries concurrently.
            if getattr(info, 'st_file_attributes', 0) & 0x400:
                raise BrowserFailure('scratch_reparse_denied')
            count += 1
            size += info.st_size if item.is_file() else 0
            if size > 128 * 1024 * 1024 or count > 4096:
                self.close()
                raise BrowserFailure('scratch_budget_exceeded')
            if item.is_dir():
                pending.extend(item.iterdir())

    def _write(self, filename, data):
        target = self.work / filename
        temporary = target.with_suffix('.pending-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
        temporary.replace(target)

    def _events(self):
        with self.entry['lock']:
            output = self.entry['output'].data
            data = bytes(output[self.cursor:])
            self.cursor = len(output)
        self.buffer += data
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                raise BrowserFailure('invalid_browser_protocol') from None

    def exchange(self, command, broker, cancel, timeout=20):
        if not self.lock.acquire(blocking=False):
            raise BrowserFailure('browser_busy')
        try:
            if command.get('op') == 'navigate':
                command = {**command, 'url': broker.prepare_navigation(command['url'])}
            self.sequence += 1
            self._write(f'command-{self.sequence}.json', {**command, 'id': self.sequence})
            deadline = time.monotonic() + timeout
            while True:
                if cancel.is_set():
                    self.close()
                    raise BrowserFailure('cancelled')
                self._scratch_budget()
                for event in self._events():
                    if event.get('type') == 'network':
                        key = event.get('key', '')
                        if not isinstance(key, str) or not key.isdigit() or len(key) > 9:
                            raise BrowserFailure('invalid_browser_protocol')
                        response = broker(event) if broker else {'error': 'network_outside_command'}
                        self._write('network-' + key + '.json', response)
                    elif event.get('type') == 'result' and event.get('id') == self.sequence:
                        if event.get('error'):
                            raise BrowserFailure(event['error'])
                        return event['value']
                    elif event.get('type') == 'commit_request' and event.get('id') == self.sequence:
                        answer = broker.authorize_action() if command.get('op') == 'action' else {'approved': False}
                        self._write(f'action-gate-{self.sequence}.json', answer)
                    elif event.get('type') == 'action_proof' and event.get('id') == self.sequence:
                        if command.get('op') == 'action':
                            broker.action_proof(event['proof'])
                    elif event.get('type') == 'fatal':
                        raise BrowserFailure('isolated_browser_unavailable')
                    elif event.get('type') == 'ready':
                        from core.tools.browser_adapter import ENGINE_VERSION
                        if event.get('engine_version') != ENGINE_VERSION:
                            self.close()
                            raise BrowserFailure('engine_version_mismatch')
                        self.ready = True
                if self.entry['done'].is_set():
                    raise BrowserFailure('browser_closed' if self.ready else 'isolated_browser_unavailable')
                if time.monotonic() >= deadline:
                    self.close()
                    raise BrowserFailure('timeout' if self.ready else 'isolated_browser_unavailable')
                time.sleep(.01)
        except NetworkError:
            if cancel.is_set():
                self.close()
                raise BrowserFailure('cancelled') from None
            raise
        finally:
            self.lock.release()

    def close(self):
        self.service.terminate(self.info['job_id'], owner=self.info['owner'],
                               started_at=self.info['started_at'], reason='cancelled')
