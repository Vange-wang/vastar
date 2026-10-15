"""Bounded explicit selection only; never scans/imports collab or a project tree."""
import hashlib
import json
from pathlib import Path

from core.tools.permissions import PathRejected, is_env_path, resolve_read_path
from memory.knowledge_codec import STATES, encode

MAX_SOURCE_BYTES = 65536


def check_project(memory, payload):
    if payload['project_id'] != memory.project_id or payload['scope'] != 'project:' + memory.project_id:
        raise ValueError('knowledge_scope_denied')


class KnowledgeSources:
    def __init__(self, memory, project_root, cwd=None):
        self.memory = memory
        self.root = Path(project_root).resolve()
        self.cwd = Path(cwd or project_root).resolve()

    def file(self, path, *, stored=False):
        resolved = resolve_read_path(self.root if stored else self.cwd, path)
        if is_env_path(resolved):
            raise ValueError('敏感配置不能作为归档材料；普通read_file策略不变')
        with resolved.open('rb') as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('knowledge_source_too_large：请用户选取不超过64KiB的材料')
        text = raw.decode('utf-8-sig')
        location = resolved.relative_to(self.root).as_posix() if resolved.is_relative_to(self.root) else str(resolved)
        return {'kind': 'file', 'path': location, 'version': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}, text

    def task(self, task_id):
        task = self.memory.store.task_get(task_id)
        if task is None or task['project_id'] != self.memory.project_id:
            raise ValueError('knowledge_scope_denied')
        return {'kind': 'task', 'task_id': task_id, 'version': hashlib.sha256(encode(task).encode()).hexdigest(),
                'revision': task['revision']}, task

    def select(self, selection, *, stored=False):
        if set(selection) - {'path', 'task_id', 'key'} or bool(selection.get('path')) == bool(selection.get('task_id')):
            raise ValueError('显式选择一个path或task_id和一个key；不接受目录导入')
        key = selection.get('key')
        if not isinstance(key, str) or not key:
            raise ValueError('selection_key_required')
        if selection.get('task_id'):
            source, task = self.task(selection['task_id'])
            completed = task['status'] == 'COMPLETED' and bool(task['steps']) and all(s['status'] == 'COMPLETED' and s['evidence_refs'] for s in task['steps'])
            entry = {'key': key, 'text': '已验收任务：' + task['goal'], 'state': 'effective' if completed else 'pending'}
        else:
            source, text = self.file(selection['path'], stored=stored)
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError('knowledge_source_requires_object')
            entries = document.get('entries', [])
            if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
                raise ValueError('selected_material_requires_1_to_32_entries')
            matches = [e for e in entries if isinstance(e, dict) and e.get('key') == key]
            if len(matches) != 1:
                raise ValueError('selected_key_missing_or_duplicate')
            entry = dict(matches[0])
            if document.get('status') != 'approved' and entry.get('state') == 'effective':
                entry['state'] = 'pending'  # a draft header cannot promote its own row
        if entry.get('state') not in STATES or not isinstance(entry.get('text'), str) or not entry['text'].strip():
            raise ValueError('knowledge_state_or_text_invalid')
        source['key'] = key
        return source, entry

    def verify(self, payload):
        check_project(self.memory, payload)
        if payload['state'] != 'effective':
            raise ValueError('knowledge_not_effective')
        source = payload['source']
        selection = {k: source[k] for k in ('path', 'task_id', 'key') if k in source}
        try:
            current, entry = self.select(selection, stored=True)
        except (OSError, UnicodeError, PathRejected, TypeError, KeyError) as exc:
            raise ValueError('knowledge_source_unavailable') from exc
        if current != source or entry['state'] != 'effective' or entry['text'] != payload['conclusion']:
            raise ValueError('knowledge_source_changed')
        scope = entry.get('scope', 'project:' + self.memory.project_id)
        if payload['scope'] != scope or payload['key'] != entry['key']:
            raise ValueError('knowledge_source_scope_mismatch')
        return current, entry

    def open(self, payload):
        self.verify(payload)
        source = payload['source']
        if source['kind'] == 'file':
            current, body = self.file(source['path'], stored=True)
        else:
            current, body = self.task(source['task_id'])
        if {**current, 'key': source['key']} != source:
            raise ValueError('knowledge_source_changed_during_read')
        return {'source': source, 'body': body}
