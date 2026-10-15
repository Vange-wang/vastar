"""Vastar 启动入口（main.py：只装配，不承载业务——基线 §6）。

用法：
    python main.py              # 新会话
    python main.py --continue   # 续聊最近一次会话

启动即装配（本地操作，无网络预连接，实测满足 ≤2s 进界面）；
崩溃恢复：启动时把非终态 Run 标记 INTERRUPTED，不自动重跑（基线 §4）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agents.definition import load_agent  # noqa: E402
from core.artifacts import ArtifactStore  # noqa: E402
from core.continuation import ContinuationRunner  # noqa: E402
from core.env_facts import format_env_facts  # noqa: E402
from core.llm.factory import (  # noqa: E402
    build_primary_and_fallback,
    build_provider_spec,
    load_env_file,
)
from core.llm.retry import RetryPolicy  # noqa: E402
from core.path_cli import add_path_arguments, handle_maintenance  # noqa: E402
from core.path_config import load_launch_config  # noqa: E402
from core.path_migration import format_result  # noqa: E402
from core.prompt import build_prompt  # noqa: E402
from core.runtime import ReActRuntime  # noqa: E402
from core.session import SessionCoordinator  # noqa: E402
from core.skills.context import build_skills_context  # noqa: E402
from core.skills.tool_spec import build_use_skill_spec  # noqa: E402
from core.tools.registry import build_tools_context  # noqa: E402
from memory.store import Store  # noqa: E402
from memory.writer import StoreWriter  # noqa: E402
from ui.renderer import Renderer, enable_windows_vt  # noqa: E402
from ui.repl import ReplUI  # noqa: E402
from ui.startup_screen import start_startup, stop_startup  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vastar", description="Vastar — 个人专属通用 Agent")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                        help="续聊最近一次会话")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 VASTAR_CONFIG 或 config/settings.json）")
    parser.add_argument("--verbose-timing", action="store_true",
                        help="退出时打印 startup_to_input_ready 计时（KM-24：注册幽灵开关）")
    add_path_arguments(parser)
    return parser.parse_args(argv)


def build_app(args: argparse.Namespace):
    """装配全部组件（本地、无网络）；返回运行所需对象集合。"""
    launch = load_launch_config(args, BASE_DIR)
    store = Store(launch.db_path,
                  require_existing=launch.paths.schema_version == 2 or args.continue_session)
    try:
        return _assemble_app(args, launch, store)
    except BaseException:
        store.close()
        raise


def _assemble_app(args, launch, store):
    raw, paths = launch.raw, launch.paths
    load_env_file(str(launch.env_file))
    agent_def = load_agent(launch.config_path, raw=raw)
    prompt_bundle = build_prompt(raw["prompt"], BASE_DIR)

    primary, fallback = build_primary_and_fallback(raw["llm"], raw["context"])
    wait_budget = float(primary.timeouts.get("retry_wait_budget_s", 30))
    retry_policy = RetryPolicy(
        fallback_provider=fallback.name if fallback else None,
        retry_wait_budget_s=wait_budget,
    )

    # 运行时日志（P0-D #2：写失败证据不依赖同一库，落 logs/vastar.log）
    _setup_logging(launch.log_dir)

    marked = store.recover_abandoned_runs()

    writer = StoreWriter(store)
    # R11：唯一入口 = vastar（inline 主路径）；全屏/输入框实验代码已整体删除
    
    
    renderer = Renderer(
        show_reasoning=bool(
            raw["llm"]["providers"][primary.name].get("show_reasoning", True)),
        
        thinking_visible_lines=int(raw.get("ui", {}).get("thinking_visible_lines", 5)))
    tools_context, skills_context, artifacts = _assemble_tools(raw, paths, store)
    
    cont_cfg = raw["llm"].get("continuation", {})
    continuation = ContinuationRunner(
        enabled=bool(cont_cfg.get("enabled", True)),
        max_count=cont_cfg.get("max_count", 2))
    runtime = ReActRuntime(answer_message_id_factory=lambda s, r: f"{r}-assistant",
                           tools=tools_context,
                           env_facts=format_env_facts(raw.get("env")) + "\n" + paths.prompt_facts(),
                           continuation=continuation,
                           skills=skills_context)
    
    model_catalog = [
        (name, model)
        for name, block in raw["llm"]["providers"].items()
        for model in block["models"]
    ]
    coordinator = SessionCoordinator(
        store=store, writer=writer, renderer=renderer, runtime=runtime,
        agent_def=agent_def, primary=primary, fallback=fallback,
        retry_policy=retry_policy, prompt_bundle=prompt_bundle,
        token_cfg=raw["token"], context_cfg=raw["context"],
        config_id=agent_def.config_version_hash,
        model_catalog=model_catalog,
        spec_factory=lambda p, m: build_provider_spec(raw["llm"], p, raw["context"], model=m),
        skills=skills_context,
    )
    from core.harness.durable_session import install_recovery
    install_recovery(tools_context, coordinator)
    return {
        "coordinator": coordinator, "renderer": renderer, "store": store, "writer": writer,
        "agent_def": agent_def, "primary": primary, "fallback": fallback,
        "recovered_count": marked, "raw": raw,
        "tools": tools_context,   
        "skills": skills_context,   # B11：技能上下文（/status 与测试同口径的公开键）
        "artifacts": artifacts,       # B16：产物库（测试与排障的公开键）
        "paths": paths,
    }


def _assemble_tools(raw, paths, store):
    """工具、任务、技能与历史的子装配；为隔离能力接线保留主装配余量。"""
    tools_context = build_tools_context(raw, paths.workspace or paths.project_root, paths=paths)
    from core.harness.sandbox import ExecutionSandbox
    from core.tools.process_policy import ProcessPolicy
    tools_context.execution_sandbox = ExecutionSandbox(ProcessPolicy.from_config(raw))
    from core.harness.task_service import TaskService
    from core.harness.task_tools import install_task_tools

    # B50：编排管理面（opt-in；enabled=false 不接线，/team 仅剩只读视图）
    from core.orchestration.config import load_orchestration
    orchestration = load_orchestration(raw)
    task_service = TaskService(store, paths.project_id)
    install_task_tools(tools_context, task_service)
    if orchestration.enabled:
        tools_context.team_management = task_service.management(orchestration, actor='user')
    # B16：产物库（data_dir 未初始化 = legacy → put 时显式报错，不静默退化）
    artifacts = ArtifactStore(store, tools_context.data_dir,
                              owner=paths.project_id if paths else "")
    tools_context.artifacts = artifacts
    from core.tools.process_tools import install_process_tools
    install_process_tools(tools_context)
    from core.tools.network_tools import install_network_tools
    install_network_tools(tools_context, raw)
    from core.skills.research_tools import install_research_tool
    install_research_tool(tools_context)
    from core.tools.document_tools import install_document_tools
    install_document_tools(tools_context)
    from core.tools.report_tools import install_report_tools
    install_report_tools(tools_context)
    from core.tools.sheet_tools import install_sheet_tools
    install_sheet_tools(tools_context)
    from core.tools.browser_tools import install_browser_tools
    install_browser_tools(tools_context, raw)
    from core.tools.providers import install as install_providers
    install_providers(tools_context, raw)
    from core.skills.code_change import install as install_code_change
    install_code_change(tools_context)
    # Discover only after read-only dependencies are registered; B11 contracts stay intact.
    skills_context = build_skills_context(raw, BASE_DIR, tools_context.registry)
    tools_context.registry.register(build_use_skill_spec(skills_context))
    tools_context.skills = skills_context
    from core.history_tools import install_history
    install_history(tools_context, store, paths)
    return tools_context, skills_context, artifacts


def _setup_logging(log_dir: Path) -> None:
    """装配运行时文件日志（幂等：重复调用不叠加 handler）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    target = str((log_dir / "vastar.log").resolve())
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("vastar.log"):
            if h.baseFilename == target:
                return
            root.removeHandler(h)
            h.close()
    handler = logging.FileHandler(log_dir / "vastar.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _welcome_line(coordinator) -> str:
    """Vastar runtime component."""
    return (f"Vastar 就绪 · 模型 {coordinator.primary_label()}"
            f" · 输出上限 {coordinator.output_limit_label()} · /help 查看命令")


async def _run_ui(app: dict, *, continue_session: bool) -> None:
    try:
        await _serve_ui(app, continue_session=continue_session)
    finally:
        # 包含 UI 构造/欢迎输出失败路径；SQLite 连接关闭前等 writer commit。
        service = getattr(app.get("tools"), "process_service", None)
        if service is not None:
            from core.tools.operation_runner import thread_call
            await thread_call(service.close)
        network = getattr(app.get("tools"), "network_service", None)
        if network is not None:
            from core.tools.operation_runner import thread_call
            await thread_call(network.close)
        if "writer" in app:
            result = await app["writer"].close()
            if result != "confirmed":
                raise app["writer"].storage_error()


async def _serve_ui(app: dict, *, continue_session: bool) -> None:
    coordinator = app["coordinator"]
    renderer = app["renderer"]
    if continue_session:
        latest = app["store"].latest_session()
        if latest:
            coordinator.session_id = latest
            history_count = app["store"].history_count(latest)
            renderer.render_note(f"已续聊会话 {latest}（{history_count} 条历史消息）")
        else:
            renderer.render_note("没有可续聊的历史会话，已新建会话")
    
    # 降级可见性不丢失：结束时显式提示备用模型（Renderer._render_run_ended）。
    renderer.render_note(_welcome_line(coordinator))
    sandbox = getattr(app["tools"], "execution_sandbox", None)
    if sandbox is not None:
        renderer.render_note(sandbox.preflight_text())
    if app["recovered_count"]:
        renderer.render_note(
            f"崩溃恢复：{app['recovered_count']} 个未结束 Run 已标记 INTERRUPTED（不自动重跑）")
    ui = ReplUI(coordinator=coordinator, renderer=renderer,
                tool_registry=app["tools"].registry,
                ask_gate=app["tools"].ask_gate,
                permission_mode=app["tools"].permission_mode)
    coord_task = asyncio.create_task(coordinator.run_forever())
    try:
        await ui.run()
    finally:
        coordinator.stop()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await coord_task
        finally:
            await app["tools"].operations.close()


def main(argv: list[str] | None = None) -> int:
    start_ns = time.monotonic_ns()
    enable_windows_vt()
    args = parse_args(argv)
    try:
        if handle_maintenance(args):
            return 0
        if args.show_paths:
            launch = load_launch_config(args, BASE_DIR)
            print(format_result({**launch.paths.facts(), "db_path": str(launch.db_path),
                                 "config_sources": launch.sources,
                                 "env_file": str(launch.env_file)}))
            return 0
        start_startup(argv if argv is not None else sys.argv[1:])
        try:
            app = build_app(args)
        finally:
            stop_startup()
    except Exception as exc:  # noqa: BLE001 - 装配失败要给出可读原因
        print(f"[启动失败] {exc}", file=sys.stderr)
        return 2
    ready_ms = (time.monotonic_ns() - start_ns) / 1_000_000
    try:
        asyncio.run(_run_ui(app, continue_session=args.continue_session))
    except KeyboardInterrupt:
        pass
    finally:
        app["store"].close()
    if args.verbose_timing:
        print(f"startup_to_input_ready = {ready_ms:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
