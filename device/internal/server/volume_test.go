package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"testing"
	"time"
)

// A deliberate button press must outrank the volume arc's 2s hold. Before
// this, adjusting volume then immediately pressing the action button left
// the arc owning the ring for the remainder of its window, so the device
// gave no sign it had started listening.
func TestCancelDisplayReleasesTheRing(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })

	vc.mu.Lock()
	vc.displayActive = true
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {})
	vc.mu.Unlock()

	if !vc.DisplayActive() {
		t.Fatal("precondition: arc should own the ring")
	}

	vc.CancelDisplay()

	if vc.DisplayActive() {
		t.Fatal("arc still owns the ring after CancelDisplay — a listening " +
			"frame would be recorded but not painted")
	}
	// Idempotent: a second press must not panic on the already-stopped timer.
	vc.CancelDisplay()
}

// tinymix ctl 61 spans 0..175, but 127 is the codec's 0dB. Above it the DAC
// applies positive digital gain to near-full-scale PCM and saturates —
// measured on hardware at 65% THD by index 153, 89% by 170, with the output
// level flat from 153 up because it had stopped getting louder. Stock FireOS
// never writes this control at all. If this constant creeps back toward 175,
// the garbling above ~73% volume returns.
func TestVolumeMaxIsCodecUnityNotTheControlMaximum(t *testing.T) {
	if volumeMax != 127 {
		t.Fatalf("volumeMax = %d, want 127 (0dB). Anything higher clips the DAC.",
			volumeMax)
	}
	if volumeButtonFloor >= volumeMax {
		t.Fatalf("button floor %d must sit below the ceiling %d",
			volumeButtonFloor, volumeMax)
	}
}

// The button band must be crossable in a sane number of presses: too few and
// each press is a huge jump, too many and reaching the top is a chore.
func TestButtonBandTakesAReasonableNumberOfPresses(t *testing.T) {
	presses := (volumeMax - volumeButtonFloor) / volumeStep
	if presses < 6 || presses > 16 {
		t.Fatalf("%d presses to cross the band (step %d over %d..%d); "+
			"want roughly 8-12", presses, volumeStep, volumeButtonFloor, volumeMax)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	cases := []struct {
		name string
		in   int
		want int
	}{
		// A level below the floor — HA can set one, and so could a stored
		// level from before the cap — must reach audible in ONE press, not
		// creep up 4dB at a time through inaudible territory.
		{"far below the floor lands on it", volumeButtonFloor - 40, volumeButtonFloor},
		{"just below the floor lands on it", volumeButtonFloor - 1, volumeButtonFloor},
		{"inside the band is untouched", volumeButtonFloor + volumeStep, volumeButtonFloor + volumeStep},
		{"above the ceiling clamps down", volumeMax + 30, volumeMax},
	}
	for _, tc := range cases {
		if got := clampToButtonBand(tc.in); got != tc.want {
			t.Errorf("%s: clampToButtonBand(%d) = %d, want %d",
				tc.name, tc.in, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band.
func TestSteppingSaturatesAtBothEnds(t *testing.T) {
	level := volumeMax
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level + volumeStep)
	}
	if level != volumeMax {
		t.Errorf("stepping up from the ceiling reached %d, want %d", level, volumeMax)
	}

	level = volumeButtonFloor
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level - volumeStep)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down from the floor reached %d, want %d",
			level, volumeButtonFloor)
	}
}

func TestStepReportsWhetherTheLevelChanged(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	vc.Set(volumeMax, false)
	if vc.StepUp() {
		t.Fatal("step up at the ceiling reported a change")
	}
	if !vc.StepDown() {
		t.Fatal("step down from the ceiling did not report a change")
	}
	vc.Set(volumeButtonFloor, false)
	if vc.StepDown() {
		t.Fatal("step down at the floor reported a change")
	}
}

// frames reports how many times the ring was painted at all. Painting NOTHING
// is the assertion a muted or seeded change has to pass, and "no cyan" would
// also be satisfied by an all-black arc — which is what level 0 would have
// drawn before Set() learned to skip it.
func frames(ring *recordingRing) int {
	ring.mu.Lock()
	defer ring.mu.Unlock()
	return len(ring.frames)
}

