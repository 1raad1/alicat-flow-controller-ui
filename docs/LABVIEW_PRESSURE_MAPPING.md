# LabVIEW pressure recording and NO mapping

The working branch is `codex/nox-pressure-mapping`. The analyser response used here is corrected dry **NO**, not total NOx. The branch name does not change that measurement basis.

The previous version is preserved in the sibling folder `flow-controller-backups/flow-controller-v3-before-pressure-mapping-20260903-133746`, and Git tag `backup/before-pressure-mapping-20260903-133746` marks starting commit `aa78cbe`. Keep campaign files copied separately when switching versions. This version writes campaign schema 3 and supports loading schema 1/2 campaigns. The backup application is not forward compatible with schema 3.

## Set up the campaign and listener

1. Create a campaign with objective **Map NO + pressure**. Choose **RMS (Pa)**, **Peak excursion (Pa)**, or **Dominant spectral amplitude (Pa RMS)** as the pressure response. Set the NO mapping weight strictly between 0 and 1; the remaining weight goes to pressure. Keep the established bounds, flow ceilings and measurement duration.
2. Click **Suggest next test**, then **Load target fields**. Review and apply the targets through the existing controls. These buttons and the LabVIEW protocol do not apply flows automatically.
3. In the operation tab, set **LabVIEW UDP host** and **LabVIEW UDP port**, then click **Start Listener**. The defaults are `127.0.0.1` and `61557` for LabVIEW on the same PC. For another PC, explicitly bind the listener to the flow PC's LAN address and send from LabVIEW to that address. Allow the chosen UDP port through the local firewall for the acquisition connection.
4. In **Current test**, confirm **Pilot is off throughout this measurement** and **Burner and flows are settled; analyser is settled or a calibrated delay is available**. Select **Capture NO/O2 automatically from the MEXA network link** only when that validated live link is ready. Click **Arm LabVIEW trigger**. Arming checks current readings and does not bypass the usual operator confirmations.
5. Click **Export LabVIEW request…** for this trial. Load that JSON file into the VI. Copy its `experiment_id`, `trial_id` and `capture_id` into every message for this acquisition. Do not manufacture these IDs or reuse them for the next trial. Discarding a capture changes its identity; export again after a restart.

## Change the LabVIEW VI

Keep the existing pressure DAQ and raw recording. Add a small state machine around the recording start and stop, plus a JSON result writer. Use the compact summary route below when the VI can calculate the defined metrics; otherwise send a manifest for a completed CSV or TDMS file.

