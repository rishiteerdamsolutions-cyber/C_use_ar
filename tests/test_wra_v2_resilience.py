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


def test_elements_match_intent_alias_publish_post() -> None:
    expected = {"tagName": "button", "text": "Post", "role": "button", "intent": "publish_post", "anchor_quality": "MEDIUM"}
    actual = {"tagName": "button", "text": "Share now", "role": "button"}
    assert elements_match(expected, actual) is True


def test_elements_match_anchor_bundle_fallback() -> None:
    expected = {
        "tagName": "button",
        "text": "Post",
        "role": "button",
        "anchor_quality": "STRONG",
        "anchor_bundle": [
            {"tagName": "button", "text": "Publish", "role": "button", "anchor_quality": "STRONG"},
            {"tagName": "button", "text": "Share", "role": "button", "anchor_quality": "STRONG"},
        ],
    }
    actual = {"tagName": "button", "text": "Share", "role": "button"}
    assert elements_match(expected, actual) is True


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


def test_session_steps_insert_extra_tab_after_preserves_current_step() -> None:
    base = [
        {"step": 1, "action_type": "press_tab", "tab_count": 1},
        {"step": 2, "action_type": "type_text", "value": "hello"},
    ]
    session = SessionSteps(base)
    session.insert_extra_tab_after(0)
    steps = session.snapshot()
    assert [s["action_type"] for s in steps] == ["press_tab", "press_tab", "type_text"]


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


def test_lucky_aha_execute_only_is_non_blocking(monkeypatch) -> None:
    workflow = {
        "workflow_name": "WRA_AHA_ONLY",
        "steps": [
            {
                "step": 1,
                "action_type": "press_enter",
                "aha_execute_only": True,
                "focus_target": {"tagName": "button", "text": "Post", "role": "button", "anchor_quality": "MEDIUM"},
            },
            {
                "step": 2,
                "action_type": "wait",
                "duration": 0.0,
            },
        ],
    }

    lucky = Lucky(logs_dir="logs/lucky")

    # Force mismatch for probe, but it should stay non-blocking.
    monkeypatch.setattr(lucky._os, "capture_active_element", lambda: {"tagName": "div", "text": "Other", "role": "textbox"})
    monkeypatch.setattr(lucky, "_refresh", lambda: None)

    report = lucky.run(workflow)
    assert report.signal == "GREEN"
    assert len(report.drift_map) == 1
    assert report.drift_map[0].get("non_blocking_observation") is True


def test_lucky_report_contains_decision_fields(monkeypatch) -> None:
    workflow = {
        "workflow_name": "WRA_Decision",
        "steps": [{"step": 1, "action_type": "wait", "duration": 0.0}],
    }
    lucky = Lucky(logs_dir="logs/lucky")
    monkeypatch.setattr(lucky, "_refresh", lambda: None)
    report = lucky.run(workflow)
    assert report.go_decision in {"GO", "GO_WITH_CAUTION", "BLOCK"}
    assert 0.0 < float(report.confidence_score) <= 1.0


def test_linkedin_probe_one_tab_reaches_post() -> None:
    from cusear.engine.agami import linkedin_probe_extra_tab_count_to_target

    post = {"tagName": "button", "text": "Post", "role": "button", "anchor_quality": "MEDIUM"}
    banner = {"tagName": "a", "text": "Try Premium", "role": "link", "anchor_quality": "MEDIUM"}
    idx = {"i": 0}
    states = [banner, post]

    def capture() -> dict:
        return dict(states[idx["i"]])

    def press_tab() -> None:
        idx["i"] = min(1, idx["i"] + 1)

    def press_shift_tab() -> None:
        idx["i"] = max(0, idx["i"] - 1)

    n = linkedin_probe_extra_tab_count_to_target(
        capture=capture,
        tgt=post,
        max_probe=1,
        press_tab=press_tab,
        press_shift_tab=press_shift_tab,
        sleep_after_motion=lambda: None,
    )
    assert n == 1
    assert idx["i"] == 0


def test_linkedin_probe_already_on_post() -> None:
    from cusear.engine.agami import linkedin_probe_extra_tab_count_to_target

    post = {"tagName": "button", "text": "Post", "role": "button", "anchor_quality": "MEDIUM"}

    calls: list[str] = []

    def capture() -> dict:
        return dict(post)

    def press_tab() -> None:
        calls.append("tab")

    def press_shift_tab() -> None:
        calls.append("shift_tab")

    n = linkedin_probe_extra_tab_count_to_target(
        capture=capture,
        tgt=post,
        max_probe=1,
        press_tab=press_tab,
        press_shift_tab=press_shift_tab,
        sleep_after_motion=lambda: None,
    )
    assert n == 0
    assert calls == []


def test_linkedin_probe_no_match_restores_focus() -> None:
    from cusear.engine.agami import linkedin_probe_extra_tab_count_to_target

    post = {"tagName": "button", "text": "Post", "role": "button", "anchor_quality": "MEDIUM"}
    wrong = {"tagName": "div", "text": "Editor", "role": "textbox", "anchor_quality": "MEDIUM"}
    idx = {"i": 0}
    states = [wrong, wrong]

    def capture() -> dict:
        return dict(states[idx["i"]])

    def press_tab() -> None:
        idx["i"] = min(1, idx["i"] + 1)

    def press_shift_tab() -> None:
        idx["i"] = max(0, idx["i"] - 1)

    n = linkedin_probe_extra_tab_count_to_target(
        capture=capture,
        tgt=post,
        max_probe=1,
        press_tab=press_tab,
        press_shift_tab=press_shift_tab,
        sleep_after_motion=lambda: None,
    )
    assert n == 0
    assert idx["i"] == 0


def test_session_insert_extra_tab_before_final_enter_order() -> None:
    """Simulate Agami inserting one tab before final enter at index i."""
    from cusear.engine.session_steps import SessionSteps

    post_step = {
        "step": 2,
        "action_type": "press_enter",
        "is_final": True,
        "focus_target": {"tagName": "button", "text": "Post", "role": "button"},
    }
    session = SessionSteps([{"step": 1, "action_type": "wait", "duration": 0.0}, post_step])
    i = 1
    extra_tabs = 1
    for j in range(extra_tabs):
        session.insert_extra_tab_at(i + j)
    steps = session.snapshot()
    assert [s["action_type"] for s in steps] == ["wait", "press_tab", "press_enter"]
    assert steps[1].get("inserted_by") == "agami"
