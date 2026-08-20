import unittest

from flow_controller.core.sequence import (HOLD, LINEAR, SMOOTH, Keyframe,
                                           Sequence, SequencePlayer,
                                           SequenceRecorder, SettleGate, Track,
                                           TrackMeta, opening_mismatches)


def track(key='nh3_rich', label='NH3 rich', frames=()):
    return Track(key=key, label=label,
                 keyframes=[Keyframe(*frame) for frame in frames])


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class TrackValueTests(unittest.TestCase):
    def test_empty_track_reads_zero(self):
        self.assertEqual(track().value_at(5.0), 0.0)

    def test_value_is_held_before_the_first_keyframe(self):
        line = track(frames=[(2.0, 4.0, HOLD)])
        self.assertEqual(line.value_at(0.0), 4.0)

    def test_value_is_held_after_the_last_keyframe(self):
        line = track(frames=[(0.0, 1.0, HOLD), (2.0, 4.0, HOLD)])
        self.assertEqual(line.value_at(99.0), 4.0)

    def test_hold_does_not_interpolate(self):
        line = track(frames=[(0.0, 0.0, HOLD), (10.0, 5.0, HOLD)])
        self.assertEqual(line.value_at(9.9), 0.0)

    def test_linear_interpolates_between_its_neighbours(self):
        line = track(frames=[(0.0, 0.0, LINEAR), (10.0, 5.0, HOLD)])
        self.assertAlmostEqual(line.value_at(4.0), 2.0)

    def test_smooth_transition_eases_without_changing_key_points(self):
        line = track(frames=[(0.0, 0.0, SMOOTH), (10.0, 10.0, HOLD)])
        self.assertAlmostEqual(line.value_at(2.5), 1.5625)
        self.assertAlmostEqual(line.value_at(5.0), 5.0)
        self.assertAlmostEqual(line.value_at(10.0), 10.0)

    def test_a_negative_value_from_a_file_is_clamped(self):
        line = Track.from_dict({'key': 'nh3_rich', 'label': 'NH3',
                                'keyframes': [{'t': 0.0, 'v': -3.0}]})
        self.assertEqual(line.value_at(0.0), 0.0)

    def test_a_negative_value_from_an_edit_is_clamped(self):
        line = track(frames=[(0.0, 1.0, HOLD)])
        line.add(2.0, -5.0)
        self.assertEqual(line.value_at(2.0), 0.0)

    def test_a_non_finite_value_from_a_file_is_clamped(self):
        line = Track.from_dict({'key': 'nh3_rich', 'label': 'NH3',
                                'keyframes': [{'t': 0.0, 'v': float('nan')}]})
        self.assertEqual(line.value_at(0.0), 0.0)

    def test_span_and_duration_come_from_the_keyframes(self):
        line = track(frames=[(0.0, 1.0, HOLD), (7.0, 9.0, HOLD)])
        self.assertEqual(line.span, 9.0)
        self.assertEqual(line.duration, 7.0)


class TrackEditTests(unittest.TestCase):
    def test_add_replaces_a_keyframe_at_the_same_instant(self):
        line = track(frames=[(0.0, 1.0, HOLD), (5.0, 2.0, HOLD)])
        line.add(5.0, 3.0)
        self.assertEqual(len(line.keyframes), 2)
        self.assertEqual(line.value_at(6.0), 3.0)

    def test_add_inherits_the_interpolation_of_the_split_segment(self):
        line = track(frames=[(0.0, 0.0, LINEAR), (10.0, 10.0, HOLD)])
        index = line.add(5.0, 5.0)
        self.assertEqual(line.sorted_frames()[index].interp, LINEAR)

    def test_a_keyframe_cannot_be_dragged_past_its_neighbour(self):
        line = track(frames=[(0.0, 0.0, HOLD), (5.0, 1.0, HOLD),
                             (10.0, 2.0, HOLD)])
        line.move(1, 50.0, 1.0)
        self.assertLess(line.sorted_frames()[1].t, 10.0)

    def test_the_opening_keyframe_cannot_be_removed(self):
        line = track(frames=[(0.0, 0.0, HOLD), (5.0, 1.0, HOLD)])
        self.assertFalse(line.remove(0))
        self.assertTrue(line.remove(1))

    def test_hold_edges_are_sampled_as_steps(self):
        line = track(frames=[(0.0, 0.0, HOLD), (10.0, 5.0, HOLD)])
        times, values = line.samples()
        self.assertEqual(values[:3], [0.0, 0.0, 5.0])
        self.assertEqual(times[:3], [0.0, 10.0, 10.0])


