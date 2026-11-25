"""One user-initiated project inspection with a machine-readable receipt."""
import asyncio
import json
import math
import os
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.events import EventType
from core.tools import permissions
from core.tools.registry import ToolRegistry, build_tools_context

READ_TOOLS = ("read_file", "list_dir", "stat_path", "glob_files", "search_text")
SCHEMA_BYTE_LIMIT = 64 * 1024
TOOL_ISSUES = (EventType.TOOL_REJECTED, EventType.TOOL_FAILED, EventType.TOOL_CANCELLED)
TOOL_PROGRESS = (EventType.TOOL_REQUESTED, EventType.TOOL_EXECUTED, *TOOL_ISSUES)
# Out-of-schema model tool names may be arbitrary text. Use public names only on the event stream.
PUBLIC_TOOL_NAMES = {*READ_TOOLS, "<batch>", "<next_round>", "write_file", "edit_file", "mkdir",
                     "move_file", "delete_path", "start_process", "web_search", "read_web_page"}


def inspection_tools(raw, paths, store):
    from core.artifacts import ArtifactStore
    from core.history_tools import install_history
    context = build_tools_context(raw, paths.workspace or paths.project_root, paths=paths)
    context.artifacts = ArtifactStore(store, context.data_dir, owner=paths.project_id)
    install_history(context, store, paths)
    # A separate registry prevents externally registered tools from enabling
    # themselves via provider_gate, including calls absent from the advertised schema.
    registry = ToolRegistry([name for name in READ_TOOLS if context.registry.is_enabled(name)])
    for name in registry.enabled:
        registry.register(context.registry.get(name))
    context.registry = registry
    context.project_inspection = True
    context.ask_gate = None
    return context, None, context.artifacts


def readable(root, protected_paths, path):
    """Check lexical scope before looking at any descendant or resolving a link."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root) or permissions.check_credential_path(path):
        return False
    if any(path.is_relative_to(protected) for protected in protected_paths):
        return False
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if (part.casefold() in {".git", ".venv", ".vastar"} or ":" in part
                or current.is_reserved() or permissions.is_env_path(current)):
            return False
        try:
            entry = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
    return True


def prompt_from_args(args):
    if any((args.continue_session, args.show_paths, args.init_project, args.migrate_from,
            args.apply_migration, args.migrate_storage, args.apply_storage_migration, args.verbose_timing)):
        raise ValueError("--exec 与会话续聊、维护、路径显示或交互计时参数互斥")
    if args.exec_prompt == "-" and sys.stdin.isatty():
        raise ValueError("--exec - 需要通过标准输入传入任务")
    prompt = sys.stdin.read() if args.exec_prompt == "-" else args.exec_prompt
    if not prompt.strip():
        raise ValueError("--exec 需要非空任务文本")
    return prompt


def _strict_json(text):
    def unique(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError('JSON 对象包含重复的字段名')
        return obj

    def finite(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError('JSON 数字必须有限')
        try:
            exact = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError('JSON 小数的指数超出可核实范围') from exc
        if exact != Decimal(str(value)):
            raise ValueError('JSON 小数无法在最终回执中原值表示')
        return value

    value = json.loads(text, object_pairs_hook=unique, parse_constant=finite, parse_float=finite)
    # An unpaired escaped surrogate must not make the final UTF-8 receipt impossible to print.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def load_output_schema(filename):
    from core.harness.validators import validate_schema_contract

    try:
        path = Path(filename)
        if not path.is_file():
            raise ValueError('需要普通 UTF-8 JSON 文件')
        with path.open('rb') as source:
            raw = source.read(SCHEMA_BYTE_LIMIT + 1)
        if len(raw) > SCHEMA_BYTE_LIMIT:
            raise ValueError('schema 文件超过 64 KiB')
        schema = _strict_json(raw.decode('utf-8-sig'))
        validate_schema_contract(schema)
        return schema
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        raise ValueError(f'--output-schema 无效：{exc}') from exc


def validate_receipt_output(receipt, schema):
    from core.harness.validators import check_schema

    if receipt['status'] != 'COMPLETED' or receipt['durability'] != 'confirmed' or receipt['tool_issues']:
        return {**receipt, 'output_validation': {'status': 'not_run'}}
    try:
        value = _strict_json(receipt['answer'])
        if not check_schema(value, schema):
            raise ValueError('最终 JSON 与指定 schema 不符')
    except (ValueError, TypeError, RecursionError) as exc:
        return {**receipt, 'output_validation': {'status': 'failed', 'reason': str(exc)}}
    return {**receipt, 'structured_output': value, 'output_validation': {'status': 'passed'}}


class ReceiptRenderer:
    """Publish only allowlisted progress fields. Finish from the durable caller receipt."""
    def __init__(self, *, stream_events=False):
        self.tool_issues = []
        self.run_id = None
        self.stream_events = stream_events
        self.sequence = 0
        self.finished = False

    def _emit(self, kind, **fields):
        self.sequence += 1
        record = {"seq": self.sequence, "type": kind, "run_id": self.run_id, **fields}
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def render(self, event):
        if self.finished:
            return
        self.run_id = event.run_id or self.run_id
        if event.type == EventType.RUN_STATE_CHANGED and event.payload.get("to") == "PREPARING":
            if self.stream_events and self.sequence == 0:
                self._emit("run.started")
        if event.type in TOOL_PROGRESS:
            name = event.payload.get("tool")
            if self.stream_events and not (isinstance(name, str) and name in PUBLIC_TOOL_NAMES):
                name = "<unlisted>"
            if event.type in TOOL_ISSUES:
                self.tool_issues.append({"tool": name, "event": event.type})
            if self.stream_events:
                self._emit(event.type, tool=name)

    def finish(self, receipt):
        if not self.finished:
            self.finished = True
            self.run_id = receipt["run_id"]
            if self.stream_events:
                self._emit("run.finished", receipt=receipt)
            else:
                print(json.dumps(receipt, ensure_ascii=False), flush=True)
        return 0 if (receipt["status"] == "COMPLETED" and receipt["durability"] == "confirmed"
                     and not receipt["tool_issues"]
                     and receipt.get("output_validation", {}).get("status", "passed") == "passed") else 1

    def render_error(self, error):
        print(error, file=sys.stderr)

    def render_note(self, text):
        print(text, file=sys.stderr)


async def inspect_once(app, prompt):
    coordinator = app["coordinator"]
    task = asyncio.create_task(coordinator.run_once(prompt))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        coordinator.request_cancel()
        result = await task
    store = app["store"]
    answer = store.assistant_text(coordinator.session_id, result.answer_message_id)
    return {"session_id": coordinator.session_id, "run_id": result.run_id,
            "status": result.status.upper(), "finish_reason": result.finish_reason,
            "answer": answer, "durability": result.durability, "model_calls": result.model_calls,
            "tool_issues": app["renderer"].tool_issues}


def failed_receipt(app, error):
    renderer = app["renderer"]
    cancelled = isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
    return {"session_id": app["coordinator"].session_id, "run_id": renderer.run_id,
            "status": "CANCELLED" if cancelled else "FAILED",
            "finish_reason": "cancelled_by_user" if cancelled else type(error).__name__, "answer": "",
            "durability": "unconfirmed", "model_calls": None, "tool_issues": renderer.tool_issues}
