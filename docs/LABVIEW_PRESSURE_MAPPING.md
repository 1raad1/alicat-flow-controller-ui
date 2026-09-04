# LabVIEW pressure recording and NO mapping

Use LabVIEW's existing recording controls and localhost UDP `log`/`stop` messages. No LabVIEW code changes or JSON messages are required. Configure the TDMS source once in the flow app, arm each test there, then start and stop recording in LabVIEW as usual.

The flow app associates both pressure waveforms with that physical recording interval and calculates their metrics afterwards. NO collection can continue after LabVIEW stops. Keep the same burner condition and flows throughout that extra collection.

## Configure the TDMS source

1. Create a campaign with **Map NO + pressure**. New campaigns map the dominant in-band spectral amplitude from each of two pressure transducers. Set the operating bounds, flow ceilings, NO mapping weight and NO measurement window. The app oxygen-corrects the analyser's dry NO readings; this is not a total-NOx measurement. Existing one-transducer campaigns retain their saved response choice.
2. In **Current test**, click **TDMS source…**. Set **Recording folder** to the folder where LabVIEW saves its `.tdms` files. The app searches that folder directly, not its subfolders. The flow PC must be able to read it.
3. Click **Inspect sample TDMS…** and select a representative recording. Choose two distinct time-domain waveforms, one for each pressure transducer. Give each a clear display label that identifies its physical location. The saved IDs remain `pressure_1` and `pressure_2`, so changing a label does not change which model receives the values. The picker initially prefers the `converted` group. Confirm each actual channel after checking its sensor assignment. The `FFT` group contains spectra rather than time-domain waveforms and is excluded.
4. For each transducer, select **Known pressure units** (Pa or kPa), or enter a custom **Pressure scale (Pa per stored unit)**, and supply its **Calibration identifier**. Check its **Pressure offset (Pa)** and optional clipping limits independently. The calculation is `pressure = stored value × scale + offset`. The Pa shortcut sets that channel's scale to `1`; kPa sets it to `1000`. A `converted` group name does not establish physical units. Resolve both conversions from the acquisition setup before saving the profile.
5. Set **Minimum pressure recording (s)** independently of the campaign's NO averaging window. Its default is 1 second. The inspected sample has 21,082 samples at 10,000 Hz, equivalent to 2.1082 seconds of samples; it does not need to be stretched into a 30-second pressure recording. LabVIEW remains in control of recording duration. The NO window has its own minimum, at least 5 seconds under the campaign settings.
6. Review the shared **Spectrum lower frequency (Hz)**, **Spectrum upper frequency (Hz)**, **Spectral segment (samples)** and **Spectral overlap (samples)** settings. A blank upper frequency uses Nyquist. The defaults are 4,096-sample segments with 2,048-sample overlap. Segments must fit the selected record. Clipping limits are in each channel's stored units; leaving them blank means clipping was not checked.
7. Leave **Fallback sample rate (Hz)** blank when TDMS contains valid waveform timing. If a file lacks a start timestamp, the checkbox **If TDMS has no start timestamp, use trigger time / I confirm LabVIEW writes one new file per trigger** is an explicit fallback. Enable it only when that recording arrangement is true. File modification time is never used as the sample start time. Save the profile with **OK**.

The app reads both selected channels through npTDMS, including any NI scaling represented by the file. Enter the Pa conversions for those returned values, not additional ADC conversions that would apply the same scaling twice. Both selected slices must have the same rate, start and end time, sample count and timing source. Keep the channel assignments, labels, calibrations, sample rate and analysis settings consistent within a campaign. The first attached pressure result locks those comparison settings.

TDMS support requires the optional `npTDMS` dependency. It is installed in this PC's flow-controller environment. For another installation, run `python -m pip install -r requirements-pressure.txt` using that application's Python environment.

The expected amplitudes, about 0.2 kPa when stable and 3–6 kPa when unstable, are useful checks after calibration. They are not calibration factors or automatic classification thresholds. The sample's numerical RMS of about 0.14108 stored units would be 141.08 Pa if those units are kPa. That agreement is consistent with the expected stable scale, but does not prove the stored units.

## Run each test

