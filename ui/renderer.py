"""Renderer：事件 → 呈现（独立通道；不反向驱动执行，采纳件 §2.2）。

颜色纪律（基线 §1）：颜色集中在本文件的语义样式键里，事件处理代码不出现具体颜色。
文案纪律（token-gate 裁定②）：只允许说"估算未超预算"，禁止声称"已保证不超模型上限"。

输出路径（TASK-07 v1.12 P0-9.1 按 R9 调整）：内容流写**当前 sys.stdout**（REPL 中 =
patch_stdout(raw=True) 代理）**缓冲不逐片 flush**；由 UI 层边界计时器周期调用
`flush_line_boundary()`：思考整块重绘；正文部分行用 NEL＋相对定位成批落屏，
不向正文注入换行（HM-182），提示框始终独占下一行（R9 三判据帧级成立），
思考与正文均以 ≤0.2s 粒度可见（实时）；最终 sink 统一 CRLF（HM-183）。
raw=True 走 write_raw，ANSI 原样到达终端由
VT 窗口解释为颜色。**不得经 output.write()**（Vt100_Output.write 把 ESC 替换为 "?"）；
**不得逐 delta run_in_terminal**（擦写重画互相打架）。

呈现决策（通道/折叠/think toggle）在本类与其 mixin；本类是唯一呈现 sink——实验版
（AppRenderer，`--ui app`）已随 TASK-29 KM-25 整体删除，该形态留待输入框阶段按备忘重建：
collab/decisions/2026-09-26-ui-app-phase-backlog_hm.md。落地方式：
- inline 主路径（HM-031/HM-034，TASK-12）：思考以**滚动窗口**显示（状态头 `▏思考中~~~`
  + 分隔线 + 省略行 + 最近 N 行，块高恒 ≤N+3=8，见 ui/thinking_window.py）；结束即按
  **实际块高**整块上擦——擦除有界、折叠必成（C4② 实机已证同序列可用）。旧"超屏高
  降级 append"路径随滚动窗口废除：不再存在"因思考过长而放弃折叠"的分支。

B40 职责拆分（TASK-2026-10-05-77，纯搬家不改行为）：思考折叠机制 → ui/render_thinking.py
（RenderThinking mixin）；Run 生命周期呈现 → ui/render_lifecycle.py（RenderLifecycle）；
工具/技能事件一行式 → 本文件模块级函数（守 V6 类红线）。
"""

from __future__ import annotations

import os
import sys

from core.errors import VastarError
from core.events import EventType
from ui.mood import MoodParser, MoodState
from ui.render_lifecycle import RenderLifecycle
from ui.render_thinking import RenderThinking
from ui.theme import (
    BRIGHT_BLUE,
    BRIGHT_YELLOW,
    DEFAULT_TERMINAL_WIDTH,
    DIM_BLUE,
    DIM_CONTENT,
    DIM_PROCESS,
    DIM_STATUS,
    ERROR,
    WARNING,
    box_rules,
    process_symbol,
    terminal_width,
)
from ui.thinking_window import ThinkingWindow

_RESET = "\x1b[0m"
_PLAIN = "style.content"
_RAW = "style.raw"               # 已着色文本（欢迎块/消息框线）：不再叠加样式
_terminal_width = terminal_width   # 兼容旧导入（render_thinking / 既有用例）

STYLE_KEYS = {
    "style.reasoning": DIM_BLUE,       # 弱化浅蓝：思考区
    "style.content": DIM_CONTENT,      # 默认前景正文（HM-181 v1.3：白色非 dim）
    "style.status": DIM_STATUS,        # 暗灰蓝：紧随消息的提示让位（HM-177）
    "style.process": DIM_PROCESS,      # 降亮蓝：中间过程（§10）
    "style.warning": WARNING,          # 橙黄：警告
    "style.error": ERROR,              # 红：错误
    "style.accent": BRIGHT_BLUE,       # 亮蓝粗体：强调
}


def enable_windows_vt() -> None:
    if os.name == "nt":
        os.system("")  # 激活 ANSI/VT 处理（公开技巧，无第三方依赖）


def active_output_class() -> str:
    """当前 prompt_toolkit Output 实现类名（无 REPL 环境返回 "-"）。证据 ① 用。"""
    try:
        from prompt_toolkit.application import get_app_session

        return type(get_app_session().output).__name__
    except Exception:  # noqa: BLE001 - 无终端环境
        return "-"


