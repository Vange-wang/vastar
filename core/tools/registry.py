"""工具注册表与启用开关（Spec §3）：登记 + tools.enabled 清单（空 = 全关，default-closed）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core.limits import optional_limit
from core.tools.final_answer import FinalAnswerPolicy

if TYPE_CHECKING:
    from core.tools.ask_gate import AskGate
    from core.tools.operation_runner import OperationRunner


def _make_operations():
    # A read child imports ToolSpec but never needs an asyncio runtime.
    from core.tools.operation_runner import OperationRunner
    return OperationRunner()


class ToolParamError(Exception):
    """工具私有参数/内容校验失败（显式拒绝，不静默截断）——执行层转为 tool.failed。

    本异常属工具契约的一部分：工具实现只抛它表达"参数或内容不满足"，
    权限级别判定不在此列（由执行层 permissions 统一收口）。
    B10：origin 类属性供执行层标记结果来源（避免 operation_receipt → registry 循环依赖）。
    """

    origin = "tool_param"


@dataclass
class OpPlan:
    """Vastar runtime component."""

    action: str                       # create / edit / move / delete（判定表动作主行）
    targets: list[Path]               # 受影响目标全集（目录操作 = 展开后的受影响项）
    detail: str = ""                  # 大小/数量文案（进询问提示括号）
    overwrite: bool = False           # 是否覆盖既有目标（覆盖 → 备份底线）
    backup_source: Path | None = None  # 覆盖/改写前需备份的文件（None = 无需）
    reversible_delete: bool = False   # 删除类（可逆底线，询问提示"可逆删除"）
    needs_recycle: bool = False       
    target_count: int = 1
    total_bytes: int = 0
    recursive: bool = False


@dataclass
class ToolSpec:
    """Vastar runtime component."""

    name: str
    description: str
    parameters: dict                        # JSON Schema（function-calling parameters）
    level: str                              # "A"/"B"/"C"/"D"（判定在执行层，工具内不得判定）
    run: Callable[..., str]                 # (args, *, resolved_path, ...) -> 文本
    plan: Callable[..., OpPlan] | None = None
    cooperative: bool = False  # 显式声明同步工具实现取消检查与提交回执
    effect: str = "read"
    resource_scope: str = "file"
    retryable: bool = True
    cancel_phase: str = "any"
    reconcile: str = "none"
    preserve_full_result: bool = False  # 技能正文不可截断；完整结果仍须过下一请求 Token 门
    server_id: str = ""
    tool_name: str = ""
    schema_version: str = ""
    capability: str = ""
    model_name: str = ""
    provider_gate: Callable | None = None


@dataclass
class ToolsContext:
    """装配期 tool 能力上下文（main.py 构建；runtime 与 /status 共享）。"""

    registry: ToolRegistry
    project_root: Path
    timeout_seconds: float | None = 30.0  # Explicit null disables the local tool deadline.
    max_calls_per_turn: int | None = 5    # Calls execute sequentially; null keeps every call.
    max_tool_rounds: int | None = 10      # Explicit null disables the round ceiling.
    permission_mode: str = "ask"           
    ask_timeout_s: float = 60.0            
    operations: OperationRunner = field(default_factory=_make_operations)
    ask_gate: AskGate | None = None        # 询问闸门（进程内；None = 需确认时按拒绝处理）
    skills: object | None = None           # B11：SkillsContext（round_trip 落模型通道事件；
                                           # object 类型避免 tools→skills 反向依赖）
    cwd: Path | None = None
    data_dir: Path | None = None
    artifacts: object | None = None        # B16：ArtifactStore（截断落产物 + read_artifact 续读；
                                           # object 类型避免 tools→artifacts 反向依赖）
    protected_paths: tuple[Path, ...] = ()
    task_service: object | None = None
    memory_service: object | None = None
    operation_ledger: object | None = None
    history_service: object | None = None
    execution_sandbox: object | None = None
    process_service: object | None = None
    network_service: object | None = None
    network_records: object | None = None
    browser_service: object | None = None
    provider_service: object | None = None
    final_answer_policy: FinalAnswerPolicy = field(default_factory=FinalAnswerPolicy)
    task_budget_defaults: dict = field(default_factory=dict)  # A30 draft proposal, never automatic credit.
    rolling_tool_history: bool = False


class ToolRegistry:
    """注册表 + 启用清单开关（tools.enabled）。last_call 供 /status 可观测。"""

    def __init__(self, enabled: list[str] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self.enabled: list[str] = list(enabled or [])
        self.last_call: dict | None = None   # {tool, status, duration_ms}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name) or next((s for s in self._specs.values() if s.model_name and s.model_name == name), None)

    def is_enabled(self, name: str) -> bool:
        spec = self.get(name)
        enabled = spec is not None and spec.name in self.enabled
        return spec.provider_gate(enabled) if spec is not None and spec.provider_gate else enabled

    def register_external(self, spec: ToolSpec) -> None:
        if self.get(spec.name) or self.get(spec.model_name):
            raise ValueError('provider_name_conflict')
        self.register(spec)

    def counts(self) -> dict:
        return {'builtin_registered': sum(not s.server_id for s in self._specs.values()),
                'external_unique': len({(s.server_id, s.tool_name) for s in self._specs.values() if s.server_id}),
                'builtin_25_completion': 'not derived from registry; see coverage ledger'}

    def to_openai_tools(self) -> list[dict]:
        """已启用工具的 function-calling schema（只注入已启用项；计入 token 预算，Spec §7）。"""
        out: list[dict] = []
        for name in dict.fromkeys(self.enabled):
            spec = self._specs.get(name)
            if spec is not None and self.is_enabled(name):
                out.append({"type": "function", "function": {
                    "name": spec.model_name or spec.name, "description": spec.description,
                    "parameters": spec.parameters}})
        return out

    def record_call(self, name: str, status: str, duration_ms: int) -> None:
        self.last_call = {"tool": name, "status": status, "duration_ms": duration_ms}


def build_tools_context(config: dict, project_root: Path, *, paths=None) -> ToolsContext:
    """按 config 装配 tool 能力上下文（main.py 用；config 只读，无任何值变更）。

    enabled 清单空 = default-closed（DoD 1 总闸：行为与阶段一一致）；全部 10 个工具
    恒注册，是否可用完全由 enabled 清单决定（Spec §3）。permission_mode 只允许
    ask/auto（配置错误装配期即失败，禁静默回退）。
    """
    from core.path_context import user_config_dir
    from core.tools.delete_path import DELETE_PATH_SPEC
    from core.tools.edit_file import EDIT_FILE_SPEC
    from core.tools.glob_files import GLOB_FILES_SPEC
    from core.tools.list_dir import LIST_DIR_SPEC
    from core.tools.mkdir import MKDIR_SPEC
    from core.tools.move_file import MOVE_FILE_SPEC
    from core.tools.read_artifact import READ_ARTIFACT_SPEC
    from core.tools.read_file import READ_FILE_SPEC
    from core.tools.search_text import SEARCH_TEXT_SPEC
    from core.tools.stat_path import STAT_PATH_SPEC
    from core.tools.write_file import WRITE_FILE_SPEC

    tools_cfg = config.get("tools", {})
    mode = str(tools_cfg.get("permission_mode", "ask"))
    if mode not in ("ask", "auto"):
        raise ValueError(f"tools.permission_mode 只允许 ask/auto（收到 {mode!r}）")
    ask_timeout = float(tools_cfg.get("ask_timeout_s", 60))
    from core.harness.task_budget import load_proposal
    from core.tools.ask_gate import AskGate
    registry = ToolRegistry(enabled=tools_cfg.get("enabled", []))
    for spec in (READ_FILE_SPEC, LIST_DIR_SPEC, WRITE_FILE_SPEC, EDIT_FILE_SPEC,
                 MKDIR_SPEC, MOVE_FILE_SPEC, DELETE_PATH_SPEC,
                 SEARCH_TEXT_SPEC, GLOB_FILES_SPEC, STAT_PATH_SPEC, READ_ARTIFACT_SPEC):
        registry.register(spec)
    return ToolsContext(
        registry=registry, project_root=project_root,
        timeout_seconds=optional_limit(tools_cfg.get("timeout_seconds", 30)),
        max_calls_per_turn=optional_limit(tools_cfg.get("max_calls_per_turn", 5), integer=True),
        max_tool_rounds=optional_limit(tools_cfg.get("max_tool_rounds", 10), integer=True),
        permission_mode=mode, ask_timeout_s=ask_timeout,
        ask_gate=AskGate(timeout_s=ask_timeout),
        cwd=paths.cwd if paths else project_root,
        data_dir=(paths.data_dir if paths and (paths.schema_version == 2
                  or config.get('context', {}).get('rolling_tool_history') is True) else None),
        protected_paths=((paths.data_dir, paths.project_root / ".vastar",
                          paths.app_dir / "config", paths.app_dir / "memory",
                          paths.app_dir / "logs", user_config_dir(),
                          *(Path(p) for p in config.get("config_sources", []))) if paths else ()),
        final_answer_policy=FinalAnswerPolicy.from_config(tools_cfg.get('final_answer', {})),
        task_budget_defaults=load_proposal(tools_cfg.get('task_budget', {})),
        rolling_tool_history=config.get('context', {}).get('rolling_tool_history') is True,
    )
