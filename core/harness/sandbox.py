"""Offline execution in disposable private roots; never silently fall back."""

import os
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass

from core.tools.process_policy import ProcessPolicy


@dataclass(frozen=True)
class ExecutionReceipt:
    operation_id: str
    owner_id: str
    status: str
    exit_code: int | None
    issued: bool
    rollback: str = "Code rollback cannot undo an issued command; private scratch is disposable."


class ExecutionSandbox:
    def __init__(self, policy: ProcessPolicy):
        self.policy = policy
        self.owner_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._active = False
        self._cancel = threading.Event()
        self._unresolved = False

    def preflight(self):
        available = (os.name == "nt" and self.policy.mode == "appcontainer"
                     and shutil.which("pwsh") is not None)
        return {**self.policy.capabilities(), "available": available,
                "execution_effective": available and self.policy.capabilities()["execution_effective"],
                "isolation": "AppContainer 只读文件模式（每次执行前核实并封闭）"
                             if available else "非沙箱（拒绝执行）",
                "cwd": "fresh private profile/work; not host project cwd",
                "file_root": "read-only private package; Windows shared runtime resources remain readable",
                "env_allowlist": ["SystemRoot", "WINDIR", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA",
                                  "USERPROFILE", "DOTNET_CLI_TELEMETRY_OPTOUT", "POWERSHELL_TELEMETRY_OPTOUT"],
                "network": "no capability SIDs; offline only",
                "credentials": "none in child; necessary trusted adapters retrieve separately at call time",
                "process_tree_owner": self.owner_id,
                "job_object": "tree lifecycle only; not file/network isolation"}

    def run(self, script: str, *, approved=False, timeout=30, credential_adapter=None):
        self.policy.authorize(approved=approved)
        self.assert_can_rollback()
        if os.name != "nt":
            raise PermissionError("非沙箱：AppContainer unavailable; execution refused")
        if not isinstance(script, str) or not script or not 0 < timeout <= 300:
            raise ValueError("nonempty script and timeout in (0,300] required")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("owner already has an in-flight command")
        from core.harness.windows_process import NativeProcess
        from core.harness.windows_sandbox import ContainerProfile

        profile = process = None
        operation_id = uuid.uuid4().hex
        issued = False
        status, code = "failed", None
        self._active = True
        self._cancel.clear()
        try:
            if credential_adapter is not None and not credential_adapter.perform():
                status = "credential_adapter_failed"
            else:
                profile = ContainerProfile()
                profile.install_runtime()
                if self._cancel.is_set():
                    status = "cancelled"
                else:
                    process = NativeProcess(profile, script, cancel=self._cancel)
                    issued = True
                    status, code = process.wait(timeout, self._cancel)
        except Exception as exc:
            # Native error strings can contain paths/command data; do not serialize them.
            issued = issued or getattr(exc, "execution_issued", False)
            status = "unknown" if issued else "cancelled" if isinstance(exc, InterruptedError) else "failed"
        finally:
            try:
                if process:
                    process.close()
                if profile:
                    profile.close()
            except Exception:
                status = "unknown" if issued else "cleanup_failed"
            finally:
                self._unresolved = status in ("unknown", "cleanup_failed")
                self._active = False
                self._lock.release()
        return asdict(ExecutionReceipt(operation_id, self.owner_id, status, code, issued))

    def cancel(self):
        self._cancel.set()

    def assert_can_rollback(self):
        if self._active or self._unresolved:
            raise RuntimeError("stop/reconcile owned in-flight command before adapter rollback")
        if getattr(self, "process_service", None) is not None:
            self.process_service.assert_can_rollback()

    def preflight_text(self):
        import json

        facts = self.preflight()
        service = getattr(self, "process_service", None)
        if service is not None:
            facts["tools_enabled"] = {name: service.registry.is_enabled(name) for name in
                                      ("start_process", "read_process", "terminate_process")}
        return "执行 preflight（T11/T12/T13 各自开关；启动非完成）：" + json.dumps(facts, ensure_ascii=False)
