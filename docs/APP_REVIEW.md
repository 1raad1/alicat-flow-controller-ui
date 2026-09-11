# Application review, 4 September 2026

Reviewed the implementation against the README's operating goals: manual
review of targets before sending, priority zeroing, repeatable sequences,
validated measurement windows, and responsive monitoring. Changes were tested
without connected hardware.

Validation: all 736 tests passed using the installed project environment and
`python -m unittest discover -s tests -v` (40.3 seconds). The original suite
passed 716 tests before the changes. New regressions cover the defects below,
including sparse graph acquisition, history rotation, and return-ramp timing.

## Logic fixes

- **Priority zero:** a request could wait through the configured polling delay
  or behind unrelated writes. The serial loop now checks for zero between
  writes and reconnect restores, removes affected commands from its local
  batch, and leaves the polling wait within one 50 ms sleep slice. A serial
  transaction already in progress must still finish or time out.
- **Reconnect state:** a nonzero write completing after a zero request could
  overwrite the cleared reconnect target. Zero locks now protect that target
  and apply during restoration. All positive commands, including values below
  0.001 SLPM, are blocked while the target is locked.
- **Sequence repeats:** returning to the opening setpoints overlapped the next
  pass's timeline. Linear and smooth opening segments could therefore be
  skipped in part. Return commands now complete under the current ramp limits
  before the next pass's clock advances; discrepancy holds still apply.
- **Saved measurement windows:** valid averages could hide saved flow or
  setpoint extrema outside the capture tolerance. Loading now checks extrema
  against the same 3% or 0.05 SLPM envelope, allowing only floating-point
  rounding. Legacy windows without these statistics remain supported.
- **Experiment plans:** missing or malformed referenced sequences now report
  a validation failure instead of raising an uncaught exception. A new plan
  without a path no longer inherits the previous plan's directory.

## Performance

History exports previously indexed every deque once per row, which becomes
quadratic as history grows. Each column is now copied once to a list. Timings
on this machine for five units and all six metrics:

| Retained samples | Before | After |
| --- | ---: | ---: |
| 3,600 | 0.016 s | 0.010 s |
| 30,000 | 0.928 s | 0.095 s |
| 100,000 | 19.018 s | 0.352 s |

These timings cover row preparation, excluding file writing.

Graphs now rebuild curves only when history changes. Revisions also change
when a full history rotates, units change, the history is cleared, or its
limit changes. Automatic axes retain their five-tick update cadence, including
when acquisition is slow. Synthetic histories used by existing benchmarks
continue to render without revision tracking.

## Copy cleanup

Shortened the controller heading, sequence controls and help text. Removed
repetitive narrative comments from the main window and corrected claims about
zero selection, serial ownership and repeat ramp settings. Marked the retired
agent-terminal design as historical and removed obsolete branch and local
backup instructions from the pressure guide.

## Follow-up, 7 September 2026

CSV/XLSX exports now write on a background worker. The UI only copies an
immutable snapshot after a destination is chosen; row formatting and file I/O
run off the UI thread. Excel exports use write-only workbooks to avoid building
every cell in memory.

The worker belongs to the session, so theme rebuilds preserve the job and its
busy state. A cancel button stops the export. The destination is replaced only
after a complete write; cancellation and failure remove temporary output and
preserve an existing file. Closing requests cancellation and defers shutdown
if a write is still finishing.

Copying a 100,000-row, 30-column snapshot took 0.0128 seconds on this machine;
row iteration took 0.2285 seconds on the worker, excluding file writing. The
full suite now passes all 748 tests, including cancellation, file preservation,
Excel temporary-file cleanup, and theme-rebuild coverage.

## Further work

Unsupported diagnostic telemetry that times out without an explicit rejection
is retried each pass. A retry backoff could improve polling on affected meters,
but should be tested against their firmware so temporary failures can recover.

The retired agent modules still contribute installation dependencies. Making
them optional would reduce installation size; it does not address active
monitoring CPU usage.

Hardware acceptance remains outstanding for serial timing, reconnect readback
and physical flow response. The tests exercise simulated controllers and Qt
behavior; they do not establish burner safety.
