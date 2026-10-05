"""
#354: a control-plane blip, and what may run inside it.

Pure decision tests, for the reason em_barge and em_linkauth are pure: the CI
suite cannot import em_controller, so a decision left in it is a decision with
no coverage. The source-shape guard that the stamp and the refusal exist lives
in test_control_grace.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_linkdown


def test_a_connected_device_may_run_a_turn():
    assert not em_linkdown.link_down(in_registry=True, link_down_since=None)


def test_a_device_popped_for_its_grace_may_not():
    # The window this exists for: out of the registry, satellite still up.
    assert em_linkdown.link_down(in_registry=False, link_down_since=12.5)


def test_a_device_stamped_but_still_registered_may_not():
    """Either signal alone is enough. The registry check is identity, so a
    replacement registering does not clear the OLD object's stamp — and the
    old object is exactly the one a turn would be started on."""
    assert em_linkdown.link_down(in_registry=True, link_down_since=12.5)


def test_a_device_that_never_registered_is_down():
    assert em_linkdown.link_down(in_registry=False, link_down_since=None)


def test_a_refusal_inside_a_blip_is_pipeline_refused_not_no_speech():
    """
    The whole point. `no_speech` is persisted and reads as a user who said
    nothing; during a blip the pipeline was never entered because the Echo
    had no link. Filing it as no_ha or no_speech would put a working Home
    Assistant integration into the activity stats under a fault it does not
    have — and the statistic wake diagnosis leans on is exactly this one.
    """
    assert em_linkdown.refusal_outcome(had_satellite=True) == "pipeline_refused"


def test_a_refusal_with_no_satellite_is_still_no_ha():
    """Nothing behind the Echo at all is the other fault, and keeps its own
    outcome and its own ring cue."""
    assert em_linkdown.refusal_outcome(had_satellite=False) == "no_ha"
