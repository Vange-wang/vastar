"""Vastar runtime component."""

from __future__ import annotations

import os
import shutil
import sys

from core.errors import VastarError
from core.events import EventType
from ui.render_lifecycle import RenderLifecycle
from ui.render_thinking import RenderThinking
from ui.thinking_window import ThinkingWindow

_RESET = "\x1b[0m"
_PLAIN = "style.content"
DEFAULT_TERMINAL_WIDTH = 80   
MIN_TERMINAL_WIDTH = 20       

STYLE_KEYS = {
    "style.reasoning": "\x1b[2m\x1b[38;5;110m",   # 弱化浅蓝：思考区
    "style.content": "",                           # 正文默认色
    "style.status": "\x1b[38;5;75m",               # 蓝：状态
    "style.warning": "\x1b[38;5;214m",             # 橙：警告
    "style.error": "\x1b[38;5;203m",               # 红：错误
    "style.accent": "\x1b[1m\x1b[38;5;117m",       # 亮蓝粗体：强调
}

_REASONING_TAG = "▏思考 "

_CONTENT_TAG = "▏回答 "


def _terminal_width() -> int:
    """当前终端宽度（下限钳制；无终端环境回退 DEFAULT_TERMINAL_WIDTH）。"""
    try:
        return max(MIN_TERMINAL_WIDTH, shutil.get_terminal_size().columns)
    except Exception:  # noqa: BLE001 - 无终端环境
        return DEFAULT_TERMINAL_WIDTH


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



def _incomplete_text(p: dict) -> str:
    """run.ended 的 incomplete 可行动提示（撞顶给上限值与 /continue；超时给时长口径）。"""
    reason = str(p.get("finish_reason", ""))
    
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
        
        self._thinking_window = ThinkingWindow(thinking_visible_lines)
        self._open_channel: str | None = None
        self._last_provider: str | None = None
        # R2 思考状态：_rv_text = 思考全文（/think + 窗口数据源，不受限高影响）；
        # _rv_block_lines/_rv_dirty 驱动窗口重绘与折叠擦除（口径=实际块高，恒 ≤8）。
        # _rv_lines/_rv_col 仅供 /think toggle 展开时统计全文打印行数（_rv_feed）。
        self._rv_lines = 0
        self._rv_col = 0
        self._rv_width = DEFAULT_TERMINAL_WIDTH
        self._rv_text = ""
        self._rv_block_lines = 0
        self._rv_dirty = False
        self._last_reasoning: str | None = None
        # R2 v1.1：/think toggle 状态
        self._think_expanded = False
        self._think_rv_lines = 0
        self._think_rv_col = 0
        self._think_writes_since_expand = 0
        # R9（P0-6）：光标是否处于行首——提示行只在行边界重绘（帧级不动）
        self._at_line_start = True
        
        self._in_continuation = False

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
        out = self._stream
        out.write(text)
        if flush:
            out.flush()

    def flush_line_boundary(self) -> None:
        """Vastar runtime component."""
        if self._open_channel == "reasoning" and self._rv_dirty:
            self._rv_repaint()
            return
        if not self._at_line_start:
            self._emit(_PLAIN, "\n", flush=True)

    def _styled(self, key: str, text: str) -> str:
        style = STYLE_KEYS.get(key, "")
        return f"{style}{text}{_RESET}" if style else text

    def _close_open_channel(self) -> None:
        if self._open_channel == "reasoning":
            self._collapse_reasoning()
        elif self._open_channel is not None:
            self._emit(_PLAIN, "\n", flush=True)
        self._open_channel = None

    # ---- 事件渲染 -----------------------------------------------------------------

    def render(self, event) -> None:  # RuntimeEvent（避免循环导入用鸭子类型）
        t = event.type
        if t == EventType.REASONING_DELTA:
            self._stream_delta("reasoning", str(event.payload.get("text", "")),
                               _REASONING_TAG, "style.reasoning")
        elif t == EventType.CONTENT_DELTA:
            self._stream_delta("content", str(event.payload.get("text", "")),
                               _CONTENT_TAG, "style.content")
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

    def _stream_delta(self, channel: str, text: str, tag: str, style_key: str) -> None:
        if not text:
            return
        if not self._show_reasoning and channel == "reasoning":
            return
        if self._open_channel != channel:
            self._close_open_channel()
            if channel == "reasoning":
                
                # 窗口块由边界计时器合批重绘（≤0.2s 可见，R2 实时性不破）。
                self._rv_width = _terminal_width()
                self._rv_text = ""
                self._rv_block_lines = 0
                self._rv_dirty = False
            elif self._in_continuation:
                
                self._in_continuation = False
            else:
                self._emit(style_key, tag)      # 部分行缓冲；边界计时器成批落屏（R9）
            self._open_channel = channel
        if channel == "reasoning":
            self._rv_text += text
            self._rv_dirty = True               # 落屏合批点：flush_line_boundary
        else:
            self._emit(style_key, text)         # 同上：批尾换行 → 提示行独占下一行

    # ---- 协调器直接通知 --------------------------------------------------------------

    def render_error(self, err: VastarError) -> None:
        self._close_open_channel()
        self._emit("style.error", f"[error] {err.code} · {err.message}\n", flush=True)

    def render_note(self, text: str) -> None:
        self._close_open_channel()
        self._emit("style.status", f"{text}\n", flush=True)


def _render_tool_event(renderer: Renderer, event) -> None:
    """Vastar runtime component."""
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
        "tool.requested": f"▏调用 {name} …（{payload.get('args_summary', '')}）",
        "tool.executed": (f"▏{name} 完成（{payload.get('duration_ms', 0)}ms"
                          + (f"，截断 {payload.get('truncated_bytes')} 字节"
                             if payload.get("truncated_bytes") else "") + "）"),
        "tool.rejected": f"▏{name} 被拒绝：{payload.get('detail') or payload.get('reason', '')}",
        "tool.failed": f"▏{name} 失败：{payload.get('reason', '')}",
        "tool.cancelled": f"▏{name} 已取消",
    }
    if kind == "tool.executed" and payload.get("active"):
        lines[kind] = f"▏{name} 已提交，执行体仍在收尾核实中"
    if kind == "tool.failed" and payload.get("operation_state") == "unknown":
        lines[kind] = f"▏{name} 结果待核实：{payload.get('reason', '')}"
    if kind == "tool.cancelled" and payload.get("operation_state") == "cancelled_before_commit":
        lines[kind] = f"▏{name} 已在提交前取消（目标未提交新写入）"
    style = "style.error" if kind == "tool.failed" else \
        ("style.warning" if kind in ("tool.rejected", "tool.cancelled") else "style.status")
    renderer._close_open_channel()
    renderer._emit(style, lines[kind] + "\n")


def _render_skill_event(renderer: Renderer, event) -> None:
    """B11：技能触发落屏（D4：加载/替换/拒绝都用户可见；模块级守 V6 类红线）。"""
    p = event.payload
    label = p.get("trigger_label", p.get("trigger", ""))
    if event.type == EventType.SKILL_REJECTED:
        style, line = "style.warning", f"✋ 技能未加载：{p.get('detail', '')}"
    elif p.get("action") == "replaced":
        style = "style.status"
        line = (f"⚡ 已加载技能「{p.get('skill')}」v{p.get('version')}"
                f"（{label}触发；{p.get('detail', '')}）")
    else:
        style = "style.status"
        line = f"⚡ 已加载技能「{p.get('skill')}」v{p.get('version')}（{label}触发）"
    renderer._close_open_channel()
    renderer._emit(style, line + "\n")
