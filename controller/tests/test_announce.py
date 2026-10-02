"""
VoiceAssistantAnnounceFinished is HA's completion signal, and HA BLOCKS on it.

`assist_satellite.entity.async_internal_announce` documents `async_announce`
as "should block until the announcement is done playing", holds
`_is_announcing` and the RESPONDING state for its duration, and raises
`SatelliteBusyError` if another announcement arrives meanwhile. The esphome
integration implements that by awaiting our reply
(`send_voice_assistant_announcement_await_response`).

We used to answer it synchronously in the message handler, before a byte had
played. So the `assist_satellite.announce` service returned early, the entity
left RESPONDING early, and two chained announcements overlapped on the device
instead of queueing behind HA's own guard.

The justification in the code was that the setup wizard would otherwise time
out. It would not: `_ANNOUNCEMENT_TIMEOUT_SEC` is 5 minutes, and the wizard's
connection test does not wait on this message at all — it fires when the device
fetches the `CONNECTION_TEST_URL_BASE` media id.

Both directions are pinned here, because they pull against each other and the
code reads fine either way: the reply must not come early, and it must always
come.

The sequencing lives in `em_announce` rather than `em_esphome` so this suite can
reach it — the suite does not import `em_esphome` (zeroconf, aiohttp, the
database). Async tests run through `asyncio.run()`, the idiom the rest of the
suite uses; pytest-asyncio is not in the test environment and this is not worth
adding it for.
"""

import asyncio
import gc
import re
import warnings
from pathlib import Path

import em_announce

CONTROLLER = Path(__file__).resolve().parents[1]
ESPHOME_SRC = (CONTROLLER / "em_esphome.py").read_text()


def fetch_returning(pcm):
    async def _fetch(url):
        return pcm

    return _fetch


def fetch_raising(exc):
    async def _fetch(url):
        raise exc

    return _fetch


async def play_nothing(pcm):
    return None


def _junk_resolver():
    """An awaitable that yields something that is not a playback callback."""

    async def _resolve():
        return "not a callback"

    return _resolve()


class Replies:
    """Records what was reported to HA, and when."""

    def __init__(self):
        self.calls = []

    def __call__(self, ok):
        self.calls.append(ok)


# ── The reply lands after playback, not before ───────────────────────────────


def test_the_reply_waits_for_playback_to_finish():
    """
    The whole point. While the audio is playing HA must still be blocked, so a
    second announcement queues rather than talking over the first.
    """
    replies = Replies()
    playing = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(pcm):
        playing.set()
        await release.wait()

    async def main():
        task = asyncio.create_task(
            em_announce.run(
                "http://ha/x.flac",
                fetch=fetch_returning(b"\x00\x00" * 100),
                play=slow_play,
                on_finished=replies,
            )
        )
        await playing.wait()
        during = list(replies.calls)
        release.set()
        await task
        return during

    during = asyncio.run(main())
    assert during == [], "reported finished while the audio was still playing"
    assert replies.calls == [True]


def test_success_is_reported_when_the_audio_reached_the_speaker():
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [True]


# ── The reply always comes ───────────────────────────────────────────────────


def test_a_fetch_failure_still_replies():
    """
    Not replying parks HA for five minutes holding _is_announcing, after which
    every announcement fails SatelliteBusyError. success=False is strictly
    better than silence.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_raising(RuntimeError("ha unreachable")),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_failing_playback_still_replies():
    replies = Replies()

    async def boom(pcm):
        raise RuntimeError("device gone")

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=boom,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_an_empty_media_id_still_replies():
    replies = Replies()
    asyncio.run(
        em_announce.run("", fetch=fetch_returning(b""), play=play_nothing, on_finished=replies)
    )
    assert replies.calls == [False]


def test_no_playback_callback_is_not_a_success():
    """
    Audio fetched but nothing to play it on — the physical device is not
    connected. HA should not be told the announcement happened.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=None,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_empty_audio_is_not_a_success():
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b""),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_wedged_playback_gives_up_and_replies():
    """
    Our cap has to fire before HA's, or HA is the one left holding the
    announcement. The layer below is already bounded; this is the guard for
    when it isn't.
    """
    replies = Replies()

    async def wedged(pcm):
        await asyncio.sleep(30)

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=wedged,
            on_finished=replies,
            timeout=0.05,
        )
    )
    assert replies.calls == [False]