class SequenceRoundTripTests(unittest.TestCase):
    def test_a_sequence_survives_a_dict_round_trip(self):
        original = Sequence(name='run', tracks=[
            track(frames=[(0.0, 0.0, HOLD), (4.0, 2.5, LINEAR)])],
            markers=[2.0])
        restored = Sequence.from_dict(original.to_dict())
        self.assertEqual(restored.name, 'run')
        self.assertEqual(restored.markers, [2.0])
        self.assertAlmostEqual(restored.duration, 4.0)
        self.assertEqual(restored.tracks[0].sorted_frames()[1].interp, LINEAR)
        self.assertEqual(original.to_dict()['version'], 1)

    def test_a_smoothed_sequence_uses_v2_and_preserves_the_transition(self):
        original = Sequence(tracks=[
            track(frames=[(0.0, 0.0, SMOOTH), (4.0, 2.5, HOLD)])])

        data = original.to_dict()
        restored = Sequence.from_dict(data)

        self.assertEqual(data['version'], 2)
        self.assertEqual(restored.tracks[0].sorted_frames()[0].interp, SMOOTH)

    def test_a_foreign_file_is_rejected_by_format(self):
        with self.assertRaisesRegex(ValueError, 'Not a flow-controller'):
            Sequence.from_dict({'format': 'something-else'})

    def test_a_newer_file_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'newer'):
            Sequence.from_dict({'format': 'flow-controller-sequence',
                                'version': 99})

    def test_bind_reports_roles_that_are_not_assigned(self):
        sequence = Sequence(tracks=[track(key='nh3_rich'),
                                    track(key='ch4_pilot', label='Pilot')])
        bound, missing = sequence.bind(
            lambda key: 'A' if key == 'nh3_rich' else None)
        self.assertEqual(bound, {'nh3_rich': 'A'})
        self.assertEqual([line.key for line in missing], ['ch4_pilot'])

    def test_speed_change_retimes_every_track_and_marker_together(self):
        sequence = Sequence(
            tracks=[track(frames=[(0.0, 0.0, HOLD),
                                  (12.0, 2.0, LINEAR)]),
                    track(key='ch4_pilot', label='Pilot',
                          frames=[(0.0, 1.0, HOLD),
                                  (6.0, 2.0, HOLD)])],
            markers=[3.0, 9.0])

        self.assertTrue(sequence.scale_speed(1.2))

        self.assertAlmostEqual(sequence.duration, 10.0)
        self.assertEqual(sequence.markers, [2.5, 7.5])
        self.assertAlmostEqual(sequence.track('ch4_pilot').duration, 5.0)

    def test_smooth_all_changes_every_real_transition_but_not_values(self):
        sequence = Sequence(tracks=[track(frames=[
            (0.0, 1.0, HOLD), (5.0, 4.0, LINEAR), (10.0, 2.0, HOLD)])])

        sequence.smooth_all()

        frames = sequence.tracks[0].sorted_frames()
        self.assertEqual([frame.interp for frame in frames],
                         [SMOOTH, SMOOTH, HOLD])
        self.assertEqual([frame.value for frame in frames], [1.0, 4.0, 2.0])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.recorder = SequenceRecorder(clock=self.clock)
        self.metas = [TrackMeta(key='nh3_rich', label='NH3', unit='A'),
                      TrackMeta(key='ch4_pilot', label='Pilot', unit='B')]

    def test_recording_opens_with_a_keyframe_per_track(self):
        self.recorder.start(self.metas, {'nh3_rich': 2.0})
        sequence = self.recorder.stop('run')
        for line in sequence.tracks:
            self.assertEqual(line.sorted_frames()[0].t, 0.0)
        self.assertEqual(sequence.track('nh3_rich').value_at(0.0), 2.0)
        self.assertEqual(sequence.track('ch4_pilot').value_at(0.0), 0.0)

    def test_notes_are_captured_at_the_elapsed_time(self):
        self.recorder.start(self.metas)
        self.clock.advance(3.0)
        self.recorder.note('nh3_rich', 5.0)
        sequence = self.recorder.stop()
        frames = sequence.track('nh3_rich').sorted_frames()
        self.assertAlmostEqual(frames[1].t, 3.0)
        self.assertEqual(frames[1].value, 5.0)

    def test_an_unchanged_setpoint_is_not_a_keyframe(self):
        self.recorder.start(self.metas, {'nh3_rich': 5.0})
        self.clock.advance(1.0)
        self.assertFalse(self.recorder.note('nh3_rich', 5.0))
        self.assertEqual(len(self.recorder.stop().track('nh3_rich').keyframes), 2)

    def test_an_unknown_track_is_ignored(self):
        self.recorder.start(self.metas)
        self.assertFalse(self.recorder.note('not_a_role', 1.0))

    def test_nothing_is_captured_before_recording_starts(self):
        self.assertFalse(self.recorder.note('nh3_rich', 1.0))
        self.assertIsNone(self.recorder.mark())
        self.assertIsNone(self.recorder.stop())

    def test_a_mark_anchors_every_track_at_its_held_value(self):
        self.recorder.start(self.metas, {'nh3_rich': 4.0})
        self.clock.advance(2.0)
        at = self.recorder.mark()
        self.assertAlmostEqual(at, 2.0)
        sequence = self.recorder.stop()
        self.assertEqual(sequence.markers, [2.0])
        for line in sequence.tracks:
            self.assertIn(2.0, [frame.t for frame in line.sorted_frames()])
        # The anchor records what was held, not a new value.
        self.assertEqual(sequence.track('nh3_rich').value_at(2.0), 4.0)

    def test_stopping_closes_every_track_at_the_final_instant(self):
        self.recorder.start(self.metas)
        self.clock.advance(1.0)
        self.recorder.note('nh3_rich', 3.0)
        self.clock.advance(4.0)
        sequence = self.recorder.stop()
        self.assertAlmostEqual(sequence.duration, 5.0)
        for line in sequence.tracks:
            self.assertAlmostEqual(line.duration, 5.0)

    def test_cancelling_discards_the_recording(self):
        self.recorder.start(self.metas)
        self.recorder.cancel()
        self.assertFalse(self.recorder.active)
        self.assertIsNone(self.recorder.stop())

    def test_a_line_put_down_to_zero_is_a_keyframe(self):
        # The batch "zero all flows" arrives here as a note of 0.0.  A recording
        # that dropped it would show the flows still up at the moment the
        # operator put them down, and replaying it would leave them up.
        self.recorder.start(self.metas, {'nh3_rich': 12.0, 'ch4_pilot': 3.0})
        self.clock.advance(4.0)
        self.assertTrue(self.recorder.note('nh3_rich', 0.0))
        self.assertTrue(self.recorder.note('ch4_pilot', 0.0))
        sequence = self.recorder.stop()
        self.assertEqual(sequence.track('nh3_rich').value_at(4.0), 0.0)
        self.assertEqual(sequence.track('ch4_pilot').value_at(4.0), 0.0)
        # And what it was before the zero is still in the curve.
        self.assertEqual(sequence.track('nh3_rich').value_at(0.0), 12.0)

    def test_zeroing_a_line_already_at_zero_adds_nothing(self):
        self.recorder.start(self.metas, {'nh3_rich': 12.0})
        self.clock.advance(1.0)
        self.assertFalse(self.recorder.note('ch4_pilot', 0.0))


