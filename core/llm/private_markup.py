"""Streaming recognition of private dialect envelopes, never a tool-call parser.

Dialect identifiers are arbitrary. New delimiter families require a data entry,
not another vendor-name blacklist. Literal backtick code and escaped spellings
are documentation; the legacy reserved-tag guard remains independently active.
"""
import re

PRIVATE_FORMS = (("｜｜", "｜｜"), ("||", "||"))
CALL_ROLES = ("calls", "tool_calls")
INVOKE_ROLES = ("invoke", "function_call")
PARAMETER_ROLES = ("parameter", "argument")
_ATTR = re.compile(r"\b([\w.-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s'\"/>]+)")


class PrivateMarkupGuard:
    """Quarantine a possible envelope until structure distinguishes it from prose."""

    def __init__(self):
        self.forms = tuple((opening, re.compile(
            rf"<\s*(?P<close>/?)\s*(?P<dialect>{re.escape(opening)}[\w.-]+{re.escape(ending)})"
            r"\s+(?P<role>\w+)\b(?P<attrs>[^<>]*)>", re.I))
            for opening, ending in PRIVATE_FORMS)
        self.probe = ""
        self.held = []
        self.header = ""
        self.anchor = None
        self.container = self.invocation = self.parameter = self.blocked = False
        self.ticks = self.code_ticks = 0
        self.escaped = False

    def _literal(self, char):
        escaped = self.escaped
        if char == "`" and not escaped:
            self.ticks += 1
        elif self.ticks:
            if self.code_ticks == 0:
                self.code_ticks = self.ticks
            elif self.code_ticks == self.ticks:
                self.code_ticks = 0
            self.ticks = 0
        self.escaped = char == "\\" and not escaped
        return escaped or char == "`" or self.code_ticks != 0

    def _tag(self, text):
        for _, pattern in self.forms:
            match = pattern.fullmatch(text)
            if match:
                return (match["dialect"].casefold(), match["role"].casefold(),
                        bool(match["close"]), any(name.casefold() == "name" and value.strip("\"'")
                        for name, value in _ATTR.findall(match["attrs"])))
        return None

    def _possible(self):
        body = self.probe[1:].lstrip().removeprefix("/").lstrip()
        return any(opening.startswith(body) or body.startswith(opening)
                   for opening, _ in self.forms)

    def _observe(self, tag):
        _, role, closing, named = tag
        if closing:
            return
        self.container |= role in CALL_ROLES
        self.invocation |= role in INVOKE_ROLES and named
        self.parameter |= role in PARAMETER_ROLES and named
        # Two related directive roles plus a nonempty argument/call name are
        # control structure. A dialect token or a single mentioned tag is not.
        self.blocked = self.invocation and (self.container or self.parameter)

    def _envelope(self, char, literal):
        self.held.append(char)
        if char == "<" and not literal:
            self.header = "<"
        elif self.header:
            self.header += char
        if not self.header.endswith(">"):
            return ""
        tag, self.header = self._tag(self.header), ""
        if tag:
            self._observe(tag)
            if self.blocked:
                self.held.clear()
            elif tag[2] and tag[:2] == self.anchor:
                text = "".join(self.held)
                self.held.clear()
                self.anchor = None
                self.container = self.invocation = self.parameter = False
                return text
        return ""

    def feed(self, text):
        visible = []
        for char in text:
            if self.blocked:
                break
            literal = self._literal(char)
            if self.anchor:
                visible.append(self._envelope(char, literal))
            elif self.probe or (char == "<" and not literal):
                self.probe += char
                if char == ">":
                    tag = self._tag(self.probe)
                    if tag and not tag[2] and tag[1] in CALL_ROLES + INVOKE_ROLES:
                        self.anchor = tag[:2]
                        self.held.append(self.probe)
                        self._observe(tag)
                    else:
                        visible.append(self.probe)
                    self.probe = ""
                elif not self._possible():
                    restart = self.probe.endswith("<")
                    visible.append(self.probe[:-1] if restart else self.probe)
                    self.probe = "<" if restart else ""
            else:
                visible.append(char)
        return "".join(visible)

    def tail(self):
        # A truncated nested directive is still control; never release it as an answer.
        partial = self._tag(self.header + ">") if self.header else None
        if self.anchor and partial:
            self._observe(partial)
        text = "".join(self.held) + self.probe
        self.held.clear()
        self.probe = ""
        return "" if self.blocked else text