1. Click **Suggest next test**, then **Load target fields**. Review and apply the targets through the existing flow controls. Suggestions, field loading and LabVIEW triggers do not command the flows automatically.
2. In the operation tab, check **LabVIEW UDP host** is `127.0.0.1` and **LabVIEW UDP port** is `61557`, matching the existing local LabVIEW messages. Click **Start Listener** if it is off.
3. In **Current test**, confirm **Pilot is off throughout this measurement** and **Burner and flows are settled; analyser is settled or a calibrated delay is available**. For live NO/O2, select **Capture NO/O2 automatically from the MEXA network link** and use the validated live analyser connection. Select the appropriate calibrated analyser response delay before this run.
4. Click **Arm LabVIEW trigger**. The app records the current TDMS folder contents so it can distinguish a new or changed recording. Now start recording with the existing LabVIEW control that sends `log`.
5. Record for the duration you choose, meeting the source profile's minimum pressure duration. Stop using LabVIEW's existing control, which sends `stop`. Let LabVIEW finish writing its TDMS file.
6. Keep the condition steady while the flow app finishes NO and flow collection. Do not apply the next operating point at LabVIEW stop. Watch the status in **Current test**: it shows the remaining collection after stop, then the TDMS search and processing.
7. Review the attached metrics for both transducers and the matching saved NO/O2 averages. Confirm **Uncorrected dry averages from this saved window**, then click **Save result**. Pressure processing does not save the NO result automatically. A mapping trial cannot be completed, or used to advance the mapping campaign, without both valid pressure results.

Arming is required for `log`/`stop` to control the optimiser capture. Without local arming, the existing commands retain their ordinary flow-CSV logging behaviour.

## Why NO collection continues after stop

The app collects around the LabVIEW `log`–`stop` interval and keeps collecting after `stop` for the selected calibrated NO response delay. It excludes the initial delay from the NO averaging window. It extends collection further when the campaign's minimum averaging duration or fresh flow/MEXA sample coverage still needs to be met. The saved NO window therefore need not have the same duration as the pressure record.

For example, a roughly 2-second pressure recording can be paired with a longer NO averaging window. That only represents the same operating condition if the burner and flows remain steady throughout the run and the collection after stop. The delay setting accounts for the selected analyser response calibration; UDP triggers do not create hardware synchronisation.

## Finding the correct TDMS record

After NO collection finishes, the app searches in the background for a new or changed TDMS file relative to the snapshot taken at arm. It uses both selected waveforms' timestamps and sample rates to match the physical `log`–`stop` interval. A file must remain unchanged for 2 seconds before processing; incomplete final segments, disagreement between the two selected slices and detected changes during processing are rejected.

For one file covering the run, the app can use the complete waveform when its timing agrees within the association tolerance. For a continuous recording, it selects the samples covering the physical trigger interval. It does not extend pressure selection into the later NO collection. The source TDMS file stays unchanged. The saved result retains the source path, SHA-256, selected sample offset and timing association so the raw recording can be analysed again later.

If no file matches, or more than one matches, the app reports the problem rather than choosing the newest file. Once the background operation finishes, click **Choose TDMS file…** and select the intended recording to retry. Check its channel, timing and calibration if it is rejected. Manual selection still validates the waveform; it does not silently waive those checks. Preserve the current trial until pressure is attached, or mark the test invalid if the recording cannot be recovered.

## What the pressure values and maps mean

For each transducer, RMS is calculated from the complete selected waveform after subtracting its mean. Peak excursion is the largest absolute deviation from that mean. Dominant spectral amplitude is the RMS amplitude at the strongest in-band Welch spectrum bin, with its frequency reported separately. It is a sinusoidal spectral amplitude, not peak-to-peak pressure or a PSD value. The analysis uses periodic flattop windows and constant segment detrending. The frequency band restricts the dominant-bin search; it does not band-pass the RMS or peak calculation. Optional block-RMS spread describes variability and is not a standard error.

Initial suggestions fill the operating space. Later suggestions fit three separate standardized Gaussian-process models: corrected dry NO, pressure-1 dominant spectral amplitude and pressure-2 dominant spectral amplitude. If the configured NO mapping weight is $w$, the acquisition score is $wR_{NO}+(1-w)(R_{P1}+R_{P2})/2$. It allocates experiments by expected uncertainty reduction; it does not create a combined NO/stability performance score. RMS, peak excursion, block-RMS spread and the dominant frequencies remain recorded for later diagnosis but do not drive new dual-transducer suggestions.

After the initial completed design, open **Operating-space maps**, choose **Horizontal** and **Vertical**, then click **Refresh maps**. Other variables stay at the selected completed test's measured condition, or their bounds midpoint. **Show uncertainty (latent SD)** switches between predicted means and uncertainty. Blank cells exceed bounds or flow ceilings. These response maps do not classify flame stability or establish a safe operating region.

## Existing JSON support and previous version

The `flow-pressure-v1` JSON summary and file-ready routes remain available for advanced integrations and compatibility. They are optional; this TDMS workflow does not require exported IDs, a manifest, UDP acknowledgements or changes to the LabVIEW VI. The files in `docs/examples` illustrate those advanced payloads rather than the normal recording procedure.

The working branch is `codex/nox-pressure-mapping`. The prior version is preserved in the sibling folder `flow-controller-backups/flow-controller-v3-before-pressure-mapping-20260903-133746`; Git tag `backup/before-pressure-mapping-20260903-133746` marks starting commit `aa78cbe`. This version writes campaign schema 4 and supports schema 1–3 campaigns. Keep copies of campaign files before switching versions: the backup application cannot read the new schema.