class OpeningMismatchTests(unittest.TestCase):
    """The gate on running a saved sequence from the operation tab."""

    def setUp(self):
        self.sequence = Sequence(tracks=[
            track(key='nh3_rich', label='NH3 rich',
                  frames=[(0.0, 100.0, LINEAR), (30.0, 150.0, HOLD)]),
            track(key='ch4_pilot', label='Pilot',
                  frames=[(0.0, 0.0, LINEAR), (30.0, 10.0, HOLD)]),
        ])

    def test_a_rig_standing_at_the_opening_setpoints_is_ready(self):
        self.assertEqual(
            opening_mismatches(self.sequence,
                               {'nh3_rich': 100.0, 'ch4_pilot': 0.0}),
            [])

    def test_a_controller_holding_close_to_its_setpoint_is_ready(self):
        # A line settled on 100 reads 99.4 as often as it reads 100; refusing
        # that would refuse every real rig.
        self.assertEqual(
            opening_mismatches(self.sequence,
                               {'nh3_rich': 99.4, 'ch4_pilot': 0.03}),
            [])

    def test_a_line_in_the_wrong_place_is_named(self):
        rows = opening_mismatches(self.sequence,
                                  {'nh3_rich': 40.0, 'ch4_pilot': 0.0})
        self.assertEqual([(line.key, wanted, actual)
                          for line, wanted, actual in rows],
                         [('nh3_rich', 100.0, 40.0)])

    def test_the_worst_line_is_reported_first(self):
        rows = opening_mismatches(self.sequence,
                                  {'nh3_rich': 90.0, 'ch4_pilot': 8.0})
        self.assertEqual([line.key for line, _wanted, _actual in rows],
                         ['nh3_rich', 'ch4_pilot'])

    def test_a_line_that_is_not_reporting_counts_as_no_flow(self):
        # The honest reading for a controller that is not answering, and the
        # one that refuses rather than starts.
        rows = opening_mismatches(self.sequence, {'ch4_pilot': 0.0})
        self.assertEqual([(line.key, actual)
                          for line, _wanted, actual in rows],
                         [('nh3_rich', 0.0)])

    def test_a_tiny_track_is_judged_on_the_floor_not_its_span(self):
        # Two percent of a 0.2 SLPM pilot trim is 4 millilitres; no controller
        # holds that, and holding the rig back for it would be theatre.
        tiny = Sequence(tracks=[track(key='ch4_pilot', label='Pilot',
                                      frames=[(0.0, 0.2, HOLD)])])
        self.assertEqual(opening_mismatches(tiny, {'ch4_pilot': 0.24}), [])
        self.assertTrue(opening_mismatches(tiny, {'ch4_pilot': 0.9}))

    def test_the_tolerance_can_be_tightened(self):
        self.assertTrue(
            opening_mismatches(self.sequence,
                               {'nh3_rich': 99.4, 'ch4_pilot': 0.0},
                               tolerance=0.001, floor=0.0))

    def test_an_empty_sequence_has_nothing_to_disagree_with(self):
        self.assertEqual(opening_mismatches(Sequence(), {}), [])


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.logged = []

    def player(self, sequence, **kwargs):
        return SequencePlayer(
            sequence, {line.key: line.key[0].upper()
                       for line in sequence.tracks},
            lambda unit, value: self.sent.append((unit, round(value, 4))),
            log=self.logged.append, **kwargs)

    def test_priming_sends_the_opening_values(self):
        sequence = Sequence(tracks=[track(frames=[(0.0, 2.0, HOLD),
                                                  (5.0, 4.0, HOLD)])])
        self.player(sequence).prime()
        self.assertEqual(self.sent, [('N', 2.0)])

    def test_an_unbound_track_is_never_sent(self):
        sequence = Sequence(tracks=[track(frames=[(0.0, 1.0, HOLD),
                                                  (2.0, 3.0, HOLD)])])
        player = SequencePlayer(sequence, {}, lambda u, v: self.sent.append(u))
        player.prime()
        player.tick(2.0, 0.1)
        self.assertEqual(self.sent, [])

    def test_the_deadband_suppresses_insignificant_changes(self):
        sequence = Sequence(tracks=[track(frames=[(0.0, 0.0, LINEAR),
                                                  (100.0, 100.0, HOLD)])])
        player = self.player(sequence)
        player.prime()
        self.sent.clear()
        player.tick(0.01, 0.01)     # a 0.01 move against a span of 100
        self.assertEqual(self.sent, [])
        player.tick(5.0, 0.1)
        self.assertEqual(self.sent, [('N', 5.0)])

    def test_a_rate_limited_line_is_ramped_over_a_step_edge(self):
        sequence = Sequence(tracks=[
            track(key='ch4_pilot', label='Pilot',
                  frames=[(0.0, 0.0, HOLD), (1.0, 10.0, HOLD)])])
        player = self.player(sequence, rate_limited={'ch4_pilot'},
                            min_ramp_s=1.0)
        player.prime()
        self.sent.clear()
        player.tick(1.5, 0.1)
        # One tenth of a 1 s ramp across a span of 10 is one unit, not ten.
        self.assertEqual(self.sent, [('C', 1.0)])
        self.assertFalse(player.finished)
        self.assertTrue(any('step edge spread' in line
                            for line in self.logged))

    def test_an_unlimited_line_takes_a_step_edge_directly(self):
        sequence = Sequence(tracks=[track(frames=[(0.0, 0.0, HOLD),
                                                  (1.0, 10.0, HOLD)])])
        player = self.player(sequence)
        player.prime()
        self.sent.clear()
        player.tick(1.5, 0.1)
        self.assertEqual(self.sent, [('N', 10.0)])

    def test_a_replay_is_not_finished_until_the_lines_arrive(self):
        sequence = Sequence(tracks=[
            track(key='ch4_pilot', label='Pilot',
                  frames=[(0.0, 0.0, HOLD), (1.0, 10.0, HOLD)])])
        player = self.player(sequence, rate_limited={'ch4_pilot'},
                            min_ramp_s=1.0)
        player.prime()
        self.assertFalse(player.tick(1.0, 0.1))
        for _ in range(40):
            if player.tick(1.0, 0.1):
                break
        self.assertTrue(player.finished)
        self.assertAlmostEqual(self.sent[-1][1], 10.0, places=3)


