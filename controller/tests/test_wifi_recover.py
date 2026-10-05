"""
#439: a device already in the field can clear Android's WiFi auto-join block.

#416 stops NEW provisions acquiring the fault and leaves every device
provisioned before it unable to fix it from the dashboard. The fault is that
the device stops joining: on a local-only network Android 5.1's
WifiAutoJoinController suppresses auto-join once
`num_no_internet_access_reports` climbs, and every successful connection is
reported as having no internet.

These are shape guards because em_api imports aiohttp and the CI suite cannot
import it. Asked structurally, not as a text window — a window is a claim about
formatting, which is how #754's tests broke on an unrelated line.
"""

import ast
import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]


def _nodes(stmt):
    yield stmt
    for _, val in ast.iter_fields(stmt):
        for node in (val if isinstance(val, list) else (val,)):
            if isinstance(node, ast.AST):
                yield from _nodes(node)


def _dump(stmt) -> str:
    return " ".join(ast.dump(n) for n in _nodes(stmt))


def _handler(name: str) -> ast.AST:
    tree = ast.parse((CONTROLLER / "em_api.py").read_text())
    for n in _nodes(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in em_api.py")


def test_the_recovery_is_reachable_without_an_ota():
    """
    The whole point. A device on the latest firmware is never OTA'd again, so
    the reconcile path cannot reach it — which is exactly the case this fault
    appears on, a device that has been up and rebooting for months.
    """
    api = (CONTROLLER / "em_api.py").read_text()
    assert '"/api/devices/{id}/wifi_recover"' in api, \
        "no manual wifi_recover endpoint registered"
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "/wifi_recover" in jsx, "the dashboard must be able to trigger it"


def test_it_refuses_on_a_device_that_is_not_running_android():
    """
    emOS drives wpa_supplicant directly: no `settings` binary, no
    networkHistory.txt, no WifiAutoJoinController. A dashboard-only rule
    protects nothing here — it is a plain POST with a session token.
    """
    body = _dump(_handler("_post_device_wifi_recover"))
    assert "android_userspace" in body, \
        "the endpoint must refuse on live.android_userspace, server-side"
    assert "not_android" in body, \
        "the refusal needs its own code so the dashboard can say why"


def test_it_says_the_reboot_is_required():
    """
    WifiStateMachine holds the network history in its Java layer, so deleting
    the file under a running framework is likely to be written back. The
    response and the button both have to say so — rebooting somebody's voice
    assistant is not a thing to do quietly.
    """
    assert "reboot_required" in _dump(_handler("_post_device_wifi_recover")), (
        "the response must carry reboot_required, or the operator reboots and "
        "wonders why nothing changed"
    )
    jsx = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    assert "REBOOT THE DEVICE" in jsx, \
        "the confirmation must state that a reboot is required"


def test_it_reads_the_counter_before_changing_anything():
    """
    The issue asks whether this can detect the fault rather than wait to be
    told. Reading the number answers that for one shell round trip and turns a
    button whose only output is "done" into one that says whether the device
    was affected. Reading it first also means an unaffected device reports 0
    rather than appearing to have needed the fix.
    """
    body = _dump(_handler("_post_device_wifi_recover"))
    read = body.index("num_no_internet_access_reports")
    write = body.index("captive_portal_detection_enabled")
    assert read < write, (
        "the counter must be read before anything is changed, or the report "
        "describes a device we have already fixed"
    )


def test_it_verifies_the_write_instead_of_trusting_it():
    """
    A command that reports success while doing nothing is the failure this
    whole area has — the destination-directory probe, TRANSFER_OK, the md5
    compare, the preflight probes. The provisioning wizard already refuses its
    run on exactly this check for the same `settings` key.
    """
    body = _dump(_handler("_post_device_wifi_recover"))
    # The GET of the same key has to come after the PUT, and it has to be a
    # different call: a read of the value before writing it proves nothing.
    put = "settings put global captive_portal_detection_enabled 0"
    get = "settings get global captive_portal_detection_enabled"
    assert put in body and get in body, (
        "both the write and its read-back must be present; the wizard's "
        "provisioning step fails its run on exactly this check for the same key"
    )
    assert body.index(get) > body.index(put), \
        "the setting must be read back after it is written"
    assert "wifi_recover_unverified" in body, \
        "an unverifiable write must fail the call rather than report success"


def test_the_commands_carry_no_user_text():
    """
    Nothing from a request reaches these. Asserted because the rule is
    absolute and the endpoint is the kind of place it gets relaxed: a WiFi
    SSID interpolated into one of these would be a shell injection.
    """
    body = _dump(_handler("_post_device_wifi_recover"))
    # Nothing from the request body or query may be read at all. The routed
    # device_id IS allowed — it is looked up in _devices to find the live
    # Device, never interpolated into a command — and the command set check
    # below is what proves that.
    for bad in ("query", "json", "rel_url", "headers", "POST", "matchdict"):
        hits = [i for i in range(len(body))
                if body.startswith(f"attr='{bad}'", i) or body.startswith(f"id='{bad}'", i)]
        assert not hits, (
            f"{bad} must not be read here — the SSID and the device label are "
            "attacker-influenced in a support scenario"
        )
    # Every shell command is one fixed literal, so there is nothing to
    # interpolate into and nothing for a caller to reach. Counted distinct:
    # the dump repeats nodes, and the set is the claim worth making.
    commands = set(re.findall(r'Constant\(value="(su -c[^"]*)"\)', body))
    assert commands == {
        "su -c 'settings get global num_no_internet_access_reports'",
        "su -c 'rm -f /data/misc/wifi/networkHistory.txt'",
        "su -c 'settings put global captive_portal_detection_enabled 0'",
        "su -c 'settings get global captive_portal_detection_enabled'",
    }, f"unexpected shell commands: {sorted(commands)}"