def test_exactly_one_reply_per_announcement():
    """
    A second AnnounceFinished has no run to belong to — HA pairs it with
    whatever it is waiting for next.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert len(replies.calls) == 1


def test_a_play_callback_reporting_failure_is_not_a_success():
    """
    The device can cancel mid-playback — a mute, a button press — and then the
    user did not hear the announcement. Reporting success for it is untrue, and
    success is the one fact this reply carries.
    """
    replies = Replies()

    async def cancelled(pcm):
        return False

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=cancelled,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_play_callback_with_no_opinion_counts_as_played():
    """
    Most callbacks return None. Treating that as failure would report every
    ordinary announcement as failed.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [True]


# ── Waiting for the playback callback (#219) ─────────────────────────────────


def test_a_callback_that_is_already_there_costs_nothing():
    """
    The common case is a device that has been connected for minutes, and it
    must not pay for the wait that exists for the other case.
    """
    played = []

    async def play(pcm):
        played.append(len(pcm))

    replies = Replies()

    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=em_announce.wait_for_play_cb(lambda: play),
            on_finished=replies,
        )
        return ok, loop.time() - started

    ok, elapsed = asyncio.run(main())
    assert ok is True
    assert played == [200]
    assert elapsed < 0.1, f"waited {elapsed:.3f}s for a callback that was there"


def test_the_wait_notices_a_callback_that_turns_up_late():
    """
    The bug this is for. The Dot's `/control` connect races HA's ESPHome TCP
    connect, so an announce can be fetched before the callback exists. Reading
    it once answered success=False for audio that was playable milliseconds
    later.
    """
    played = []
    reads = []

    async def play(pcm):
        played.append(len(pcm))

    def get_cb():
        reads.append(1)
        # Two polls of nothing, then the Dot's connect lands.
        return play if len(reads) > 2 else None

    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=em_announce.wait_for_play_cb(
                get_cb, timeout=5.0, poll_s=0.01),
            on_finished=Replies(),
        )
        return ok, loop.time() - started

    ok, elapsed = asyncio.run(main())
    assert ok is True, "a callback that arrived during the wait was not used"
    assert played == [200], "the audio never reached the speaker"
    assert len(reads) == 3, f"the callback took {len(reads)} reads to notice"
    assert 0.02 <= elapsed < 1.0, f"waited {elapsed:.3f}s, expected ~two polls"


def test_a_callback_that_never_appears_still_answers_promptly():
    """
    The other half of the rule the wait could break. A satellite whose Dot is
    off has no callback coming, and holding HA's `_is_announcing` is the worst
    outcome available — so the wait is bounded and `success=False` still goes
    out, inside a bound this asserts rather than assumes.
    """
    replies = Replies()

    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=em_announce.wait_for_play_cb(
                lambda: None, timeout=0.2, poll_s=0.01),
            on_finished=replies,
        )
        return ok, loop.time() - started

    ok, elapsed = asyncio.run(main())
    assert ok is False, "no callback means the audio reached nobody"
    assert replies.calls == [False]
    assert elapsed < 1.0, f"the unbounded path: {elapsed:.3f}s for a failure"


def test_a_resolver_that_raises_does_not_hang():
    """
    A broken resolver must land on the same answer as an absent one —
    `success=False` — rather than becoming a reply that never arrives.
    """
    replies = Replies()

    async def broken():
        raise RuntimeError("owning server went away")

    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=broken(),
            on_finished=replies,
        )
        return ok, loop.time() - started

    ok, elapsed = asyncio.run(main())
    assert ok is False
    assert replies.calls == [False]
    assert elapsed < 1.0