class RepeatTests(unittest.TestCase):
    """Looping a recorded transition back to its starting conditions."""

    def setUp(self):
        self.sent = []

    def sequence(self):
        return Sequence(tracks=[track(frames=[(0.0, 1.0, HOLD),
                                              (5.0, 9.0, HOLD)])])

    def player(self, repeats, **kwargs):
        sequence = self.sequence()
        return SequencePlayer(
            sequence, {'nh3_rich': 'A'},
            lambda unit, value: self.sent.append(round(value, 4)),
            repeats=repeats, **kwargs)

    def test_a_single_pass_has_no_next_cycle(self):
        player = self.player(1)
        self.assertEqual(player.cycle, 1)
        self.assertEqual(player.cycles_left, 0)
        self.assertFalse(player.next_cycle())

    def test_a_counted_repeat_runs_exactly_that_many_passes(self):
        player = self.player(3)
        self.assertEqual(player.cycles_left, 2)
        self.assertTrue(player.next_cycle())
        self.assertEqual(player.cycle, 2)
        self.assertTrue(player.next_cycle())
        self.assertEqual(player.cycle, 3)
        self.assertFalse(player.next_cycle())
        self.assertEqual(player.cycle, 3)

    def test_zero_repeats_never_runs_out(self):
        player = self.player(0)
        self.assertTrue(player.endless)
        self.assertIsNone(player.cycles_left)
        for expected in range(2, 60):
            self.assertTrue(player.next_cycle())
            self.assertEqual(player.cycle, expected)

    def test_a_wrap_rewinds_the_position(self):
        player = self.player(2)
        player.prime()
        player.tick(5.0, 0.1)
        self.assertAlmostEqual(player.position, 5.0)
        player.next_cycle()
        self.assertEqual(player.position, 0.0)

    def test_a_wrap_does_not_force_the_opening_value_out(self):
        """The end-to-start jump must go through the rate limiter.

        Re-priming at the wrap would write it straight to the controller, which
        on the pilot or an air line is the transient the limiter exists to stop.
        """
        player = self.player(2, rate_limited={'nh3_rich'}, min_ramp_s=1.0)
        player.prime()
        player.tick(5.0, 10.0)          # settle at the closing value
        self.assertAlmostEqual(self.sent[-1], 9.0)
        self.sent.clear()

        player.next_cycle()
        self.assertEqual(self.sent, [])  # the wrap itself sends nothing
        player.tick(0.0, 0.1)
        # Heading back down towards 1.0 at one tenth of the span per tick,
        # not landing on it.
        self.assertAlmostEqual(self.sent[-1], 8.1, places=3)
        self.assertGreater(self.sent[-1], 1.0)

    def test_an_unlimited_line_returns_to_the_opening_value_at_the_wrap(self):
        player = self.player(2)
        player.prime()
        player.tick(5.0, 0.1)
        self.assertAlmostEqual(self.sent[-1], 9.0)
        player.next_cycle()
        player.tick(0.0, 0.1)
        self.assertAlmostEqual(self.sent[-1], 1.0)

    def test_the_second_pass_repeats_the_transition(self):
        player = self.player(2)
        player.prime()
        player.tick(5.0, 0.1)
        player.next_cycle()
        player.tick(0.0, 0.1)
        self.sent.clear()
        player.tick(5.0, 0.1)
        self.assertAlmostEqual(self.sent[-1], 9.0)