# TASK-34：incomplete 三类区分文案（撞顶/超时/其它不得混用，QD-276 两处落点之一）。
def _incomplete_text(p: dict) -> str:
    """run.ended 的 incomplete 可行动提示（撞顶给上限值与 /continue；超时给时长口径）。"""
    reason = str(p.get("finish_reason", ""))
    # HM-148（TASK-78）：任务态 Run 的 /continue 受步骤/预算/恢复门禁约束，
    # 提示不再单轨承诺 /continue，同时给 /task status 入口（run.ended 经 annotate 带 task_id）。
    cont_hint = ("输入 /continue 可继续" if not p.get("task_id")
                 else "可输入 /continue 续写，或 /task status 查看任务动作")
    if reason == "output_limit":
        limit = p.get("max_output_tokens") or "?"
        n = int(p.get("continuations") or 0)
        cont = f"，已自动续写 {n} 次" if n else ""
        return (f"[未完整] 已达单次输出上限（max_tokens={limit}）{cont}，"
                f"本条回答被截断；{cont_hint}")
    if reason == "run_total_timeout":
        total = p.get("run_total_s") or "?"
        return (f"[未完整] 已达单次时长上限（run_total_s={total}），"
                f"本条回答被截断；{cont_hint}")
    if reason == "stream_closed_without_finish":
        return f"[未完整] 生成中断（未收到结束帧），已输出内容已保留；{cont_hint}"
    if reason.isupper():
        # B05 断流/异常收尾：err.code 作 finish_reason——人能看懂的提示（来源已随
        # error 事件与 errors 列表保留，文案只负责"看懂"，不改布局）
        return f"[未完整] 生成中断（{reason}），已输出内容已保留；{cont_hint}"
    return f"[未完整] {reason}"


