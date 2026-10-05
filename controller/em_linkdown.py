"""
#354: the shape of a control-plane blip, and what may run inside it.

A control connection that drops used to pop its Device out of the registry
immediately while the four service releases waited out
CONTROL_RECONNECT_GRACE_S (#315 / PR #320). For the length of that five-second
window the device is ABSENT from `_devices` but its ESPHome satellite is
still registered with Home Assistant and still accepts turns.

A turn HA starts in that window runs against closed sockets. Nothing crashes
— `send_control` logs and swallows, `send_data` takes the no-data-connection
path — but no microphone frames arrive, FIRST_AUDIO_GRACE expires, and the
turn is persisted as **`no_speech`**: a turn that never had a chance,
recorded against a user who did not speak. It is the failure
`pipeline_refused` exists to avoid, and it pollutes the statistic wake
diagnosis leans on.

This module owns the decision, pure, for the reason em_barge and
em_linkauth are pure: the suite cannot import em_controller, so a decision
left inside it is a decision with no coverage.
"""

from __future__ import annotations


def link_down(*, in_registry: bool, link_down_since: float | None) -> bool:
    """
    Whether this device's control link is currently down.

    `in_registry` is `_devices.get(device_id) is device` — the same identity
    check the close path and the grace task already use, so this cannot
    disagree with them about who owns the device.

    `link_down_since` is the stamp the close path left. A device popped for
    the grace is down but COMING BACK; a device with no stamp was never here.
    Either way the turn cannot run, because its sockets are closed either way.
    """
    return (not in_registry) or link_down_since is not None


def refusal_outcome(*, had_satellite: bool) -> str:
    """
    The outcome recorded for a turn refused inside a blip.

    NOT `no_speech` — that outcome is persisted, so using it here would put
    every HA-side refusal during a blip into the activity stats as a silent
    user, which is the whole of #354. `pipeline_refused` says what happened:
    the pipeline could not be entered, because the Echo that would have fed
    it has no link.
    """
    return "pipeline_refused" if had_satellite else "no_ha"
