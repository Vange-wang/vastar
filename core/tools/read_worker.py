"""Private JSON pipe for registered filesystem reads; no model-supplied Python or shell."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from core.tools import permissions
from core.tools.operation_receipt import _contract_from_facts, _friendly_failure_reason
from core.tools.operation_resources import file_facts

BUILTINS = ('search_text', 'read_file', 'list_dir', 'stat_path', 'glob_files')


def skip(path):
    return ('.git' in path.parts or '.venv' in path.parts or
            permissions.check_credential_path(path) is not None)


def perform(payload):
    if payload['tool'] not in BUILTINS:
        raise ValueError('not a registered filesystem read')
    path, root = Path(payload['path']), Path(payload['root'])
    # Parent owns cancellation, quota and permissions; this child can only read.
    control = SimpleNamespace(facts={}, checkpoint=lambda stage: None)
    facts = {'before': file_facts([path])}
    try:
        module = importlib.import_module('core.tools.' + payload['tool'])
        result = module.run(payload['args'], resolved_path=path, project_root=root, skip=lambda p: skip(p) or (p != path and permissions.is_env_path(p) and
                            permissions.location_of(p, root) == permissions.OUT_ROOT),
                            op_context={'control': control})
        receipt = {'status': 'executed', 'result': result, 'contract': _contract_from_facts(control.facts)}
    except Exception as exc:
        receipt = {'status': 'failed', 'reason': _friendly_failure_reason(exc, path),
                   'origin': getattr(exc, 'origin', 'tool_runtime')}
    facts.update(control.facts)
    facts['after'] = file_facts([path])
    return {**receipt, 'facts': facts}


if __name__ == '__main__':
    for line in sys.stdin.buffer:
        payload = json.loads(line.decode('utf-8'))
        sys.stdout.buffer.write(json.dumps(perform(payload), ensure_ascii=False).encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()