class Renderer(RenderLifecycle, RenderThinking):
    """流式渲染器：思考与正文分流；深色蓝主题；提示克制。

    B40 职责拆分：思考折叠机制（滚动窗口/折叠//think toggle）在 RenderThinking
    （ui/render_thinking.py）；Run 生命周期呈现在 RenderLifecycle；本类留
    事件分发、通道原语与 /status 之外的直接通知。"""

    def __init__(self, *, out=None, show_reasoning: bool = True,
                 thinking_visible_lines: int = 5) -> None:
        # out 仅测试注入用；生产恒为 None → 写入时动态取 sys.stdout（绝不缓存：
        # patch_stdout 在 REPL 启动后才替换 sys.stdout，缓存会绕过代理直写控制台）
        self._out = out
        self._show_reasoning = show_reasoning
        # TASK-12/HM-034：思考滚动窗口；TASK-116 §9：顶线+转动状态头+最近 N 行+底线，
        # 块高恒 ≤N+4（擦除有界、折叠必成不变）。
        self._thinking_window = ThinkingWindow(thinking_visible_lines)
        self._open_channel: str | None = None
        # HM-185：正文部分行以 NEL＋相对定位在内容尾与提示行往返，
        # 不注入换行也不保存会随滚屏失效的绝对行；dirty 防止重复擦画提示框。
        self._content_prompt_below = False
        self._content_dirty = False
        self._content_col = 0
        self._last_provider: str | None = None
        # R2 思考状态：_rv_text = 思考全文（/think + 窗口数据源，不受限高影响）；
        # _rv_block_lines/_rv_dirty 驱动窗口重绘与折叠擦除（口径=实际块高）。
        # _rv_lines/_rv_col 仅供 /think toggle 展开时统计全文打印行数（_rv_feed）。
        self._rv_lines = 0
        self._rv_col = 0
        self._rv_width = DEFAULT_TERMINAL_WIDTH
        self._rv_text = ""
        self._rv_block_lines = 0
        self._rv_dirty = False
        self._rv_frame = 0               # §9 思考行转动帧（一／|／/／\ 循环）
        self._last_reasoning: str | None = None
        # R2 v1.1：/think toggle 状态
        self._think_expanded = False
        self._think_rv_lines = 0
        self._think_rv_col = 0
        self._think_writes_since_expand = 0
        # R9（P0-6）：光标是否处于行首——提示行只在行边界重绘（帧级不动）
        self._at_line_start = True
        # TASK-116 §6⑤：心情随生成流剥离（零额外付费调用）；状态行只读 MoodState。
        self._mood_parsers = {"content": MoodParser(), "reasoning": MoodParser()}
        self.mood = MoodState()
        self._ui_dirty = False           # 状态行需重绘（Run 结束/心情更新）
        self.last_run_seconds: float | None = None   # 时间栏 `√` 后的上轮耗时

    # ---- 基础输出 ----------------------------------------------------------------

    @property
    def _stream(self):
        return self._out if self._out is not None else sys.stdout

    def _emit(self, style_key: str, text: str, *, flush: bool = False) -> None:
        """呈现原语：语义键 + 可视文本。stdout sink 翻译为 ANSI；全屏应用子类覆盖。

        行首状态与 /think 写计数基于**可视文本**在此判定——styled 之后的字符串以
        RESET 码结尾，endswith("\\n") 判定恒 False（R9 判据失效根因，v2.2 修复）。"""
        if text:
            self._at_line_start = text.endswith("\n")
        if self._think_expanded:
            self._think_writes_since_expand += 1
        self._raw_write(self._styled(style_key, text), flush=flush)

    def _raw_write(self, text: str, *, flush: bool = False) -> None:
        """最终 sink 统一 CRLF；二进制可行时绕开 TextIOWrapper 再翻译（HM-183）。"""
        out = self._stream
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
        if hasattr(out, "encoding") and hasattr(out, "buffer"):
            out.buffer.write(normalized.encode(out.encoding or "utf-8", "replace"))
        else:
            out.write(normalized)
        if flush:
            out.flush()

    def flush_line_boundary(self) -> None:
        """R9 两段式重绘：思考按节拍重绘，正文部分行用相对光标往返落屏。

        思考通道打开期间每个边界节拍都整块重绘（HM-034 合批 ≤0.2s 红线；
        TASK-116 §9 状态头转动在无语义增量时也逐帧推进）。正文通道只在有增量时
        移到下一行重绘提示框；下个增量按记录列相对返回正文末尾，**不注入换行**
        （HM-182），因此流式分片不会变成硬断行。其它通道仍按真实行边界落屏。
        """
        if self._open_channel == "reasoning":
            self._rv_repaint()
            return
        if self._open_channel == "content":
            if not self._content_dirty:
                return
            self._content_dirty = False
            if self._at_line_start:
                return
            # NEL 在屏幕底部会滚屏；CNL 只钳位，随后上移会覆盖正文（HM-185）。
            self._raw_write("\x1bE", flush=True)
            self._content_prompt_below = True
            return
        if not self._at_line_start:
            self._emit(_PLAIN, "\n", flush=True)

    def _styled(self, key: str, text: str) -> str:
        style = STYLE_KEYS.get(key, "")
        return f"{style}{text}{_RESET}" if style else text

    def _close_open_channel(self) -> None:
        channel = self._open_channel
        if channel is not None:
            # 心情残片不静默丢字：思考并入全文（/think 可见），正文原样落屏。
            pending = self._mood_parsers[channel].flush_pending()
            if channel == "reasoning":
                self._rv_text += pending
                self._collapse_reasoning()
            elif channel == "content":
                _, bottom = box_rules("Vastar", BRIGHT_YELLOW)
                if pending and not pending.endswith("\n"):
                    pending += "\n"
                # 底线必须独占一行：提示在正文下时，未满列相对回正文末；
                # 满列后提示已在自然新行，直接写底线，不多移一行（HM-183 滚屏安全）。
                prefix = ""
                wrapped_prompt_line = False
                if self._content_prompt_below:
                    wrapped_prompt_line = self._content_col == _terminal_width()
                    if not wrapped_prompt_line:
                        prefix = f"\x1b[1A\x1b[{self._content_col + 1}G"
                self._content_prompt_below = False
                self._content_dirty = False
                if not self._at_line_start and not wrapped_prompt_line:
                    # 满列时终端处于 delayed-wrap；先离开该状态，避免底线覆盖末字。
                    prefix += ("\x1bE" if self._content_col == _terminal_width()
                               else "\n")
                self._content_col = 0
                self._emit(_RAW, prefix + pending + bottom + "\n", flush=True)
            else:
                self._emit(_PLAIN, pending + "\n", flush=True)
        self._open_channel = None

    # ---- 事件渲染 -----------------------------------------------------------------

    def render(self, event) -> None:  # RuntimeEvent（避免循环导入用鸭子类型）
        t = event.type
        if t == EventType.REASONING_DELTA:
            self._stream_delta("reasoning", str(event.payload.get("text", "")),
                               "style.reasoning")
        elif t == EventType.CONTENT_DELTA:
            self._stream_delta("content", str(event.payload.get("text", "")),
                               "style.content")
        elif t == EventType.ERROR:
            self._close_open_channel()
            payload = event.payload
            self._emit("style.error",
                       f"[error] {payload.get('code', 'ERROR')} · "
                       f"{payload.get('message', '')}\n", flush=True)
        elif t == EventType.ATTEMPT_STARTED:
            self._render_attempt(event)
        elif t == EventType.RUN_ENDED:
            self._render_run_ended(event)
        elif t == EventType.INPUT_ACCEPTED:
            if str(event.payload.get("mode")) == "interrupt":
                self._close_open_channel()
                self._emit("style.warning", "已打断当前回答，正在执行新输入…\n", flush=True)
        elif str(t).startswith("task."):
            self.render_note(str(event.payload))
        elif str(t).startswith("tool."):
            _render_tool_event(self, event)
        elif t == EventType.CONTEXT_COMPACTED and event.payload.get('kind') == 'tool_archive':
            self.render_note(event.payload['detail'])
        elif str(t).startswith("skill."):
            _render_skill_event(self, event)   # B11：替换/拒绝必须用户可见（D4）
        # run.state_changed / usage.reported / context.compacted：不逐条刷屏（提示不冗余）

    def _stream_delta(self, channel: str, text: str, style_key: str) -> None:
        # TASK-116 §6⑤：心情标记先从可视流剥离（畸形/残片不吞字，见 MoodParser）。
        text, moods = self._mood_parsers[channel].feed(text)
        self.mood.update(moods)
        if not text:
            return
        if not self._show_reasoning and channel == "reasoning":
            return
        if self._open_channel != channel:
            self._close_open_channel()
            if channel == "reasoning":
                # TASK-12/HM-034：思考不再逐片直写——只累积全文并置 dirty，
                # 窗口块由边界计时器合批重绘（≤0.2s 可见，R2 实时性不破）。
                self._rv_width = _terminal_width()
                self._rv_text = ""
                self._rv_block_lines = 0
                self._rv_dirty = False
            else:
                # TASK-116 §8：Vastar 回复块——黄色顶线 `╭─ Vastar …╮`。
                self._content_col = 0
                top, _ = box_rules("Vastar", BRIGHT_YELLOW)
                self._emit(_RAW, top + "\n")    # 部分行缓冲；边界计时器成批落屏（R9）
            self._open_channel = channel
        if channel == "reasoning":
            self._rv_text += text
            self._rv_dirty = True               # 落屏合批点：flush_line_boundary
        else:
            from prompt_toolkit.utils import get_cwidth

            # HM-182/HM-183：上一拍若已把提示框移到下一行，用相对上移＋列号
            # 回正文末尾，滚屏后不依赖 DECRC 的过期绝对行；本拍仍不伪造换行。
            width = _terminal_width()
            if self._content_prompt_below:
                if self._content_col == width:
                    # 满列后的提示行就是自然折行后的新行，无需再移动。
                    self._content_col = 0
                else:
                    self._raw_write(f"\x1b[1A\x1b[{self._content_col + 1}G")
                self._content_prompt_below = False
            elif self._content_col == width:
                # 满列时终端处于 delayed-wrap；先落到下一行，防止首字覆盖末字。
                self._raw_write("\x1bE")
                self._content_col = 0
            self._emit(style_key, text)
            for ch in text:
                if ch == "\n":
                    self._content_col = 0
                    continue
                w = max(0, get_cwidth(ch))
                if w == 2 and self._content_col == width - 1:
                    # ConPTY 允许宽字符占末列；随后按满列 delayed-wrap 处理。
                    self._content_col = width
                elif self._content_col + w > width:
                    self._content_col = w
                else:
                    self._content_col += w
            self._content_dirty = True

    # ---- 协调器直接通知 --------------------------------------------------------------

    @property
    def thinking_enabled(self) -> bool:
        """状态行「思考程度」只读口径（生效态）。"""
        return self._show_reasoning

    def consume_ui_dirty(self) -> bool:
        """状态行重绘请求（Run 结束/心情更新）；读取即清。"""
        dirty = self._ui_dirty or self.mood.dirty
        self._ui_dirty = False
        self.mood.consume_dirty()
        return dirty

    def render_error(self, err: VastarError) -> None:
        self._close_open_channel()
        self._emit("style.error", f"[error] {err.code} · {err.message}\n", flush=True)

    def render_note(self, text: str) -> None:
        self._close_open_channel()
        self._emit("style.status", f"{text}\n", flush=True)

    def render_welcome(self, text: str) -> None:
        """欢迎块（§1/§2）：已着色的多行文本原样落屏，不再叠加样式。"""
        self._close_open_channel()
        self._emit(_RAW, text, flush=True)

    def render_user_message(self, name: str, text: str) -> None:
        """§8/HM-180：亮蓝框线不变；用户块正文与输入、回复正文同色。"""
        self._close_open_channel()
        top, bottom = box_rules(name, BRIGHT_BLUE)
        body = str(text)
        if not body.endswith("\n"):
            body += "\n"
        self._emit(_RAW, f"{top}\n")
        self._emit(_PLAIN, body)
        self._emit(_RAW, f"{bottom}\n", flush=True)


