"""
em_announce.py — running an HA announcement to completion.

`VoiceAssistantAnnounceFinished` is Home Assistant's completion signal, and HA
BLOCKS on it. `assist_satellite.entity.async_internal_announce` documents
`async_announce` as "should block until the announcement is done playing",
holds `_is_announcing` and the RESPONDING state for its duration, and raises
`SatelliteBusyError` if another announcement arrives meanwhile; the esphome
integration implements that by awaiting the reply through
`send_voice_assistant_announcement_await_response`.

Two rules follow, and they pull in opposite directions:

  * do not reply early — an early reply returns the service call while audio is
    still playing, drops the entity out of RESPONDING, and lets two chained
    announcements overlap on the device instead of queueing behind HA's guard;
  * always reply — a reply that never arrives parks HA for
    `_ANNOUNCEMENT_TIMEOUT_SEC` (5 minutes) holding `_is_announcing`, after
    which every announcement fails. `success=False` is strictly better than
    silence.

Split out of em_esphome so it can be tested: the controller test suite does not
import em_esphome (zeroconf, aiohttp, the database), and both of the rules
above are invisible at the call site — the code reads fine either way. The wait
for a playback callback (below) is here for the same reason.
"""

import asyncio
import inspect
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("echomuse.announce")

# Whole-announcement cap: fetch plus playback. Sized to sit well under HA's
# _ANNOUNCEMENT_TIMEOUT_SEC (5 minutes) so WE are the side that gives up and
# replies, rather than leaving HA holding _is_announcing.
#
# The layer below is already bounded (audio_duration * 2 + 10s in
# em_controller._run_post_turn_playback), which is what the layer below always
# looks like. The callback wait is INSIDE this cap rather than beside it, so
# the budget covers one announcement end to end and cannot be spent twice.
ANNOUNCE_TIMEOUT_S = 120.0

# How long an announcement waits for a playback callback to turn up, and how
# often it looks (#219).
#
# The window is the physical Dot's own `/control` connect racing HA's ESPHome
# TCP connect — two independent events on the LAN, in no order relative to each
# other — so it closes when `em_controller.device_connected()` sets
# `_standalone_play`. Both halves are LAN-local: ICMP from the Dot to the
# controller measures p50 5.5ms / max 16.8ms, and the app-layer RTT excursions
# this fleet does have cluster at 400–700ms and 1000–1400ms (TCP RTO 500–800ms
# under 4.6–7.1% loss), so the slow side of the race is two transmissions plus
# a handshake. Past that the Dot is not connecting at all — it is inside its own
# reconnect backoff (5s for the first two passes), and no wait reaches a device
# that is not dialling.
#
# 2.0s therefore covers the race including the loss case, and stops short of
# the backoff on purpose: a Dot inside a 5s backoff is undeliverable, and
# holding HA's _is_announcing that long to answer success=False about a device
# that is off costs more than it can ever recover.
#
# The poll is 50ms because the wait is an event-loop ordering, not a slow
# operation — the callback appears on a specific loop turn. 50ms is three
# orders of magnitude above a tick and adds that much at most to the common
# case, at ~40 wakeups for the whole 2s.
PLAY_CB_WAIT_S = 2.0
PLAY_CB_POLL_S = 0.05


async def wait_for_play_cb(
    get_cb: Callable[[], object],
    timeout: float = PLAY_CB_WAIT_S,
    poll_s: float = PLAY_CB_POLL_S,
    log_name: str = "",
) -> Optional[Callable]:
    """
    Read the playback callback, giving it a moment to turn up (#219).

    `get_cb` is read repeatedly rather than once. The caller hands it over
    because reading it once — at task start — is what produced
    AnnounceFinished(success=False) for audio that only needed a few more
    milliseconds of somebody else's connect event to become playable.

    Read once immediately, so a callback that is already there costs nothing at
    all. Then poll until `timeout`, and answer None if nothing turned up: the
    wait is bounded because not answering is the worst outcome of the two.

    `timeout=0` is exactly the old read-once behaviour.

    The expiry line is the diagnosis. `play_media` logs "no playback callback
    set" on the far side of the wait, and without this it cannot be told apart
    from a device that was milliseconds away — which is the question a support
    bundle is opened to answer.
    """
    deadline = time.monotonic() + timeout
    waited = 0.0
    while True:
        cb = get_cb()
        if callable(cb):
            if waited:
                log.info(
                    f"[{log_name}] Playback callback appeared after "
                    f"{waited:.2f}s — playing the announcement"
                )
            return cb
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if timeout:
                log.info(
                    f"[{log_name}] No playback callback after waiting "
                    f"{timeout}s — the device is not connected"
                )
            return None
        await asyncio.sleep(min(poll_s, remaining))
        waited += min(poll_s, remaining)


