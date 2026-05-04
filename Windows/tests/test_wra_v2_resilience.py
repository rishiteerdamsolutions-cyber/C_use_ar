from __future__ import annotations

from cusear.engine.session_steps import SessionSteps
from cusear.engine.workflow import elements_match, nearest_checkpoint_index, score_anchor
from cusear.engine.lucky import Lucky


def test_score_anchor_quality_strong_id() -> None:
    a = {"tagName": "div", "text": "Post", "id": "btn123", "className": "", "role": "button"}
    assert score_anchor(a) == "STRONG"


def test_elements_match_weak_ignores_text() -> None:
    expected = {"tagName": "div", "text": "What's on your mind, John?", "role": "button", "anchor_quality": "WEAK"}
    actual = {"tagName": "div", "text": "What's on your mind, Aditya?", "role": "button"}
    assert elements_match(expected, actual) is True


def test_elements_match_medium_requires_role_when_present() -> None:
    expected = {"tagName": "div", "text": "Post", "role": "button", "anchor_quality": "MEDIUM"}
    actual_bad = {"tagName": "div", "text": "Post", "role": "textbox"}
    assert elements_match(expected, actual_bad) is False


def test_nearest_checkpoint_index() -> None:
    steps = [
        {"step": 1, "action_type": "open_url"},
        {"step": 2, "action_type": "press_tab"},
        {"step": 3, "action_type": "press_enter", "is_checkpoint": True, "checkpoint_name": "composer_open"},
        {"step": 4, "action_type": "press_tab"},
        {"step": 5, "action_type": "type_text"},
    ]
    assert nearest_checkpoint_index(steps, before_index=5) == 2
    assert nearest_checkpoint_index(steps, before_index=3) == 2


def test_session_steps_replace_step_with_alternate_renumbers() -> None:
    base = [
        {"step": 1, "action_type": "press_tab", "tab_count": 1},
        {"step": 2, "action_type": "press_tab", "tab_count": 99},
        {"step": 3, "action_type": "press_enter"},
    ]
    session = SessionSteps(base)
    alternate = [
        {"action_type": "press_tab", "tab_count": 2},
        {"action_type": "press_tab", "tab_count": 3},
    ]
    session.replace_step_with_alternate(1, alternate)
    steps = session.snapshot()
    assert [s["step"] for s in steps] == [1, 2, 3, 4]
    assert steps[1]["tab_count"] == 2
    assert steps[2]["tab_count"] == 3


def test_lucky_abort_threshold_is_two_permanent_mismatches(monkeypatch) -> None:
    # Build a workflow with anchors on 3 steps. We'll force 2 permanent mismatches.
    workflow = {
        "workflow_name": "WRA_Test",
        "steps": [
            {"step": 1, "action_type": "press_tab", "tab_count": 1, "focus_target": {"tagName": "div", "text": "A", "role": "button", "anchor_quality": "MEDIUM"}},
            {"step": 2, "action_type": "press_tab", "tab_count": 1, "focus_target": {"tagName": "div", "text": "B", "role": "button", "anchor_quality": "MEDIUM"}},
            {"step": 3, "action_type": "press_tab", "tab_count": 1, "focus_target": {"tagName": "div", "text": "C", "role": "button", "anchor_quality": "MEDIUM"}},
        ],
    }

    lucky = Lucky(logs_dir="logs/lucky")

    # Avoid real keypresses/sleeps.
    monkeypatch.setattr(lucky, "_exec_step", lambda _step: None)
    monkeypatch.setattr(lucky, "_refresh", lambda: None)
    monkeypatch.setattr(lucky, "_re_navigate_to_index", lambda _steps, _idx: None)

    # Simulate: step1 MATCH, step2 PERMANENT, step3 PERMANENT -> ABORT at second permanent
    verdicts = iter(
        [
            ("MATCH", {}, {}),
            ("PERMANENT", {"tagName": "div"}, {"tagName": "span"}),
            ("PERMANENT", {"tagName": "div"}, {"tagName": "span"}),
        ]
    )
    monkeypatch.setattr(lucky, "_validate_step_anchor", lambda _step, _steps, _idx: next(verdicts))

    report = lucky.run(workflow)
    assert report.signal == "ABORT"
    assert len(report.drift_map) == 2