def _render_tool_event(renderer: Renderer, event) -> None:
    """工具事件一行式展示（TASK-15，Spec §8）：走 _emit 的合批路径（不另开落屏，R9 不破）。

    模块级函数（Renderer 类 304/300 超红线拆出，V6）。
    tool.ask（TASK-24）：询问提示按既定样式落屏（⚠ 行 + 数字选项行）——同样走
    _emit 缓冲 + flush_line_boundary() 边界成批（0.15s 粒度），不抢占 `❯` 行（R9）。
    """
    payload = event.payload
    kind = str(event.type)
    name = str(payload.get("tool", "?"))
    if kind == "tool.policy":
        limit = payload.get('configured_max_rounds')
        renderer.render_note(f"工具轮数：{'不限' if limit is None else limit}；配置 {payload.get('config_id')}")
        return
    if kind == "tool.executed" and payload.get("task_result"):
        renderer.render_note(str(payload["task_result"]))
    if kind == "tool.ask":
        renderer._close_open_channel()
        for line in payload.get("lines") or []:
            renderer._emit("style.warning", str(line) + "\n")
        return
    lines = {
        "tool.requested": f"{process_symbol(name)} 调用 {name} …（{payload.get('args_summary', '')}）",
        "tool.executed": (f"{process_symbol(name)} {name} 完成（{payload.get('duration_ms', 0)}ms"
                          + (f"，截断 {payload.get('truncated_bytes')} 字节"
                             if payload.get("truncated_bytes") else "") + "）"),
        "tool.rejected": f"⚙ {name} 被拒绝：{payload.get('detail') or payload.get('reason', '')}",
        "tool.failed": f"⚙ {name} 失败：{payload.get('reason', '')}",
        "tool.cancelled": f"⚙ {name} 已取消",
    }
    if kind == "tool.executed" and payload.get("active"):
        lines[kind] = f"⚙ {name} 已提交，执行体仍在收尾核实中"
    if kind == "tool.failed" and payload.get("operation_state") == "unknown":
        lines[kind] = f"⚙ {name} 结果待核实：{payload.get('reason', '')}"
    if kind == "tool.cancelled" and payload.get("operation_state") == "cancelled_before_commit":
        lines[kind] = f"⚙ {name} 已在提交前取消（目标未提交新写入）"
    style = "style.error" if kind == "tool.failed" else \
        ("style.warning" if kind in ("tool.rejected", "tool.cancelled") else "style.process")
    renderer._close_open_channel()
    renderer._emit(style, lines[kind] + "\n")


def _render_skill_event(renderer: Renderer, event) -> None:
    """B11：技能触发落屏（D4：加载/替换/拒绝都用户可见；模块级守 V6 类红线）。"""
    p = event.payload
    label = p.get("trigger_label", p.get("trigger", ""))
    if event.type == EventType.SKILL_REJECTED:
        style, line = "style.warning", f"✋ 技能未加载：{p.get('detail', '')}"
    elif p.get("action") == "replaced":
        style = "style.process"
        line = (f"⚡ 已加载技能「{p.get('skill')}」v{p.get('version')}"
                f"（{label}触发；{p.get('detail', '')}）")
    else:
        style = "style.process"
        line = f"⚡ 已加载技能「{p.get('skill')}」v{p.get('version')}（{label}触发）"
    renderer._close_open_channel()
    renderer._emit(style, line + "\n")
