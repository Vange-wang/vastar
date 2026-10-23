"""Vastar runtime component."""
from __future__ import annotations

import os
import shutil
import sys

RESET = "\x1b[0m"
FRAME_BG = "\x1b[48;5;16m"             # 框线/提示统一深色底：RGB(0, 0, 0)
BLUE = "\x1b[38;5;75m"                 # 蓝：通用主题
BRIGHT_BLUE = "\x1b[1m\x1b[38;5;117m"  # 亮蓝粗体：用户消息块框线/强调
DIM_BLUE = "\x1b[2m\x1b[38;5;110m"     # 弱化浅蓝：思考区
DIM_PROCESS = "\x1b[2m\x1b[38;5;75m"   # 降亮蓝：中间过程（工具/技能）
DIM_STATUS = "\x1b[2m\x1b[38;5;60m" + FRAME_BG  # 暗灰蓝：紧随消息的提示让位
YELLOW = "\x1b[38;5;220m"              # 黄：Vastar 主题/回退纯黄长线
BRIGHT_YELLOW = "\x1b[1m\x1b[38;5;226m"  # 亮黄：Vastar 消息块框线
DIM_CONTENT = ""
PROMPT_CONTENT_STYLE = ""              # 默认前景：输入框正文同为白色，不写黄系 ANSI
WARNING = "\x1b[38;5;214m"             # 橙黄：警告（语义色保留）
ERROR = "\x1b[38;5;203m"               # 红：错误（语义色保留）

DEFAULT_TERMINAL_WIDTH = 80
MIN_TERMINAL_WIDTH = 20

GRADIENT_YELLOW = (255, 208, 64)   # 渐变起点＝黄
GRADIENT_BLUE = (72, 152, 248)     # 渐变终点＝蓝

SPINNER_FRAMES = ("一", "|", "/", "\\")   # §9 思考行转动符号（一／|／/／\ 循环）
TIME_FRAMES = (".", "o", "O", "o")        # §6④ 执行中时间符号 `. o O` 渐变动画
DONE_SYMBOL = "√"                          # §6④ 完成后符号
TOOL_SYMBOL = "⚙"        # §10 走工具
CODE_SYMBOL = "</>"      # §10 走代码
SKILL_SYMBOL = "⚡"       # 既有技能符号保留


def terminal_width() -> int:
    """真实终端宽度（下限钳制；无终端回退 80）。

    依次尝试 `sys.__stdout__` / `sys.stdout` 的控制台句柄（patch_stdout 代理也
    暴露真实 fileno），**不经过 `COLUMNS` 环境变量**；全部失败才回退 shutil/默认。
    """
    for stream in (sys.__stdout__, sys.stdout):
        try:
            return max(MIN_TERMINAL_WIDTH, os.get_terminal_size(stream.fileno()).columns)
        except Exception:  # noqa: BLE001 - 无控制台/被捕获输出，试下一个句柄
            continue
    try:
        return max(MIN_TERMINAL_WIDTH, shutil.get_terminal_size().columns)
    except Exception:  # noqa: BLE001 - 无终端环境
        return DEFAULT_TERMINAL_WIDTH


def visible_width(text: str) -> int:
    """显示单元宽度（CJK 计 2，不含 ANSI；调用方负责先剥离 ANSI）。"""
    from prompt_toolkit.utils import get_cwidth

    return sum(max(0, get_cwidth(ch)) for ch in text)


def clip_to_width(text: str, width: int) -> str:
    """按显示单元截断（状态行防溢出；保留前缀，不折行）。"""
    from prompt_toolkit.utils import get_cwidth

    out, col = [], 0
    for ch in text:
        w = max(0, get_cwidth(ch))
        if col + w > width:
            break
        out.append(ch)
        col += w
    return "".join(out)


def truecolor_supported(env=None) -> bool:
    """真彩判定：NO_COLOR/TERM=dumb → False；COLORTERM=truecolor/24bit → True；
    Windows 看 VT 是否真实可用；其余保守回退 False（→ 纯黄，不花屏）。"""
    env = os.environ if env is None else env
    if "NO_COLOR" in env or env.get("TERM") == "dumb":
        return False
    if str(env.get("COLORTERM", "")).lower() in ("truecolor", "24bit"):
        return True
    if os.name == "nt":
        try:
            from prompt_toolkit.output.windows10 import is_win_vt100_enabled

            return bool(is_win_vt100_enabled())
        except Exception:  # noqa: BLE001 - 非控制台环境按不支持处理
            return False
    return False


def color_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return "NO_COLOR" not in env and env.get("TERM") != "dumb"


def gradient_rule(width: int | None = None, *, env=None) -> str:
    """黄→蓝渐变长线（显示宽 = width 列）；非真彩回退纯黄；NO_COLOR 纯文本。"""
    width = terminal_width() if width is None else max(MIN_TERMINAL_WIDTH, width)
    line = "─" * width
    if not color_enabled(env):
        return line
    if not truecolor_supported(env):
        return f"{YELLOW}{line}{RESET}"
    parts = []
    steps = max(1, width - 1)
    for i in range(width):
        ratio = i / steps
        r, g, b = (round(a + (z - a) * ratio)
                   for a, z in zip(GRADIENT_YELLOW, GRADIENT_BLUE, strict=True))
        parts.append(f"\x1b[38;2;{r};{g};{b}m─")
    return "".join(parts) + RESET


def box_rules(label: str, color: str, width: int | None = None) -> tuple[str, str]:
    """消息块上下框线：上 `╭─ {label} …╮`（右侧补 ─ 至行尾收 ╮），下 `╰…╯`。

    label 按显示单元计宽（CJK 名字不错位）；宽度过小时退化为截断标签，不溢出。
    """
    width = terminal_width() if width is None else max(MIN_TERMINAL_WIDTH, width)
    head = f"╭─ {label} "
    head = clip_to_width(head, max(4, width - 1))
    top = head + "─" * max(0, width - visible_width(head) - 1) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    if not color_enabled():
        return top, bottom
    return (f"{color}{FRAME_BG}{top}{RESET}",
            f"{color}{FRAME_BG}{bottom}{RESET}")


def format_elapsed(seconds: float) -> str:
    """时间栏格式 `9h59m59s`：小时不补零、分秒两位（§6④）。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def process_symbol(tool_name: str) -> str:
    """§10 过程符号：走代码给代码符号、走工具给工具符号。"""
    lowered = tool_name.lower()
    if any(key in lowered for key in ("code", "write", "edit", "patch")):
        return CODE_SYMBOL
    return TOOL_SYMBOL
