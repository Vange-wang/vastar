"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import signal

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from ui.renderer import active_output_class
from ui.repl_commands import ReplCommands
from ui.repl_keys import ReplKeys
from ui.status_view import _status_lines  # noqa: F401  （B40 拆分后兼容 re-export）

LINE_BOUNDARY_TICK_S = 0.15   

_COMMANDS = ("/help", "/exit", "/new", "/model", "/think", "/mode", "/status", "/steer",
             "/retry", "/compact", "/continue", "/skill", "/task", "/team", "/trace", "/history", "/remember", "/memory")

_HELP_TEXT = (
    "命令：/help 帮助 · /exit 退出 · /new 新会话 · /model 查看/切换模型（序号制） ·\n"
    "      /think 查看上一轮思考全文 · /mode [queue|interrupt] 插话模式 ·\n"
    "      /status 实时进度 · /steer <新要求> 立即介入当前任务 ·\n"
    "      /retry 重问上一条 · /continue 续写被截断的回答 ·\n"
    "      /trace 追踪与回放（任务链/运行链/未完成清单/产物） · /team 团队与任务管理 ·\n"
    "      /compact 上下文压缩（生成→ /compact ok 批准→ /compact rollback 回滚；drop 丢弃） ·\n"
    "      /skill [名|off] 查看技能 / 安排下一条输入显式加载 / 取消安排 ·\n"
    "按键：Enter 提交 · Ctrl+Enter 换行 · Ctrl+C 取消回答/退出选择/清空草稿 · Ctrl+D 退出"
)


