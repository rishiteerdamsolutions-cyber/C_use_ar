from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


WorkflowStep = dict[str, Any]
WorkflowJson = dict[str, Any]


def load_workflow(path: str) -> WorkflowJson:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_workflow(path: str, workflow: WorkflowJson) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(workflow, f, indent=2, ensure_ascii=False)


class WorkflowLiveEditor:
    """
    Persist AHA/Agami session edits (e.g. inserted press_tab steps) back to the saved workflow JSON.
    """

    def __init__(self, workflow_path: str, workflow: WorkflowJson) -> None:
        self.workflow_path = os.path.abspath(workflow_path)
        self._base = workflow
        self._lock = threading.Lock()

    def persist_session_steps(self, session_steps: list[WorkflowStep]) -> int:
        """Write current session steps to disk; returns count of Agami-inserted steps."""
        with self._lock:
            steps = clone_steps(list(session_steps))
            renumber_steps(steps, start_at=1)
            inserted = sum(
                1
                for s in steps
                if str(s.get("inserted_by") or "") in ("agami", "agami_alternate")
            )
            updated = dict(self._base)
            updated["steps"] = steps
            updated["aha_live_edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            updated["aha_self_heal_insertions"] = inserted
            save_workflow(self.workflow_path, updated)
            return inserted


def clone_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    return copy.deepcopy(steps)


_ALT_REF_KEYS = (
    "target_step",
    "return_to_step",
    "resume_step",
    "goto_step",
    "step_index",
)
_ALT_REF_NAME_KEYS = (
    "target_step_name",
    "return_to_step_name",
    "resume_step_name",
    "goto_step_name",
)


def ensure_step_ids(steps: list[WorkflowStep]) -> None:
    for s in steps:
        if not s.get("step_id"):
            s["step_id"] = uuid.uuid4().hex
        if not s.get("step_name") and s.get("checkpoint_name"):
            s["step_name"] = str(s.get("checkpoint_name") or "")


def _update_alternate_path_refs(
    steps: list[WorkflowStep],
    *,
    old_number_to_id: dict[int, str],
    id_to_new_number: dict[str, int],
    name_to_id: dict[str, str],
) -> None:
    for s in steps:
        alt_path = s.get("alternate_path")
        if not isinstance(alt_path, list):
            continue
        for alt in alt_path:
            if not isinstance(alt, dict):
                continue
            for key in _ALT_REF_KEYS:
                if key in alt and isinstance(alt[key], int):
                    step_id = old_number_to_id.get(int(alt[key]))
                    if step_id:
                        alt[f"{key}_id"] = step_id
            for key in _ALT_REF_NAME_KEYS:
                if key in alt and isinstance(alt[key], str):
                    step_id = name_to_id.get(str(alt[key]).strip())
                    if step_id:
                        alt[f"{key}_id"] = step_id

            for key in _ALT_REF_KEYS:
                id_key = f"{key}_id"
                if id_key in alt:
                    step_id = str(alt.get(id_key) or "").strip()
                    if step_id in id_to_new_number:
                        alt[key] = id_to_new_number[step_id]


def renumber_steps(steps: list[WorkflowStep], start_at: int = 1) -> None:
    ensure_step_ids(steps)
    old_number_to_id: dict[int, str] = {}
    for i, s in enumerate(steps):
        old_num = int(s.get("step", start_at + i))
        old_number_to_id[old_num] = str(s.get("step_id") or "")
    for i, s in enumerate(steps):
        s["step"] = start_at + i
    id_to_new_number = {str(s.get("step_id") or ""): int(s.get("step")) for s in steps}
    name_to_id = {str(s.get("step_name") or ""): str(s.get("step_id") or "") for s in steps if s.get("step_name")}
    _update_alternate_path_refs(
        steps,
        old_number_to_id=old_number_to_id,
        id_to_new_number=id_to_new_number,
        name_to_id=name_to_id,
    )


def insert_extra_tab(session_steps: list[WorkflowStep], at_index: int) -> None:
    """
    Insert a single extra tab step into the session clone.

    This is the fundamental "drift healing" operation: when a UI element has
    shifted forward in tab order, we add additional `press_tab` steps so AHA™
    stays aligned with the current UI.
    """
    extra: WorkflowStep = {
        "step": -1,
        "action_type": "press_tab",
        "tab_count": 1,
        "wait": 1.0,
        "focus_target": None,
        "inserted_by": "agami",
        "inserted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "insert_reason": "drift_heal",
    }
    session_steps.insert(at_index, extra)
    renumber_steps(session_steps, start_at=1)


def insert_extra_shift_tab(session_steps: list[WorkflowStep], at_index: int) -> None:
    """
    Insert a single Shift+Tab hotkey step into the session clone.

    Used for backward drift healing (element moved earlier in tab order).
    """
    extra: WorkflowStep = {
        "step": -1,
        "action_type": "hotkey",
        "keys": ["shift", "tab"],
        "wait": 0.0,
        "focus_target": None,
        "inserted_by": "agami",
        "inserted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    session_steps.insert(at_index, extra)
    renumber_steps(session_steps, start_at=1)


_INTENT_TEXT_ALIASES: dict[str, list[str]] = {
    "publish_post": ["post", "share", "publish", "tweet", "send", "next"],
    "open_composer": ["what's on your mind", "create post", "start a post", "compose", "new post"],
}


def _text_matches_with_intent(exp_text: str, act_text: str, expected: dict[str, Any]) -> bool:
    if not exp_text:
        return True
    if (exp_text in act_text) or (act_text in exp_text):
        return True

    intent = str(expected.get("intent") or expected.get("intent_key") or "").strip().lower()
    if not intent:
        return False
    aliases = _INTENT_TEXT_ALIASES.get(intent, [])
    if not aliases:
        return False

    exp_alias_hit = any(tok in exp_text for tok in aliases)
    act_alias_hit = any(tok in act_text for tok in aliases)
    return exp_alias_hit and act_alias_hit


def _elements_match_single(expected: dict | None, actual: dict | None) -> bool:
    if not expected or not actual:
        return False

    quality = str(expected.get("anchor_quality", "") or "").upper().strip()

    tag_ok = (expected.get("tagName", "").lower() == actual.get("tagName", "").lower())

    exp_text = (expected.get("text", "") or "").strip().lower()
    act_text = (actual.get("text", "") or "").strip().lower()

    # WEAK anchors: ignore text entirely (text may be personalized / volatile)
    if quality == "WEAK":
        exp_text = ""

    text_ok = _text_matches_with_intent(exp_text, act_text, expected)

    exp_role = (expected.get("role", "") or "").strip().lower()
    act_role = (actual.get("role", "") or "").strip().lower()

    role_ok = True
    if exp_role:
        # Partial match to survive small role strings on Windows UIA adapters
        role_ok = (exp_role in act_role) or (act_role in exp_role)

    if tag_ok and text_ok:
        # If role exists, require it for STRONG/MEDIUM to prevent false positives.
        if quality in ("STRONG", "MEDIUM") and exp_role and not role_ok:
            return False
        return True

    # Fallback: tag only when expected text missing
    if tag_ok and not exp_text:
        if quality in ("STRONG", "MEDIUM") and exp_role and not role_ok:
            return False
        return True

    return False


def anchor_has_validation_signal(anchor: dict | None) -> bool:
    """True when Lucky/Agami has something meaningful to compare (skip empty Rekky anchors)."""
    if not anchor or not isinstance(anchor, dict):
        return False
    if str(anchor.get("id") or "").strip():
        return True
    if str(anchor.get("tagName") or "").strip():
        return True
    if str(anchor.get("role") or "").strip():
        return True
    if len(str(anchor.get("text") or "").strip()) >= 2:
        return True
    bundle = anchor.get("anchor_bundle")
    if isinstance(bundle, list):
        return any(anchor_has_validation_signal(c) for c in bundle if isinstance(c, dict))
    return False


def elements_match(expected: dict | None, actual: dict | None) -> bool:
    if _elements_match_single(expected, actual):
        return True

    if not expected:
        return False
    bundle = expected.get("anchor_bundle")
    if not isinstance(bundle, list):
        return False
    for candidate in bundle:
        if isinstance(candidate, dict) and _elements_match_single(candidate, actual):
            return True
    return False


def score_anchor(anchor: dict[str, Any] | None) -> str:
    """
    Heuristic scoring at recording time.

    STRONG: has id OR (role + short text <= 20)
    MEDIUM: role present OR short-ish text <= 30
    WEAK  : long text > 30 or empty/unknown
    """
    if not anchor:
        return "WEAK"

    _id = str(anchor.get("id", "") or "").strip()
    role = str(anchor.get("role", "") or "").strip()
    text = str(anchor.get("text", "") or "").strip()

    if _id:
        return "STRONG"

    if role and text and len(text) <= 20:
        return "STRONG"

    if role or (text and len(text) <= 30):
        return "MEDIUM"

    return "WEAK"


def nearest_checkpoint_index(steps: list[WorkflowStep], before_index: int) -> int | None:
    """
    Return the nearest checkpoint step index strictly before `before_index`.
    """
    for j in range(before_index - 1, -1, -1):
        if steps[j].get("is_checkpoint"):
            return j
    return None


def _expand_press_tab(orig: WorkflowStep) -> list[WorkflowStep]:
    tc = max(1, int(orig.get("tab_count", 1)))
    inter = list(orig.get("intermediate_elements") or [])
    last_focus = dict(orig["focus_target"]) if orig.get("focus_target") else None
    out: list[WorkflowStep] = []
    ft_full = dict(orig["focus_target"]) if orig.get("focus_target") else None
    orig_id = str(orig.get("step_id") or "").strip()
    for j in range(tc):
        chunk = dict(orig)
        chunk["tab_count"] = 1
        chunk["intermediate_elements"] = [dict(inter[j])] if j < len(inter) else []
        if orig_id:
            chunk["step_id"] = f"{orig_id}:tab:{j + 1}"
        else:
            chunk.pop("step_id", None)
        ft = ft_full if j == tc - 1 and ft_full else None
        if ft is None and j < len(inter):
            cand = dict(inter[j])
            cand.pop("position", None)
            cand.pop("key", None)
            cand.setdefault("anchor_quality", score_anchor(cand))
            ft = cand
        elif ft is None and j == tc - 1 and last_focus:
            ft = dict(last_focus)
        if ft is not None:
            ft.pop("position", None)
            ft.pop("key", None)
        chunk["focus_target"] = ft
        if j > 0:
            chunk["is_checkpoint"] = False
            chunk["alternate_path"] = []
        elif not chunk.get("is_checkpoint"):
            chunk.setdefault("alternate_path", orig.get("alternate_path") or [])
        chunk.setdefault("_expanded_part", j)
        chunk.setdefault("_expanded_tabs_of", tc)
        out.append(chunk)
    return out


def _expand_count_action(
    orig: WorkflowStep,
    *,
    action_type: str,
    count_attr: str,
    key_builder: Callable[[int], str],
) -> list[WorkflowStep]:
    n = max(1, int(orig.get(count_attr, 1)))
    inter = list(orig.get("intermediate_elements") or [])
    ft_full = dict(orig["focus_target"]) if orig.get("focus_target") else None
    out: list[WorkflowStep] = []
    orig_id = str(orig.get("step_id") or "").strip()
    for j in range(n):
        chunk = dict(orig)
        chunk["action_type"] = action_type
        chunk[count_attr] = 1
        chunk["intermediate_elements"] = [dict(inter[j])] if j < len(inter) else []
        if orig_id:
            chunk["step_id"] = f"{orig_id}:{action_type}:{j + 1}"
        else:
            chunk.pop("step_id", None)
        ft = ft_full if j == n - 1 and ft_full else None
        if ft is None and j < len(inter):
            cand = dict(inter[j])
            cand.pop("position", None)
            cand.pop("key", None)
            cand.setdefault("anchor_quality", score_anchor(cand))
            ft = cand
        if ft is not None:
            ft.pop("position", None)
            ft.pop("key", None)
        chunk["focus_target"] = ft
        if j > 0:
            chunk["is_checkpoint"] = False
            chunk["alternate_path"] = []
        elif not chunk.get("is_checkpoint"):
            chunk.setdefault("alternate_path", orig.get("alternate_path") or [])
        # attach expected key tag for enrichment / debugging only
        kb = key_builder(j)
        chunk["_expanded_key_hint"] = kb
        chunk.setdefault("_expanded_part", j)
        out.append(chunk)
    return out


def expand_runtime_navigation_steps(session_steps: list[WorkflowStep]) -> None:
    """
    Expand multi-press navigation into one logical keypress per session step.

    Enables Agami ↔ AHA LANDED_N coordination and safe mid-run insert_extra_tab_at()
    without desynchronizing in-step indices.

    Mutates `session_steps` in place after replacing its contents.
    """
    expanded: list[WorkflowStep] = []
    for step in session_steps:
        action = str(step.get("action_type") or "").strip()

        if action == "press_tab" and int(step.get("tab_count", 1)) > 1:
            expanded.extend(_expand_press_tab(step))
            continue

        if action == "press_arrow" and int(step.get("count", 1)) > 1:
            expanded.extend(
                _expand_count_action(step, action_type="press_arrow", count_attr="count", key_builder=lambda j: "arrow")
            )
            continue

        if action == "press_escape" and int(step.get("count", 1)) > 1:

            def _ej(j: int) -> str:
                return "escape"

            expanded.extend(
                _expand_count_action(step, action_type="press_escape", count_attr="count", key_builder=_ej)
            )
            continue

        if action == "press_space" and int(step.get("count", 1)) > 1:

            def _sj(j: int) -> str:
                return "space"

            expanded.extend(
                _expand_count_action(step, action_type="press_space", count_attr="count", key_builder=_sj)
            )
            continue

        expanded.append(step)

    session_steps[:] = expanded
    renumber_steps(session_steps, start_at=1)


@dataclass(frozen=True)
class LuckyReport:
    signal: str  # GREEN | ABORT
    drift_map: list[dict[str, Any]]
    type_steps: list[int]
    total_rekky_steps: int
    total_lucky_steps: int
    global_drift_delta: int
    abort_reason: str
    timestamp: str
    total_keypresses_validated: int = 0
    total_mismatches: int = 0
    permanent_mismatches: int = 0
    temporary_mismatches: int = 0
    go_decision: str = "GO"
    confidence_score: float = 1.0
    severity_summary: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "drift_map": self.drift_map,
            "type_steps": self.type_steps,
            "total_rekky_steps": self.total_rekky_steps,
            "total_lucky_steps": self.total_lucky_steps,
            "global_drift_delta": self.global_drift_delta,
            "abort_reason": self.abort_reason,
            "timestamp": self.timestamp,
            "total_keypresses_validated": self.total_keypresses_validated,
            "total_mismatches": self.total_mismatches,
            "permanent_mismatches": self.permanent_mismatches,
            "temporary_mismatches": self.temporary_mismatches,
            "go_decision": self.go_decision,
            "confidence_score": self.confidence_score,
            "severity_summary": self.severity_summary or {},
        }

