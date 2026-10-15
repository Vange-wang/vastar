"""B27 execution and network grants; independent of file-write permission C."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessPolicy:
    execution: bool = False
    network: bool = False
    mode: str = "appcontainer"

    @classmethod
    def from_config(cls, config):
        values = config.get("execution", {})
        for key in ("enabled", "network"):
            if type(values.get(key, False)) is not bool:
                raise ValueError(f"execution.{key} must be boolean")
        mode = values.get("mode", "appcontainer")
        if mode not in ("appcontainer", "disabled"):
            raise ValueError("unsupported execution mode: 非沙箱; execution refused")
        return cls(values.get("enabled", False), values.get("network", False), mode)

    def authorize(self, *, approved: bool):
        if not self.execution or self.mode != "appcontainer":
            raise PermissionError("D command execution is disabled")
        if self.network:
            raise PermissionError("C network grant recorded but this backend supports offline only")
        if approved is not True:
            raise PermissionError("command approval required; auto does not grant execution")

    def capabilities(self):
        return {"D_execution": self.execution, "C_network": self.network,
                "execution_effective": self.execution and self.mode == "appcontainer" and not self.network,
                "network_effective": False, "mode": self.mode,
                "approval_required": True, "file_access": "read_only",
                "blocked_reason": "online backend unavailable" if self.network else ""}
