"""Owned filesystem-read processes; reuse in one context and kill/reap on cancellation."""
import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

from core.tools.operation_receipt import ToolOutcome
from core.tools.read_worker import BUILTINS

REAP_SECONDS = 1.0


def supports(spec):
    return (spec.name in BUILTINS and spec.effect == 'read' and
            getattr(spec.run, '__module__', '') == 'core.tools.' + spec.name and
            getattr(spec.run, '__name__', '') == 'run' and
            spec.run is importlib.import_module('core.tools.' + spec.name).run)


def command():
    return [sys.executable, '-S', '-X', 'utf8', '-m', 'core.tools.read_worker']


def stop_sync(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=REAP_SECONDS)


class ReadWorkers:
    def __init__(self):
        self.idle = []
        self.owned = set()

    async def acquire(self):
        while self.idle:
            process = self.idle.pop()
            if process.poll() is None:
                return process
            self.discard(process)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        process = await asyncio.to_thread(subprocess.Popen, command(),
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **options)
        self.owned.add(process)
        return process

    def exchange(self, process, payload):
        process.stdin.write(payload + b'\n')
        process.stdin.flush()
        return process.stdout.readline()

    async def release(self, process):
        if process.poll() is None and not self.idle:
            self.idle.append(process)
        else:
            await asyncio.to_thread(stop_sync, process)
            self.discard(process)

    def discard(self, process):
        self.owned.discard(process)
        if process in self.idle:
            self.idle.remove(process)
        process.stdin.close()
        process.stdout.close()

    def close_sync(self):
        for process in list(self.owned):
            stop_sync(process)
            self.discard(process)


async def run_isolated(spec, args, resolved, project_root, args_sum, control):
    control.checkpoint('read_spawn')
    control.started = True
    control.on_started()
    pool = control.read_workers
    process = await pool.acquire()
    payload = json.dumps({'tool': spec.name, 'args': args, 'path': str(resolved),
                          'root': str(project_root)}, ensure_ascii=False).encode('utf-8')
    control.facts['read_worker_pid'] = process.pid
    exchange = asyncio.create_task(asyncio.to_thread(pool.exchange, process, payload))
    cancelled = asyncio.create_task(control.async_cancel.wait())
    reusable = False
    try:
        done, _ = await asyncio.wait({exchange, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done or control.cancel_token.is_set():
            await asyncio.to_thread(stop_sync, process)
            await asyncio.gather(exchange, return_exceptions=True)
            control.facts.update(read_worker_exit=process.returncode, read_worker_stopped=True)
            timeout = '超时' in control.reason
            return ToolOutcome(spec.name, 'timeout' if timeout else 'cancelled',
                               control.reason + '；只读执行体已终止；无可用结果',
                               origin='timeout' if timeout else 'user_cancel', args_summary=args_sum)
        stdout = await exchange
        if process.poll() is not None:
            raise RuntimeError('read worker exited without a valid receipt')
        receipt = json.loads(stdout.decode('utf-8'))
        control.facts.update(receipt['facts'], read_worker_idle=True, read_worker_stopped=False)
        outcome = ToolOutcome(spec.name, receipt['status'], receipt.get('reason', ''),
                              result=receipt.get('result', ''), origin=receipt.get('origin', ''),
                              args_summary=args_sum, contract=receipt.get('contract', {}))
        reusable = True
        return outcome
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        if reusable:
            await pool.release(process)
        else:
            await asyncio.to_thread(stop_sync, process)
            await asyncio.gather(exchange, return_exceptions=True)
            pool.discard(process)
