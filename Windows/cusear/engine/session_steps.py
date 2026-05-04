from __future__ import annotations

import threading
from typing import Any

from .workflow import WorkflowStep, insert_extra_shift_tab, insert_extra_tab


class SessionSteps:
    """
    Thread-safe wrapper around the session clone steps list.

    Agami may insert steps while AHA is executing. A raw Python list is not safe
    to mutate during concurrent iteration, so all access/mutation must be
    serialized through this wrapper.
    """

    def __init__(self, steps: list[WorkflowStep]) -> None:
        self._steps = steps
        self._lock = threading.RLock()

    def lock(self) -> threading.RLock:
        return self._lock

    def __len__(self) -> int:
        with self._lock:
            return len(self._steps)

    def get(self, index: int) -> WorkflowStep:
        with self._lock:
            return self._steps[index]

    def snapshot(self) -> list[WorkflowStep]:
        with self._lock:
            return list(self._steps)

    def insert_extra_tab_at(self, index: int) -> None:
        with self._lock:
            insert_extra_tab(self._steps, index)

    def insert_extra_shift_tab_at(self, index: int) -> None:
        with self._lock:
            insert_extra_shift_tab(self._steps, index)

    def to_list(self) -> list[WorkflowStep]:
        return self._steps

    def replace_step_with_alternate(self, index: int, alternate_steps: list[WorkflowStep]) -> None:
        """
        Replace the step at `index` with `alternate_steps` (flow fork).
        """
        from .workflow import renumber_steps

        with self._lock:
            # Remove current step
            self._steps.pop(index)
            # Insert alternates (mark them as inserted_by agami)
            insert_at = index
            for s in alternate_steps:
                step = dict(s)
                step.setdefault("wait", 0.0)
                step.setdefault("focus_target", None)
                step["inserted_by"] = "agami_alternate"
                self._steps.insert(insert_at, step)
                insert_at += 1
            renumber_steps(self._steps, start_at=1)

