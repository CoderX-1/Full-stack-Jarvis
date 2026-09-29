"""Device-control contract and offline test double; no real devices are paired here.

An adapter must identify the same device before and after an action. The hub
never reports success merely because a command was accepted by a transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeviceState:
    identity: str
    capability: str
    value: str
    revision: int
    online: bool = True


class DeviceAdapter(Protocol):
    def observe(self, identity: str, capability: str) -> DeviceState: ...

    def set_value(self, identity: str, capability: str, value: str) -> None: ...


class DeviceHub:
    """Allowlisted routing with observed-state verification, never blind replay."""

    def __init__(self) -> None:
        self._devices: dict[str, tuple[DeviceAdapter, frozenset[str]]] = {}

    def pair(self, identity: str, adapter: DeviceAdapter, capabilities: set[str]) -> None:
        if not identity or not identity.isascii() or len(identity) > 100:
            raise ValueError("device identity must be a short ASCII identifier")
        if identity in self._devices:
            raise ValueError("device identity already paired")
        allowed = frozenset(capabilities)
        if not allowed or any(not item or len(item) > 50 for item in allowed):
            raise ValueError("device capabilities must be explicit")
        self._devices[identity] = (adapter, allowed)

    def status(self) -> str:
        if not self._devices:
            return "No phone or physical device is paired. Real device control is unavailable."
        lines = []
        for identity, (_, capabilities) in sorted(self._devices.items()):
            lines.append(f"{identity}: {', '.join(sorted(capabilities))}")
        return "Paired device contracts (availability is checked per action):\n" + "\n".join(lines)

    def set_value(self, identity: str, capability: str, value: str) -> str:
        if identity not in self._devices:
            return "error: device is not paired; no action sent"
        adapter, capabilities = self._devices[identity]
        if capability not in capabilities:
            return "error: capability is not allowed for this device; no action sent"
        try:
            before = adapter.observe(identity, capability)
            if (before.identity, before.capability) != (identity, capability):
                return "error: device identity mismatch before action; no action sent"
            if not before.online:
                return "error: device is offline; no action sent"
            adapter.set_value(identity, capability, value)
            after = adapter.observe(identity, capability)
        except Exception as exc:
            return f"unknown: device action or verification failed ({type(exc).__name__}); inspect device state before retry"
        if (after.identity, after.capability) != (identity, capability):
            return "unknown: device identity changed during verification; do not retry blindly"
        if not after.online:
            return "unknown: device went offline during verification; do not retry blindly"
        if after.value != value or after.revision <= before.revision:
            return "unverified: requested state was not observed as a new revision; do not retry blindly"
        return f"Verified {identity} {capability}={value} at revision {after.revision}."


class SimulatedDeviceAdapter:
    """Deterministic offline test double; never used as evidence of real control."""

    def __init__(self, identity: str, capability: str, value: str = "off") -> None:
        self.state = DeviceState(identity, capability, value, 0)
        self.accept_commands = True

    def observe(self, identity: str, capability: str) -> DeviceState:
        return self.state

    def set_value(self, identity: str, capability: str, value: str) -> None:
        if self.accept_commands:
            self.state = DeviceState(identity, capability, value, self.state.revision + 1)