1. Call **UDP Open** once, using an available local port. Retain its refnum for **UDP Write** and **UDP Read** in the state-machine loop, then **UDP Close** when the VI ends. Read replies on the same socket that sent the request: the flow app replies to the sender's address and port. Do not open a second unrelated receive socket. NI's [communication reference](https://download.ni.com/support/manuals/320587c.pdf) describes the UDP Open/Read/Write functions.
2. Serialize one JSON object as UTF-8 bytes per datagram. Preserve JSON numbers, integers and booleans as their actual types; strings such as `"false"` and `"1000"` are invalid for those fields. The UDP limit is 16,384 bytes, and a pressure summary must be strictly smaller than that. Do not send waveform arrays over UDP.
3. Include `protocol: "flow-pressure-v1"`, a `type`, the three exported IDs, and a `request_id` string in each operation. IDs and request IDs in UDP messages must be 1–128 characters. Retain the exported start request ID for start retries. Give stop, summary/file-ready and each new status query their own request IDs, for example `capture-id:stop` and `capture-id:status:1`.
4. Send the exported `start` object. Wait for a JSON `ack` with `ok: true` and matching `request_id` and acquisition IDs. A successful write alone is not an acknowledgement.
5. Inspect `state`. If it is `waiting_for_analyser`, send `status` queries until it becomes `capturing`. Begin the pressure record only then. The flow/NO averaging window starts after any selected live-analyser response delay. The reply includes `minimum_recording_s`, `window_start` and `window_end`; the last field is null until the window is saved.
6. Record for at least `minimum_recording_s`. Keep flow polling running throughout. The flow window also needs at least three fresh polling passes and its own minimum elapsed duration. The minimum is a floor, so allow the final flow pass to reach the configured duration before sending `stop`. The current-test progress displays fresh passes. This protocol does not expose a pass counter in status replies.
7. Stop the pressure recording and retain the first/last sample times. Send `stop`; require a successful acknowledgement with `window_saved`. If it reports an insufficient flow window, keep the operating condition steady, allow another fresh pass, and retry within a bounded interval. A successful stop saves the flow/NO window. It closes the flow CSV only if this start operation opened it.
8. Finish writing and close the raw pressure file. Send either `pressure_summary` or `file_ready` using the definitions below. A summary acknowledgement should reach `pressure_saved`. A file-ready acknowledgement can be `processing`; keep querying `status` until `pressure_saved` or `pressure_error` appears. Read the `pressure_error` field on failure. `ok: true` on file-ready means the request was accepted, not that processing succeeded.
9. In **Current test**, review the saved pressure metrics and the matching NO/O2 averages. Confirm **Uncorrected dry averages from this saved window**, then click **Save result**. Pressure upload never finalises the NO result. A later status query returns `completed` after the operator saves it.

A status or stop message has the same identity fields as the exported request, with `type` changed to `status` or `stop` and its own `request_id`. Replies have this shape; the values here are placeholders:

```json
{
  "protocol": "flow-pressure-v1",
  "type": "ack",
  "ok": true,
  "request_id": "replace-with-capture-id:status:1",
  "experiment_id": "replace-with-exported-experiment-id",
  "trial_id": "replace-with-exported-trial-id",
  "capture_id": "replace-with-exported-capture-id",
  "state": "capturing",
  "window_start": "2026-09-03T12:00:00+00:00",
  "window_end": null,
  "minimum_recording_s": 30.0
}
```

Use a finite receive timeout, for example 1 second, and retry an unchanged operation at most three times before reporting a communication failure. Match request IDs so a delayed reply cannot complete the wrong operation. Query state after an uncertain result. Keep status polling bounded too; use a deadline that allows the configured analyser delay or file processing, and show an error if it expires. `ok: false` carries an `error` string; correct the reported problem rather than retrying indefinitely.

Identical start/stop retries do not create another window. Identical pressure summaries are acknowledged without overwriting the saved result. Repeated unchanged file-ready requests are recognised during processing; query status after completion. A different result cannot overwrite an attached capture. A discarded or unknown capture ID is rejected. Do not advance to a new trial merely because a UDP packet was sent.

## Calculate the compact pressure summary

Start from [pressure-summary.json](examples/pressure-summary.json). Replace every example identity, timestamp, calibration and metric with acquisition data. The values illustrate a 30-second record at 1,000 Hz: 30,000 samples, a 1,000-sample segment and 500-sample overlap. The 10–400 Hz analysis band is an example, not a recommendation for this burner or sensor.

Convert the selected DAQ channel to physical Pa using its calibration before calculating metrics. Record a calibration ID that identifies the applied calibration. For source units `u`, the file-processing convention is `p = u * scale_pa_per_unit + offset_pa`. Use scale 1 and offset 0 only when the stored samples are already Pa.

For the complete calibrated record `p`, form `x = p - mean(p)`. Calculate:

| Field | Definition |
|---|---|
| `rms_pa` | `sqrt(mean(x*x))` over the complete record |
| `peak_abs_pa` | `max(abs(x))`, the largest sampled excursion from the complete-record mean |
| `dominant_frequency_hz` | Frequency of the largest Welch spectrum bin inside `analysis.band_hz`, including the band endpoints |
| `dominant_amplitude_pa` | Square root of that bin's averaged power spectrum, in Pa RMS |
| `rms_window_sd_pa` | Optional population SD of RMS values from complete, nonoverlapping `segment_samples` blocks of `x`; this is variability, not SEM |

The band limits only the dominant spectral search. It does not band-pass the record used for RMS or peak excursion.

Use periodic flattop windows, constant detrending in each segment, and arithmetic averaging of the segment power spectra. To match the Python processor, use an unnormalised forward FFT and divide squared FFT magnitude by `sum(window)^2`. Double the positive-frequency bins except DC and the Nyquist bin when present. Average those power spectra, then take the square root at the selected dominant bin. Do not substitute a PSD in Pa²/Hz or a peak-amplitude FFT display without converting its scaling. SciPy documents the [Welch spectrum/RMS convention](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html).

If the VI's window function does not expose this periodic convention, generate the coefficients for `n = 0..L-1` with `theta = 2*pi*n/L`:

```text
w[n] = 0.21557895 - 0.41663158*cos(theta)
       + 0.277263158*cos(2*theta) - 0.083578947*cos(3*theta)
       + 0.006947368*cos(4*theta)
```

Use `L`, not `L-1`, in the angle denominator. Advance frames by `segment_samples - overlap_samples` and exclude incomplete final frames.

Give the actual LabVIEW implementation a meaningful `analysis.id`, such as `labview-welch-flattop-v1` after implementing and checking that calculation. Preserve its settings in the summary. Keep the channel, calibration ID, sample rate, units and full analysis settings fixed across a campaign. The first saved pressure result locks this comparison signature. The Python file processor sets its own ID, `python-welch-flattop-v1`, and records scale and offset. The two example routes are alternatives; changing implementation or settings can require a new campaign.

Set `quality.clipped` from actual DAQ overrange/clipping checks and `quality.nonfinite` from actual sample validity checks. The example's false values describe its clean illustrative record; do not hard-code them in the VI. Flagged captures are rejected. Save the raw record for investigation and use the existing discard/invalid-test workflow if it cannot supply a valid measurement. Optional `raw_file` and `raw_sha256` fields in a summary retain the recording location and its SHA-256; the summary route does not reopen or verify that raw file.

## Use sample times, not message times

`start` is the UTC time of the first pressure sample. `end` is the UTC time of the last pressure sample. At a fixed sample rate `fs` with `N` samples, `end = start + (N-1)/fs`. Thus the example ends at `2026-09-03T12:00:29.999+00:00`, although `N/fs` is 30 seconds. The campaign's minimum recording-duration check uses `N/fs`.

LabVIEW timestamps count from 1904-01-01 UTC. Format the timestamp using **Format Date/Time String** with UTC formatting enabled, preserving fractional seconds, then include `Z` or `+00:00`. Do not label local time as UTC. If converting a numeric LabVIEW timestamp through Unix time, subtract 2,082,844,800 seconds first. See NI's [timestamp definition](https://www.ni.com/en/support/documentation/supplemental/08/labview-timestamp-overview.html) and [date/time formatting function](https://www.ni.com/docs/en-US/bundle/labview-api-ref/page/functions/format-date-time-string_1.html).

Align the two PCs' clocks before acquisition. The pressure record must overlap and lie inside the saved flow/NO window, allowing 2 seconds at each boundary. This is an association check, not hardware synchronisation. A shared DAQ trigger or clock, if needed, must be implemented separately. UDP send times are not sample timestamps. Account for any DAQ timing offsets when identifying the first sample.

## Let the flow PC process a completed file

Start from [pressure-file-ready.json](examples/pressure-file-ready.json). For CSV, write a header and one numeric selected-channel value per row; the example selects `pressure_pa`. Other columns can remain in the file, but they are not analysed. For TDMS, change `format` to `tdms`, remove `column`, add `group`, and set `channel` to the exact selected TDMS channel name. TDMS reading requires the optional `npTDMS` package in the flow application's Python environment.

The current development environment has that dependency installed. On another installation, run this PowerShell command from the project folder:

```powershell
& "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install -r .\requirements-pressure.txt
```

For package installations, the equivalent extra is `pip install '.[pressure]'`. JSON summaries and CSV imports need no extra package. When the TDMS channel contains `wf_increment`, its sample rate must agree with the manifest. The parser rejects a mismatch.

Use an absolute `raw_file` path accessible from the flow PC, such as a local completed file or a shared-folder UNC path. A LabVIEW PC's local drive path is not automatically accessible from the flow PC. Close the recording before announcing it. The processor reads only the selected channel, checks file size and modification state across processing/hashing, and rejects detected changes. Keep the raw file below 1 GB and at most 20 million selected-channel samples. Reading, metric calculation and hashing run in a background worker.

The manifest supplies the first-sample time and sample rate. Do not add `sample_count` or `end` fields to it; these are derived from the selected-channel record. The example expects a separate 30,000-row CSV, which is not included. The parser applies scale and offset, calculates the defined metrics, and retains the absolute file path and SHA-256 in the saved summary.

UDP is optional. After saving the measurement window, click **Import pressure JSON…** and select a summary JSON or file-ready JSON manifest. A relative `raw_file` in an imported manifest resolves relative to that JSON file's folder. You cannot select a bare CSV or TDMS file without its manifest. Keep the exported acquisition IDs in the imported JSON. Review pressure and complete the same **Save result** step afterwards.

## Existing triggers and the resulting maps

Bare legacy `log` and `stop` messages retain their flow-CSV logging behaviour. They also start/finish an optimiser window only when the current trial has been locally armed. Prefer the correlated JSON sequence: bare commands have no acquisition-ID acknowledgement and do not establish hardware synchronisation. In particular, starting pressure immediately with a bare `log` can include the analyser delay; use JSON status to wait for `capturing`, or start pressure later after checking the actual averaging window.

The initial mapping points use a space-filling design. Later suggestions fit separate standardized Gaussian-process models for corrected dry NO and the chosen pressure amplitude. The next point targets weighted reduction of their uncertainty over the feasible operating space. The weight does not turn NO and pressure into a combined emissions/pressure objective.

After the initial completed design, open **Operating-space maps**, choose **Horizontal** and **Vertical** variables, and click **Refresh maps**. The other variables stay at the selected completed test's measured condition, or their bounds midpoint. **Show uncertainty (latent SD)** switches between predicted mean and latent standard deviation for each response. Blank cells exceed bounds or flow ceilings. A low predicted pressure amplitude does not classify a flame as stable, and these slices do not establish a safe operating region.