class RampRateTests(unittest.TestCase):
    """A slew limit per controller, on top of the lines that always have one."""

    def setUp(self):
        self.sent = []
        self.logged = []

    def player(self, sequence, **kwargs):
        return SequencePlayer(
            sequence, {line.key: line.key[0].upper()
                       for line in sequence.tracks},
            lambda unit, value: self.sent.append((unit, round(value, 4))),
            log=self.logged.append, **kwargs)

    def line(self, key='nh3_rich'):
        return track(key=key, label=key,
                     frames=[(0.0, 0.0, HOLD), (1.0, 10.0, HOLD)])

    def test_a_rate_is_cleared_by_none_zero_and_nonsense(self):
        line = self.line()
        for value in (None, 0.0, -2.0, float('nan'), 'fast', object()):
            line.ramp_rate = 7.0
            self.assertIsNone(line.set_ramp_rate(value), repr(value))
            self.assertIsNone(line.ramp_rate)

    def test_a_rate_is_written_only_when_it_is_set(self):
        line = self.line()
        self.assertNotIn('ramp', line.to_dict())
        line.set_ramp_rate(2.5)
        self.assertEqual(line.to_dict()['ramp'], 2.5)

    def test_a_rate_survives_a_round_trip_and_its_absence_reads_as_none(self):
        original = Sequence(tracks=[self.line(), self.line(key='ch4_pilot')])
        original.tracks[0].set_ramp_rate(1.25)
        restored = Sequence.from_dict(original.to_dict())
        self.assertEqual(restored.tracks[0].ramp_rate, 1.25)
        self.assertIsNone(restored.tracks[1].ramp_rate)

    def test_an_unlimited_line_with_a_rate_is_ramped(self):
        sequence = Sequence(tracks=[self.line()])
        sequence.tracks[0].set_ramp_rate(2.0)
        player = self.player(sequence)
        player.prime()
        self.sent.clear()
        player.tick(1.5, 0.1)
        self.assertEqual(self.sent, [('N', 0.2)])
        self.assertTrue(any('its ramp limit is set' in entry
                            for entry in self.logged))

    def test_a_rate_limited_line_may_be_slowed_but_not_hurried(self):
        sequence = Sequence(tracks=[self.line(key='ch4_pilot')])
        line = sequence.tracks[0]
        player = self.player(sequence, rate_limited={'ch4_pilot'},
                            min_ramp_s=1.0)
        # The default here is the span, 10, over one second.
        self.assertAlmostEqual(player.rate_for(line), 10.0)
        line.set_ramp_rate(1.0)
        self.assertAlmostEqual(player.rate_for(line), 1.0)
        line.set_ramp_rate(500.0)
        self.assertAlmostEqual(player.rate_for(line), 10.0)

    def test_an_unlimited_line_without_a_rate_is_free(self):
        sequence = Sequence(tracks=[self.line()])
        self.assertIsNone(self.player(sequence).rate_for(sequence.tracks[0]))