class ReplUI(ReplCommands, ReplKeys):
    """输入循环：读入 → 斜杠命令或提交协调器。"""

    def __init__(self, *, coordinator, renderer,
                 input=None, output=None, use_patch_stdout: bool = True,
                 tool_registry=None, ask_gate=None,
                 permission_mode: str | None = None) -> None:
        self._coordinator = coordinator
        self._renderer = renderer
        self._tool_registry = tool_registry   # None = 工具能力关闭（/status 照实显示）
        self._ask_gate = ask_gate             
        self._permission_mode = permission_mode   # /status 展示（ask/auto）
        self._exit_requested = False
        self._session: PromptSession | None = None
        self._model_mode = False   # R3：/model 选择模式（模式吃输入，Ctrl+C 唯一回退）
        self._injected_input = input      # 测试注入（键事件驱动用例）
        self._injected_output = output
        self._use_patch_stdout = use_patch_stdout   # 无头测试关闭（无 Win32 控制台）
        self._last_ctrl_c = 0.0    # ⑤ 强退窗口计时（monotonic；模式内按键不武装）
        self._ctrl_c_announced = False  # ④ 提示只说一次，避免连按刷屏
        self._gen_note_shown = False    # R1 v1.1：生成中状态行每次 Run 只提示一次
        
        # 块高恒 ≤N+3=8）+ 结束按实际块高整块上擦——擦除有界、折叠必成，erase 为
        # 唯一路径（旧 append 超屏高降级已废除）；窗口逻辑见 ui/thinking_window.py。

    @property
    def exit_requested(self) -> bool:
        return self._exit_requested


    async def run(self) -> None:
        # P0-9.3 兜底：真控制台 Ctrl+C 以 SIGINT 到达（prompt_toolkit 键路径之外）时，
        
        def _sigint_handler(signum, frame):  # noqa: ANN001
            if self._ask_gate is not None and self._ask_gate.cancel():
                return   
            if self._coordinator.has_active_or_pending():
                self._coordinator.request_cancel()

        try:
            signal.signal(signal.SIGINT, _sigint_handler)
        except (ValueError, OSError):  # 非主线程/非主解释器环境
            pass
        self._session = PromptSession(
            multiline=True, key_bindings=self._build_bindings(),
            message="vastar> ",
            input=self._injected_input, output=self._injected_output,
        )
        
        # raw=False 会经 Vt100_Output.write 把 ESC 字面替换为 "?"（颜色全毁，
        # 即 `?[38;5;75m` 乱码根因）；raw=True 使 ANSI 原样经 write_raw 到达终端，
        # 由 Windows10_Output.flush 的 VT 窗口解释为颜色。提示框安全由代理的
        # 成批 + 重画机制保证（Renderer 已改为写 sys.stdout 走代理）。
        ticker = asyncio.create_task(self._line_boundary_ticker())
        try:
            if self._use_patch_stdout:
                with patch_stdout(raw=True):
                    await self._input_loop()
            else:
                await self._input_loop()
        finally:
            ticker.cancel()

    async def _line_boundary_ticker(self) -> None:
        """R9 两段式重绘的边界计时器：周期性把缓冲的部分行以换行成批落屏，
        使 `vastar>` 提示行只在行边界重绘（独占一行、横向不动、纵向随内容行数）。
        生成中思考/正文以 ≤0.2s 粒度成批出现（实时）；空闲时为空操作。"""
        try:
            while True:
                await asyncio.sleep(LINE_BOUNDARY_TICK_S)
                self._renderer.flush_line_boundary()
        except asyncio.CancelledError:
            return

    async def _input_loop(self) -> None:
        """R1 v1.1（改判）：`vastar>` 输入行**常驻**——生成期间 prompt 保持活跃，
        可继续输入（提交即排队，回到基线 §3 默认排队语义）；生成中 Ctrl+C 经
        c-c 键绑定取消当前 Run（语境②）。流式文本按行边界落屏（Renderer 依赖
        代理的换行缓冲），提示行不被部分行拽动。"""
        # 证据 ①：记录当前 Output 实现类（Win32Output / Windows10_Output / Vt100_Output）
        self._renderer.render_note(f"Output 实现：{active_output_class()}")
        while not self._exit_requested:
            if not self._coordinator.has_active_or_pending():
                self._gen_note_shown = False
            try:
                text = await self._session.prompt_async()
            except KeyboardInterrupt:
                # R9 实机（P0-9.3）：真控制台的 Ctrl+C 走 SIGINT → KeyboardInterrupt
                # （c-c 绑定只覆盖键事件路径）。语义 = 取消当前 Run 并**保留输入循环**。
                if self._coordinator.has_active_or_pending():
                    self._coordinator.request_cancel()
                    self._renderer.render_note("已请求取消当前回答…")
                    continue
                break
            except EOFError:
                break
            text = text.strip()
            if not text:
                continue
            if text.split(maxsplit=1)[0].lower() in ('/steer', '/status'):
                await self._handle_command(text)
                continue
            if self._ask_gate is not None:
                
                # 到 AskGate；非法输入提示重答（问题保持 pending，超时仍生效）。
                state = self._ask_gate.resolve_input(text)
                if state == "answered":
                    continue
                if state == "invalid":
                    self._renderer.render_note(
                        "请回答：1 允许 / 2 拒绝 / 3 本会话内“这类动作”都允许")
                    continue
            if self._model_mode:
                # R3：模式吃输入——模式内键入绝不作为普通消息发给模型
                self._handle_model_mode_input(text)
                continue
            if text.startswith("/"):
                await self._handle_command(text)
                if self._exit_requested:
                    break
                continue
            receipt = await self._coordinator.submit(text)
            if receipt.note:
                self._renderer.render_note(receipt.note)
            self._announce_generation()

    def _announce_generation(self) -> None:
        """R1 v1.1：生成期间状态行一次性提示（输入行仍常驻、可输入排队）。"""
        if (self._coordinator.has_active_or_pending()
                and not self._gen_note_shown):
            self._renderer.render_note("[生成中 · /steer <新要求> 立即介入 · /status 进度 · Ctrl+C 取消]")
            self._gen_note_shown = True

    def _handle_model_mode_input(self, text: str) -> None:
        """R3 选择模式：序号 → 切换并退出；非法输入 → 提示并**留在模式**；
        Ctrl+C 回退由 _ctrl_c_semantics 处理（唯一回退方式）。模式内键入绝不发给模型。"""
        ok, message = self._coordinator.switch_model(text)
        if ok:
            self._model_mode = False
            self._renderer.render_note(message)
        else:
            total = len(self._coordinator.model_catalog())
            self._renderer.render_note(f"请输入 1~{total}，Ctrl+C 回退")

