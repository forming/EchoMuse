"""
#315: a control-plane drop used to tear down a device's services
immediately — HA entities deregistered, BLE proxy dropped, media session
killed — and rebuilt all of it when the device returned seconds later.
The data plane has had DATA_RECONNECT_GRACE_S for exactly this reason;
the control plane never got the equivalent.

em_controller is deliberately not importable here (see conftest); these
are shape guards on the shipped source.
"""

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _nodes(stmt):
    """Every AST node under stmt, skipping the list-valued fields.

    ast.walk and ast.dump both raise on a statement node's handler/else lists,
    so the traversal is done by hand.
    """
    yield stmt
    for _, val in ast.iter_fields(stmt):
        for node in (val if isinstance(val, list) else (val,)):
            if isinstance(node, ast.AST):
                yield from _nodes(node)


def _dump(stmt) -> str:
    return " ".join(ast.dump(n) for n in _nodes(stmt))


def _tree():
    return ast.parse((CONTROLLER / "em_controller.py").read_text())


def _handle_control_finally():
    """The `finally` block of handle_control, as a node."""
    for n in _nodes(_tree()):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_control":
            for child in ast.walk(n):
                if isinstance(child, ast.Try) and child.finalbody:
                    return child
    raise AssertionError("no try/finally in handle_control")


def _branch(pred) -> ast.AST:
    """The statement under handle_control's `finally` matching pred.

    The teardown is one `if device:` whose last branch is the
    stale-check if/else, so the search has to reach the `else` arm as well as
    the top-level statements.
    """
    for stmt in _handle_control_finally().finalbody:
        if pred(stmt):
            return stmt
        for field in ("body", "orelse"):
            for sub in getattr(stmt, field, []) or []:
                if pred(sub):
                    return sub
    raise AssertionError("expected statement not found in handle_control's finally")


def test_teardown_is_deferred_not_immediate():
    """
    The four service releases must not run on the close path; they belong in
    the grace task.

    Asked structurally rather than as a slice of the source: the previous
    version read 1,200 characters from the log line and asserted the spawn sat
    inside the window, which is a claim about formatting. Adding two lines of
    comment — which is what #354's change did — pushed the spawn past it and
    failed a build whose behaviour was correct.
    """
    close = _branch(lambda s: "Device disconnected" in _dump(s))
    dumped = _dump(close)
    spawn = dumped.index("spawn")

    for call in ("esphome.device_disconnected", "em_ble_proxy.device_disconnected",
                 "device_gone", "notify_device_disconnected"):
        assert call not in dumped[:spawn], (
            f"{call} must move into the grace task, not run on close"
        )
    assert "_release_device_services" in dumped[spawn:], \
        "the close path must hand over to the grace task"


def test_the_grace_task_checks_for_a_replacement():
    task = next(n for n in _nodes(_tree())
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_release_device_services")
    dumped = _dump(task)
    assert "CONTROL_RECONNECT_GRACE_S" in dumped
    # ast.dump renders the call as Call(func=Attribute(attr='get')), so match
    # the pieces rather than the source spelling.
    assert "attr='get'" in dumped and "id='_devices'" in dumped, \
        "the task must check whether a replacement registered"
    # ast.dump renders a call as Call(func=Attribute(value=Name(id='esphome'),
    # attr='device_disconnected')), so match the module and the attribute
    # rather than the dotted spelling the source uses.
    for module, attr in (("api", "notify_device_disconnected"),
                         ("esphome", "device_disconnected"),
                         ("em_ble_proxy", "device_disconnected"),
                         ("em_player", "device_gone")):
        assert f"id='{module}'" in dumped and f"attr='{attr}'" in dumped, \
            f"{module}.{attr} belongs in the deferred release"


def test_the_grace_window_exists_and_is_documented():
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "CONTROL_RECONNECT_GRACE_S" in src
    # The data-plane constant this mirrors:
    assert "DATA_RECONNECT_GRACE_S = 3.0" in src


def test_a_blip_stamps_the_link_down_before_the_pop():
    """
    #354: the grace must be VISIBLE while the device is out of the registry,
    or nothing can tell a turn refused mid-blip from a turn that never had a
    device. The stamp is what em_linkdown reads.
    """
    close = _branch(lambda s: "Device disconnected" in _dump(s))
    dumped = _dump(close)
    assert "link_down_since" in dumped, (
        "the close path must stamp link_down_since so a turn starting inside "
        "the grace can be refused instead of run against closed sockets"
    )
    assert dumped.index("link_down_since") < dumped.index("attr='pop'"), (
        "stamp before the pop: after it, the device is already gone and the "
        "stamp never lands on anything the turn path can read"
    )


def test_a_conversation_started_inside_a_blip_is_refused():
    """
    The bug itself. HA can start a conversation during the grace, because the
    satellite outlives the control connection; the turn then runs against
    closed sockets, hears nothing, and is persisted as `no_speech` — a silent
    user for a turn that never had a chance.

    Without this guard the refusal can be deleted and the whole suite still
    passes, which is what a first pass at this change actually found.
    """
    refused = False
    for n in _nodes(_tree()):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "_start_conversation":
            continue
        body = _dump(n)
        refused = "link_down" in body and "pipeline_refused" in body
        # And it must come before the turn, not after it has been started.
        assert body.index("link_down") < body.index("_run_voice_locked"), (
            "the refusal must come before _run_voice_locked — after it, the "
            "turn has already run and recorded itself"
        )
    assert refused, (
        "_start_conversation must refuse while the control link is down; "
        "without it a turn runs against closed sockets and is persisted as "
        "no_speech"
    )


def test_the_stand_down_asks_whether_the_device_is_the_registry_entry():
    """
    The link signal is the same identity check the close path and the grace
    task use, so no two of them can disagree about who owns the device.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    assert src.count("linked=_devices.get(device.device_id) is device") >= 2, (
        "the arbitration and stand-down call sites must pass the registry "
        "identity as the link signal, not a flag of their own"
    )
    assert src.count("can_serve_turn") >= 6, (
        "can_serve_turn gained a `linked` argument (#354); a call site left "
        "without it defaults to True and stands a blipping Echo down for "
        "nothing"
    )


def test_the_stale_connection_guard_survives():
    """
    The 2026-07-14 guard solves a different ordering problem (close arriving
    AFTER a replacement registered) and must stay untouched.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # the message wraps across two f-string lines — match the fragments
    assert "replacement is active" in src and "services up" in src, \
        "the out-of-order stale guard is still needed alongside the grace"