// arcLit reports how many LEDs the ring's most recent frame lights cyan. The
// arc is a reading, so a change that is meant to show must light a number of
// them rather than merely flash the ring.
func arcLit(ring *recordingRing) int {
	ring.mu.Lock()
	defer ring.mu.Unlock()
	if len(ring.frames) == 0 {
		return 0
	}
	lit := 0
	for _, l := range ring.frames[len(ring.frames)-1] {
		if l.R == 0 && l.G > 0 && l.B > 0 {
			lit++
		}
	}
	return lit
}

// A volume changed from Home Assistant, a service call or an automation
// applied correctly and left the ring dark (#634) — the audio getting quieter
// was the only sign the request had landed. The remote route paints the same
// arc the buttons do.
func TestRemoteVolumeChangePaintsTheArc(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	defer s.volume.CancelDisplay()

	s.SetVolume((volumeButtonFloor + volumeMax) / 2)

	if !s.volume.DisplayActive() {
		t.Fatal("a remote volume change did not take the ring, so controller " +
			"frames and the direction overlay would paint over the reading")
	}
	if arcLit(ring) < 1 {
		t.Fatalf("arc painted no lit LED at level %d", s.VolumeLevel())
	}
}

// The boot-time restore is the one caller that must stay silent, and it is
// silent for the whole run rather than for one call: a later config push
// returns before Set() even runs.
func TestSeedVolumeAtBootDoesNotPaintTheArc(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SeedVolume(volumeMax / 2)
	s.SeedVolume(volumeMax) // a later push must not stomp a live level either

	if s.VolumeLevel() != volumeMax/2 {
		t.Fatalf("SeedVolume applied %d, want the first push's %d",
			s.VolumeLevel(), volumeMax/2)
	}
	if s.volume.DisplayActive() {
		t.Fatal("seeding the stored volume lit the ring on boot — nobody asked " +
			"for that level to change")
	}
	if n := frames(ring); n != 0 {
		t.Fatalf("seeding painted the ring %d times", n)
	}
}

// HA's media-player mute arrives as volume 0 (controller/em_output_mute.py
// sends 0 and remembers the level, and re-sends 0 on every reconnect while
// muted). The device cannot tell that from a slider dragged to zero, and an
// arc has nothing to say about either: level 0 draws an empty arc, so painting
// it would blank the ring for 2s while suppressing every paint meant to
// protect something.
func TestMuteLevelDoesNotPaintTheArc(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetVolume(0)

	if s.VolumeLevel() != 0 {
		t.Fatalf("mute did not reach the device: level %d", s.VolumeLevel())
	}
	if s.volume.DisplayActive() {
		t.Fatal("a mute painted the volume arc")
	}
	if n := frames(ring); n != 0 {
		t.Fatalf("a mute painted the ring %d times", n)
	}

	// Unmute restores a real level, and that IS a volume change somebody
	// asked for — the gate is on the level, not on a mute having happened.
	s.SetVolume(volumeButtonFloor + volumeStep*2)
	defer s.volume.CancelDisplay()
	if !s.volume.DisplayActive() || arcLit(ring) < 1 {
		t.Fatal("unmute restored the level silently; the owner has no way to " +
			"see where the volume came back to")
	}
}

// Stepping must be untouched by any of the above: the arc still paints, and
// no number of presses reaches the level Set() now skips.
func TestButtonStepsPaintAndNeverReachTheSkippedLevel(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	defer s.volume.CancelDisplay()

	for i := 0; i < 30; i++ {
		s.VolumeStepDown()
		if !s.volume.DisplayActive() {
			t.Fatalf("press %d painted no arc at level %d", i+1, s.VolumeLevel())
		}
		if arcLit(ring) < 1 {
			t.Fatalf("press %d lit no LED at level %d", i+1, s.VolumeLevel())
		}
	}
	if got := s.VolumeLevel(); got != volumeButtonFloor {
		t.Fatalf("30 presses down reached %d, want the button floor %d — a "+
			"press that reached 0 would stop painting", got, volumeButtonFloor)
	}
}
