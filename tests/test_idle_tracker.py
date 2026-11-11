from idle_tracker import DISCONNECTED, PLAYING, SILENT, IdleTracker

CFG = dict(threshold=0.0005, poll_seconds=30, idle_minutes=1)


def run(tracker, levels):
    return [tracker.tick(level, **CFG) for level in levels]


def test_silence_counts_up_by_poll_seconds():
    ticks = run(IdleTracker(), [0.0])
    assert (ticks[0].state, ticks[0].idle) == (SILENT, 30)


def test_power_off_fires_once_when_limit_reached():
    ticks = run(IdleTracker(), [0.0, 0.0, 0.0, 0.0])
    assert [t.power_off for t in ticks] == [False, True, False, False]


def test_audio_resets_the_count():
    ticks = run(IdleTracker(), [0.0, 0.5, 0.0])
    assert [(t.state, t.idle) for t in ticks] == [(SILENT, 30), (PLAYING, 0), (SILENT, 30)]


def test_disconnect_resets_and_rearms():
    ticks = run(IdleTracker(), [0.0, 0.0, -1.0, 0.0, 0.0])
    assert [t.power_off for t in ticks] == [False, True, False, False, True]
    assert ticks[2].state == DISCONNECTED


def test_reset_clears_idle_count():
    tracker = IdleTracker()
    run(tracker, [0.0])
    tracker.reset()
    assert tracker.tick(0.0, **CFG).idle == 30