async def _resolve_play(play, log_name: str = ""):
    """
    The playback callback to use, however the caller handed it over.

    `play` is either the callback itself (what a test and every already-
    resolved caller passes) or an awaitable yielding one — which is how the
    announce path passes the `wait_for_play_cb` coroutine, so the wait happens
    where the answer is needed rather than once at task start.

    Anything that is not a callable comes back as None, which is the
    "nothing can play it" case: `_resolve_play` must never wait on something
    that will not become a callback, or a satellite whose Dot is absent parks
    HA for its five minutes. A resolver that raises is the same answer — the
    announcement reports failure promptly rather than not at all.
    """
    if inspect.isawaitable(play):
        try:
            play = await play
        except Exception as e:
            log.error(f"[{log_name}] Announce playback resolver failed: {e}")
            return None
    return play if callable(play) else None


def _drop_unawaited(play) -> None:
    """
    Close a coroutine we were handed and are not going to await.

    `play` is built at the call site, so an announcement that never reaches the
    fetch — no media_id — would leave one un-awaited and the loop would log
    "coroutine ... was never awaited" for a request that did nothing wrong.
    """
    if inspect.iscoroutine(play):
        play.close()


async def run(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play,
    on_finished: Callable[[bool], None],
    log_name: str = "",
    timeout: float = ANNOUNCE_TIMEOUT_S,
    preannounce_media_id: str = "",
) -> bool:
    """
    Fetch the announcement audio, play it, then report completion exactly once.

    `play` is the playback callback, or an awaitable yielding one, or None when
    nothing can play the audio — the physical device is not connected. That is
    not a successful announcement and HA is not told it was. See
    `_resolve_play` for the shapes and why an awaitable is one of them.

    `on_finished` is called from the finally on every path, including the ones
    that raise. It is a plain callable rather than a coroutine because the
    caller's job here is a socket write it may also decline to make (a closed
    transport), and awaiting a decision not to send is noise.

    `preannounce_media_id` is the attention chime HA plays BEFORE the message,
    on `VoiceAssistantAnnounceRequest` field 3. It matters most on
    `start_conversation`, where an unprompted "the garage door is open" arrives
    with no warning at all — nobody asked a question, so there is nothing else
    to tell the listener that the device is about to talk and then listen.

    **A preannounce failure is not an announcement failure.** The chime is a
    cue for the message; a missing cue is worth a log line, not a swallowed
    announcement, and `ok` reports the MESSAGE. Both share one timeout budget
    so a wedged chime cannot extend the whole thing past it.
    """
    ok = False
    try:
        if not media_id:
            log.warning(f"[{log_name}] AnnounceRequest with no media_id")
            _drop_unawaited(play)
        else:
            ok = await asyncio.wait_for(
                _preannounce_then_play(
                    media_id, preannounce_media_id, fetch, play, log_name),
                timeout)
    except (asyncio.TimeoutError, TimeoutError):
        log.error(f"[{log_name}] Announce timed out after {timeout}s")
    except Exception as e:
        log.error(f"[{log_name}] Announce fetch/play error: {e}")
    finally:
        on_finished(ok)
    return ok


async def _preannounce_then_play(
    media_id: str,
    preannounce_media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play,
    log_name: str = "",
) -> bool:
    """
    The chime, then the message. Returns whether the MESSAGE played.

    The callback is resolved ONCE, here, before either audio is fetched. The
    chime's wait and the message's wait must not be two spends of the same
    budget — the chime would take the full PLAY_CB_WAIT_S finding nothing and
    leave the message no time to be lucky.
    """
    play = await _resolve_play(play, log_name)
    if preannounce_media_id:
        try:
            await play_media(preannounce_media_id, fetch, play, log_name)
        except Exception as e:
            log.warning(f"[{log_name}] Preannounce chime failed: {e}")
    return await play_media(media_id, fetch, play, log_name)


async def play_media(
    media_id: str,
    fetch: Callable[[str], Awaitable[bytes]],
    play,
    log_name: str = "",
) -> bool:
    """
    Fetch and play, with no reply to anyone. True if the audio reached the
    speaker.

    Public because HA has TWO ways to announce and only one of them waits for
    a completion message: `VoiceAssistantAnnounceRequest` (run(), above) and
    `play_media` with announce=true, which is an ordinary media_player command.
    Sending AnnounceFinished for the latter would answer a question nobody
    asked.

    A `play` callback returning False means the audio did not reach the
    speaker — cancelled by a mute or a button mid-playback. None (the common
    case) means it has no opinion and is taken as played.

    It resolves `play` itself as well, so a caller that did not come through
    `_preannounce_then_play` is still correct. Resolving twice is a no-op: the
    second call is handed a callback or None, never the wait.
    """
    play = await _resolve_play(play, log_name)
    pcm_bytes = await fetch(media_id)
    if not pcm_bytes:
        log.warning(f"[{log_name}] Announce fetched no audio")
        return False

    if play is None:
        log.info(
            f"[{log_name}] Announce audio fetched ({len(pcm_bytes)}b) "
            f"— no playback callback set (standalone announce)"
        )
        return False

    return await play(pcm_bytes) is not False
