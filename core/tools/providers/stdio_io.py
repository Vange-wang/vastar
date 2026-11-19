"""Owned stdio pipes: bounded frames, deadlines, no shell or ambient credentials."""
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from core.tools.providers.contract import require
from core.tools.providers.protocol import envelope


def environment():
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}
    return {**{k: v for k, v in os.environ.items() if k.upper() in allowed},
            'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}


def validate(config):
    argv = config.get('command')
    require(isinstance(argv, list) and 0 < len(argv) <= 32
            and all(isinstance(s, str) and '\x00' not in s and len(s) <= 8192 for s in argv),
            'provider_stdio_command_invalid')
    executable = Path(argv[0])
    require(executable.is_absolute() and executable.is_file()
            and executable.suffix.lower() not in {'.bat', '.cmd', '.ps1'}, 'provider_stdio_executable_invalid')
    for key, default in [('startup_timeout_seconds', 10), ('timeout_seconds', 20)]:
        value = config.get(key, default)
        require(type(value) in (int, float) and 0 < value <= 60, 'provider_stdio_timeout_invalid')
    return argv


class Channel:
    def __init__(self, config, cancel, run_id):
        self.config, self.cancel, self.run_id = config, cancel, run_id
        self.messages, self.fault = queue.Queue(maxsize=8), None
        self.stderr_bytes, self.stderr_limit = 0, 4096
        self.threads, self.process = [], None
        self.startup_deadline = time.monotonic() + config.get('startup_timeout_seconds', 10)
        self.process = subprocess.Popen(validate(config), shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment(),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            for target in (self._stdout, self._stderr):
                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                self.threads.append(worker)
            self.check(self.startup_deadline)
        except BaseException:
            self.close()
            raise

    def _stdout(self):
        try:
            while data := self.process.stdout.readline(1048577):
                if len(data) > 1048576 or not data.endswith(b'\n'):
                    self.fault = 'provider_stdio_frame_invalid'
                    return
                try:
                    self.messages.put_nowait(data)
                except queue.Full:
                    self.fault = 'provider_stdio_queue_overflow'
        except (OSError, ValueError):
            self.fault = self.fault or 'provider_stdio_disconnected'

    def _stderr(self):
        try:
            while data := self.process.stderr.read(4096):
                self.stderr_bytes += len(data)  # Do not retain untrusted stderr content or credentials.
        except (OSError, ValueError):
            pass

    def check(self, deadline):
        require(not self.cancel.is_set(), 'provider_stdio_cancelled')
        require(time.monotonic() < deadline, 'provider_stdio_timeout')
        require(not self.fault, self.fault or 'provider_stdio_disconnected')
        require(self.process.poll() is None, 'provider_stdio_exited')

    def rpc(self, method, params, usage, *, notification=False, issued=lambda: None):
        deadline = time.monotonic() + self.config.get('timeout_seconds', 20)
        if method == 'initialize':
            deadline = min(deadline, self.startup_deadline)
        self.check(deadline)
        require(self.messages.empty(), 'provider_duplicate_or_unexpected_reply')
        require(usage.get('requests', 0) < 16, 'provider_budget_exceeded')
        rid = uuid.uuid4().hex
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = rid
        payload = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        errors = []
        def send():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(type(exc).__name__)
        issued()
        usage['requests'] = usage.get('requests', 0) + 1
        sender = threading.Thread(target=send, daemon=True)
        self.threads.append(sender)
        sender.start()
        while sender.is_alive():
            self.check(deadline)
            sender.join(.02)
        require(not errors, 'provider_stdio_disconnected')
        if notification:
            return {}
        while True:
            self.check(deadline)
            try:
                data = self.messages.get(timeout=.02)
                usage['bytes'] = usage.get('bytes', 0) + len(data)
                require(usage['bytes'] <= 1048576, 'provider_result_too_large')
                return envelope(json.loads(data), rid)
            except queue.Empty:
                continue

    def close(self, *, verify=False):
        process = self.process
        if process is None:
            return
        # Terminate only this Popen object, never a name/PID search or a process tree.
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        for worker in self.threads:
            worker.join(timeout=.5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        if verify:
            require(self.messages.empty() and not self.fault, 'provider_duplicate_or_unexpected_reply')

    def diagnostic(self):
        return {'pid': self.process.pid, 'run_id': self.run_id, 'exit_code': self.process.poll(),
                'stderr': '[REDACTED]', 'stderr_bytes': min(self.stderr_bytes, self.stderr_limit),
                'stderr_truncated': self.stderr_bytes > self.stderr_limit}