class RateLookupTests(unittest.TestCase):
    """The rate declared on the controller's card, asked for at replay time."""

    def setUp(self):
        self.sent = []

    def player(self, sequence, **kwargs):
        return SequencePlayer(
            sequence, {line.key: line.key[0].upper()
                       for line in sequence.tracks},
            lambda unit, value: self.sent.append((unit, round(value, 4))),
            **kwargs)

    def line(self, key='nh3_rich'):
        return track(key=key, label=key,
                     frames=[(0.0, 0.0, HOLD), (1.0, 10.0, HOLD)])

    def test_the_lookup_paces_a_line_that_carries_no_rate_of_its_own(self):
        sequence = Sequence(tracks=[self.line()])
        player = self.player(sequence, rate_lookup=lambda _track: 2.0)
        player.prime()
        self.sent.clear()
        player.tick(1.5, 0.1)
        self.assertEqual(self.sent, [('N', 0.2)])

    def test_the_lookup_wins_over_a_rate_saved_in_the_sequence(self):
        sequence = Sequence(tracks=[self.line()])
        sequence.tracks[0].set_ramp_rate(5.0)
        player = self.player(sequence, rate_lookup=lambda _track: 2.0)
        self.assertAlmostEqual(player.rate_for(sequence.tracks[0]), 2.0)

    def test_a_lookup_with_no_answer_falls_back_to_the_saved_rate(self):
        sequence = Sequence(tracks=[self.line()])
        sequence.tracks[0].set_ramp_rate(5.0)
        player = self.player(sequence, rate_lookup=lambda _track: None)
        self.assertAlmostEqual(player.rate_for(sequence.tracks[0]), 5.0)

    def test_a_lookup_that_raises_does_not_stop_the_replay(self):
        sequence = Sequence(tracks=[self.line()])
        sequence.tracks[0].set_ramp_rate(5.0)

        def broken(_track):
            raise RuntimeError('no such unit')

        player = self.player(sequence, rate_lookup=broken)
        self.assertAlmostEqual(player.rate_for(sequence.tracks[0]), 5.0)

    def test_a_looked_up_rate_still_cannot_hurry_a_never_stepped_line(self):
        sequence = Sequence(tracks=[self.line(key='ch4_pilot')])
        player = self.player(sequence, rate_limited={'ch4_pilot'},
                             min_ramp_s=1.0,
                             rate_lookup=lambda _track: 500.0)
        # The span, 10, over one second: the protection is not for sale.
        self.assertAlmostEqual(player.rate_for(sequence.tracks[0]), 10.0)