def test_something_that_is_not_a_callback_is_not_waited_on():
    """
    Whatever `_announce_play_cb` reads can be None and it can be anything the
    owning server put there. A non-callable is the "nothing can play this"
    answer, and waiting on it would park HA for its five minutes.
    """
    for play in ("not a callback", object(), _junk_resolver()):
        replies = Replies()

        async def main(play=play):
            loop = asyncio.get_running_loop()
            started = loop.time()
            ok = await em_announce.run(
                "http://ha/x.flac",
                fetch=fetch_returning(b"\x00\x00" * 100),
                play=play,
                on_finished=replies,
            )
            return ok, loop.time() - started

        ok, elapsed = asyncio.run(main())
        assert ok is False
        assert replies.calls == [False]
        assert elapsed < 1.0, f"waited {elapsed:.3f}s on {play!r}"


def test_a_resolver_whose_getter_keeps_returning_junk_still_terminates():
    """
    A getter that answers with the wrong thing is not the same as one that
    answers with a callback, and it must not turn the wait into an open end.
    """
    async def main():
        loop = asyncio.get_running_loop()
        started = loop.time()
        cb = await em_announce.wait_for_play_cb(
            lambda: "still not a callback", timeout=0.2, poll_s=0.01)
        return cb, loop.time() - started

    cb, elapsed = asyncio.run(main())
    assert cb is None
    assert elapsed < 1.0


def test_the_chime_does_not_spend_the_messages_wait():
    """
    One wait covers both. Two spends would either stretch the announcement past
    PLAY_CB_WAIT_S or — the shape the code actually has, a single-use resolver
    coroutine — leave the message with nothing at all, and the message is the
    one HA is blocked on.
    """
    played = []
    reads = []

    async def play(pcm):
        played.append(pcm)

    def get_cb():
        reads.append(1)
        # Two polls of nothing, then the Dot's connect lands.
        return play if len(reads) > 2 else None

    ok = asyncio.run(
        em_announce.run(
            "message",
            fetch=fetch_returning(b"pcm"),
            play=em_announce.wait_for_play_cb(
                get_cb, timeout=1.0, poll_s=0.01),
            on_finished=Replies(),
            preannounce_media_id="chime",
        )
    )
    assert ok is True, "the message did not play off one shared wait"
    assert played == [b"pcm", b"pcm"], "chime and message must both play"
    assert len(reads) == 3, (
        f"the callback was waited on {len(reads)} times for one announcement"
    )


def test_the_wait_cannot_outlive_the_announcement_cap():
    """
    The wait sits INSIDE `ANNOUNCE_TIMEOUT_S` rather than beside it, so one
    budget covers the announcement end to end and cannot be spent twice. Here
    the announcement cap is the smaller of the two and must be the one that
    fires — a callback that only turns up after the cap is one that arrived too
    late.
    """
    reads = []

    def get_cb():
        reads.append(1)
        return None

    assert em_announce.PLAY_CB_WAIT_S < em_announce.ANNOUNCE_TIMEOUT_S
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=em_announce.wait_for_play_cb(
                get_cb, poll_s=0.01),
            on_finished=replies,
            timeout=0.15,
        )
    )
    assert replies.calls == [False], "the cap must still answer HA"
    assert len(reads) < 30, (
        f"the wait outlived the cap: {len(reads)} reads after it fired"
    )


def test_a_request_with_no_media_id_leaves_no_unawaited_coroutine():
    """
    `play` is built at the call site, so the one path that never reaches the
    fetch would leave a coroutine un-awaited and the loop would log
    "coroutine ... was never awaited" for a request that did nothing wrong.
    """
    replies = Replies()

    async def resolver():
        raise AssertionError(
            "must not be awaited for an announcement with nothing to play")

    async def main():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await em_announce.run(
                "", fetch=fetch_returning(b""), play=resolver(), on_finished=replies
            )
            gc.collect()
        return [str(w.message) for w in caught]

    messages = asyncio.run(main())
    assert replies.calls == [False]
    assert not [m for m in messages if "never awaited" in m], messages


