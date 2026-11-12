"""Audio-idle countdown for the tray app. Pure logic, no OS calls."""
from dataclasses import dataclass

PLAYING = "playing"
SILENT = "silent"
DISCONNECTED = "disconnected"


@dataclass
class Tick:
    state: str              # PLAYING / SILENT / DISCONNECTED
    idle: int               # seconds of silence counted so far
    power_off: bool         # True exactly once when the idle limit is reached


class IdleTracker:
    """Counts silent seconds; asks for one power-off when the limit is reached.

    After a power-off it stops counting until the device drops (level < 0) or
    audio plays again, so it does not fire again and again.
    """

    def __init__(self):
        self.idle = 0
        self.suppress = False

    def reset(self) -> None:
        self.idle = 0

    def tick(self, level: float, *, threshold: float, poll_seconds: int,
             idle_minutes: int) -> Tick:
        if level < 0:
            self.suppress = False
            self.idle = 0
            return Tick(DISCONNECTED, 0, False)
        if level < threshold:
            fire = False
            if not self.suppress:
                self.idle += poll_seconds
                if self.idle >= idle_minutes * 60:
                    self.suppress = True
                    fire = True
            return Tick(SILENT, self.idle, fire)
        self.suppress = False
        self.idle = 0
        return Tick(PLAYING, 0, False)