class SettleGateTests(unittest.TestCase):
    """Holding the clock for every line while any one of them lags."""

    def setUp(self):
        self.tracks = [track(key='nh3_rich', label='NH3 rich',
                             frames=[(0.0, 0.0, HOLD), (1.0, 100.0, HOLD)]),
                       track(key='ch4_pilot', label='Pilot',
                             frames=[(0.0, 0.0, HOLD), (1.0, 10.0, HOLD)])]
        self.gate = SettleGate()

    def check(self, measured, elapsed=0.1, commanded=None):
        if commanded is None:
            commanded = {'nh3_rich': 100.0, 'ch4_pilot': 10.0}
        return self.gate.check(self.tracks, commanded, measured, elapsed)

    def test_lines_within_tolerance_do_not_hold(self):
        # 5 % of a span of 100 is 5 units; of a span of 10, half a unit.
        self.assertFalse(self.check({'nh3_rich': 96.0, 'ch4_pilot': 9.7}))
        self.assertFalse(self.gate.holding)

    def test_one_lagging_line_holds_the_clock(self):
        self.assertTrue(self.check({'nh3_rich': 100.0, 'ch4_pilot': 4.0}))
        self.assertTrue(self.gate.holding)
        self.assertIn('Pilot', self.gate.reason)
        self.assertAlmostEqual(self.gate.held_s, 0.1)

    def test_the_worst_offender_is_the_one_named(self):
        self.assertTrue(self.check({'nh3_rich': 10.0, 'ch4_pilot': 8.0}))
        self.assertIn('NH3 rich', self.gate.reason)

    def test_a_missing_reading_is_not_a_discrepancy(self):
        self.assertFalse(self.check({}))
        self.assertFalse(self.check({'nh3_rich': 100.0}))
        self.assertFalse(self.gate.holding)

    def test_an_unbound_track_is_not_judged(self):
        self.assertFalse(self.check({'nh3_rich': 100.0},
                                    commanded={'nh3_rich': 100.0}))

    def test_the_floor_covers_a_line_whose_span_is_tiny(self):
        gate = SettleGate(tolerance=0.05, floor=0.5)
        tiny = [track(key='nh3_rich', frames=[(0.0, 1.0, HOLD)])]
        # 5 % of a span of 1 is 0.02, but the floor forgives up to 0.5.
        self.assertFalse(gate.check(tiny, {'nh3_rich': 1.0},
                                    {'nh3_rich': 0.7}, 0.1))
        self.assertTrue(gate.check(tiny, {'nh3_rich': 1.0},
                                   {'nh3_rich': 0.2}, 0.1))

    def test_the_hold_accumulates_and_releases_when_the_flow_arrives(self):
        for _ in range(10):
            self.check({'nh3_rich': 100.0, 'ch4_pilot': 0.0}, 0.2)
        self.assertAlmostEqual(self.gate.held_s, 2.0)
        self.assertAlmostEqual(self.gate.total_held_s, 2.0)
        self.assertFalse(self.check({'nh3_rich': 100.0, 'ch4_pilot': 10.0}))
        self.assertEqual(self.gate.reason, '')
        self.assertEqual(self.gate.held_s, 0.0)
        # What the run waited for in total is not forgotten by a release.
        self.assertAlmostEqual(self.gate.total_held_s, 2.0)

    def test_a_line_that_never_arrives_times_out_and_lets_the_run_go(self):
        gate = SettleGate(max_hold_s=1.0)
        commanded = {'nh3_rich': 100.0, 'ch4_pilot': 10.0}
        stuck = {'nh3_rich': 100.0, 'ch4_pilot': 0.0}
        for _ in range(3):          # 0.75 s of holding, still inside the box
            self.assertTrue(gate.check(self.tracks, commanded, stuck, 0.25))
        self.assertFalse(gate.check(self.tracks, commanded, stuck, 0.25))
        self.assertTrue(gate.timed_out)
        self.assertFalse(gate.holding)
        self.assertIn('check that line', gate.reason)
        # And it stays out of the way rather than holding every other tick.
        self.assertFalse(gate.check(self.tracks, commanded, stuck, 0.25))

    def test_a_gate_that_has_given_up_re_arms_once_the_flows_come_back(self):
        gate = SettleGate(max_hold_s=0.5)
        commanded = {'nh3_rich': 100.0, 'ch4_pilot': 10.0}
        gate.check(self.tracks, commanded,
                   {'nh3_rich': 100.0, 'ch4_pilot': 0.0}, 0.6)
        self.assertTrue(gate.timed_out)
        gate.check(self.tracks, commanded,
                   {'nh3_rich': 100.0, 'ch4_pilot': 10.0}, 0.1)
        self.assertFalse(gate.timed_out)
        # A fresh excursion on another line is held on its own budget.
        self.assertTrue(gate.check(self.tracks, commanded,
                                   {'nh3_rich': 0.0, 'ch4_pilot': 10.0}, 0.1))

    def test_a_disabled_gate_never_holds(self):
        gate = SettleGate(enabled=False)
        self.assertFalse(gate.check(self.tracks, {'nh3_rich': 100.0},
                                    {'nh3_rich': 0.0}, 0.1))
        self.assertFalse(gate.holding)

    def test_turning_the_gate_off_mid_hold_releases_the_clock(self):
        self.assertTrue(self.check({'nh3_rich': 0.0, 'ch4_pilot': 10.0}))
        self.gate.enabled = False
        self.assertFalse(self.check({'nh3_rich': 0.0, 'ch4_pilot': 10.0}))
        self.assertFalse(self.gate.holding)
        self.assertEqual(self.gate.reason, '')

    def test_the_tolerance_can_be_retuned_in_place(self):
        self.assertTrue(self.check({'nh3_rich': 80.0, 'ch4_pilot': 10.0}))
        self.gate.tolerance = 0.5
        self.assertFalse(self.check({'nh3_rich': 80.0, 'ch4_pilot': 10.0}))


if __name__ == '__main__':
    unittest.main()