# ── The other way HA announces ───────────────────────────────────────────────


def test_play_media_announce_plays_without_replying():
    """
    HA has TWO announce paths and only one waits for a completion message.
    `play_media` with announce=true is an ordinary media_player command;
    sending AnnounceFinished for it answers a question nobody asked.
    """
    played = []

    async def play(pcm):
        played.append(len(pcm))

    ok = asyncio.run(
        em_announce.play_media(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play,
        )
    )
    assert ok is True
    assert played == [200]


def test_our_cap_sits_under_has():
    """
    HA's _ANNOUNCEMENT_TIMEOUT_SEC is 5 minutes. Ours must be comfortably
    below it — the point of a cap here is to be the side that gives up first.
    """
    assert em_announce.ANNOUNCE_TIMEOUT_S < 300


# ── The wiring, pinned against the source ────────────────────────────────────


def test_the_handler_does_not_answer_the_announce_itself():
    """
    The bug, in the shape it took: AnnounceFinished constructed in the
    message handler, so HA was answered before the background task had
    fetched anything. Everything else here would still pass with that
    restored.
    """
    handler = ESPHOME_SRC[ESPHOME_SRC.index("def handle_message"):]
    handler = handler[: handler.index("\n    def ", 10)]
    assert "VoiceAssistantAnnounceFinished" not in handler, (
        "the announce is answered in the message handler again — HA is told "
        "the announcement finished before any audio has played"
    )


def test_announcing_state_is_still_sent_synchronously():
    """
    ANNOUNCING describes the state we are ENTERING, unlike the completion
    reply, so it belongs in the handler. Moving it out would leave the entity
    idle for the length of the announcement.
    """
    handler = ESPHOME_SRC[ESPHOME_SRC.index("def handle_message"):]
    handler = handler[: handler.index("\n    def ", 10)]
    assert "MediaPlayerState.ANNOUNCING" in handler


def test_both_announce_paths_resolve_the_callback_the_same_way():
    """
    Renaming the shared helper broke the play_media path and not the other,
    because only one call site was checked — every play_media announce raised
    AttributeError on a released build (2026-08-17). One resolver, used by
    both, so there is nothing to miss next time.

    Both now go through the waiting resolver (#219) rather than sampling the
    live read once, so neither can be left on the old behaviour by the next
    rename.
    """
    src = ESPHOME_SRC
    assert src.count("_await_announce_play_cb()") >= 2, (
        "an announce path still resolves the callback once at task start — the "
        "connect-ordering window (#219) is then answered success=False"
    )
    waiter = src[src.index("async def _await_announce_play_cb"):]
    waiter = waiter[: waiter.index("\n    async def ", 10)]
    assert "wait_for_play_cb" in waiter and "self._announce_play_cb," in waiter, (
        "the waiting resolver is not fed the shared live read"
    )
    assert "_fetch_and_play_announce" not in src, (
        "a caller still references the removed helper"
    )


def test_no_handler_dispatches_to_a_method_that_does_not_exist():
    """
    handle_message catches nothing: a missing attribute surfaces only as
    'handle_message raised for MediaPlayerCommandRequest' in the log, at
    runtime, on a device someone is using. Cheap to check statically.
    """
    src = ESPHOME_SRC
    base = (CONTROLLER / "esphome" / "satellite_server.py").read_text()
    defined = set(re.findall(r"^    (?:async )?def (_[a-z_]+)", src + base, re.M))
    # Callables held as attributes rather than defined as methods — the
    # turn-scoped callbacks. Assigned with aligned "=" so the spacing varies.
    held = set(re.findall(r"self\.(_[a-z_]+)\s*[:=]", src))
    called = set(re.findall(r"self\.(_[a-z_]+)\(", src))
    missing = called - defined - held
    assert not missing, f"dispatched to methods that do not exist: {sorted(missing)}"
