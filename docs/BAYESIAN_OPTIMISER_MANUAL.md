# Bayesian optimiser technical manual

This manual explains the Bayesian experiment workflow implemented by Flow
Controller v3. It covers NO minimisation, joint NO/pressure mapping, LabVIEW
TDMS acquisition, numerical techniques, records, variable controls and the
boundaries between calculation, operator approval and
actuation. Code links point to the implementation at the time this manual was
written; use the named symbol if later edits move a line.

Published-source citations identify the established technique or reporting
practice on which a section is based. They do not imply that a paper validates
this particular burner, analyser, search region or software implementation.
Unless a paragraph says otherwise, numerical tolerances, persistence counts,
timeouts and safety gates are application-specific engineering choices.

For the current recording procedure, start with [LabVIEW/TDMS capture](#labviewtdms-capture-workflow).
The [LabVIEW setup guide](LABVIEW_PRESSURE_MAPPING.md) gives the full source-profile
and file-selection instructions. [Mapping acquisition](#mapping-acquisition-and-operating-space-maps)
explains how the next mapping test is selected.

## 1. Purpose and operating boundary

The optimiser supports two campaign modes for a pilot-off NH3/H2
rich-quench-lean experiment:

| Mode | What the next proposal seeks |
| --- | --- |
| **Minimise NO** | Lower dry NO corrected to a fixed dry-O2 reference. |
| **Map NO + pressure** | Reduce uncertainty in separate maps of corrected dry NO and the dominant spectral amplitude from each of two pressure transducers. |

Both modes propose one operating point at a time. A proposal does **not** send a controller command. The operator
must review the calculated flows, load them into the existing target fields,
apply them through the normal controls, wait for the rig and analyser to settle,
and start a measurement window or locally arm the LabVIEW recording trigger.
The objective mode and mapping weight are fixed when the campaign is created.
New mapping campaigns use two pressure transducers and dominant spectral
amplitude. Existing one-transducer campaigns retain their saved pressure response;
campaigns without objective settings open in **Minimise NO** mode.

Parameter bounds and controller MAX FLOW values are feasibility constraints.
They are not learned flame-stability limits and they do not establish that a
transition between two feasible endpoints is safe.

The staged rig may use NH3, H2, or CH4 for its single pilot assignment. Those
assignments are registered in
[`roles.py:12`](../flow_controller/domain/roles.py#L12), and the selected pilot fuel is
included in the Stage 1 and global live combustion balances in
[`session.py:1564`](../flow_controller/core/session.py#L1564). The optimiser deliberately
excludes pilot flow from its NH3/H2 search mixture: every assigned pilot line
must read zero before a measurement window can start. That check is implemented
by `_checked_readings()` in
[`optimiser_controller.py:278`](../flow_controller/core/optimiser_controller.py#L278).

The separation is visible in the controller:

- suggestion calculation runs in a background worker at
  [`optimiser_controller.py:25`](../flow_controller/core/optimiser_controller.py#L25);
- `ask()` requests and saves one proposal at
  [`optimiser_controller.py:190`](../flow_controller/core/optimiser_controller.py#L190);
- `prepare_targets()` fills fields without sending commands at
  [`optimiser_controller.py:262`](../flow_controller/core/optimiser_controller.py#L262);
- capture and completion require separate operator actions at
  [`optimiser_controller.py:679`](../flow_controller/core/optimiser_controller.py#L679)
  and [`optimiser_controller.py:883`](../flow_controller/core/optimiser_controller.py#L883).

## 2. Search variables

An **operating point** is one complete set of input values proposed for a
single steady experiment. Every campaign searches these three base variables:

1. $x_{H2}$, the H2 volume fraction in the NH3/H2 fuel blend, dimensionless
   and strictly between 0 and 1;
2. $\phi_1$, the Stage 1 equivalence ratio, dimensionless;
3. $\phi_g$, the overall or global equivalence ratio, dimensionless.

New campaigns may add either or both of:

4. $P$, the total lower-heating-value thermal input, in kW;
5. $s$, the fraction of the total fuel volume sent to Stage 1, dimensionless
   and greater than 0 but no greater than 1.

The variable registry is at
[`bayesian.py:29`](../flow_controller/domain/bayesian.py#L29). Configuration,
dimensionality, bound validation and backward-compatible defaults are implemented
by `SearchConfig` at
[`bayesian.py:70`](../flow_controller/domain/bayesian.py#L70). Old experiment
files omit the two optimisation flags and therefore reopen as the original
three-variable campaigns.

The order of the saved `point` array is:

```text
[h2_fraction, phi_stage1, phi_overall, bulk_velocity?, power_kw?, split_rich?]
```

A campaign may instead search $U_1$, the cold-flow bulk velocity at the Stage 1
inlet in m s$^{-1}$, in place of thermal input. `bulk_velocity` and `power_kw`
are never both present: with a searched velocity, thermal input is calculated
from the other variables and is not an independent setting. See
[Bulk velocity as the throughput variable](#bulk-velocity-as-the-throughput-variable).

The optional coordinates appear only when their corresponding configuration
flag is true. `SearchConfig.values()` validates and names this array at
[`bayesian.py:177`](../flow_controller/domain/bayesian.py#L177), and
`SearchConfig.request()` converts it into a flow-calculation request at
[`bayesian.py:195`](../flow_controller/domain/bayesian.py#L195).

### Creating a campaign

In **New Bayesian experiment**:

1. Choose **Minimise NO** or **Map NO + pressure**. For mapping, set the
   **NO mapping weight (0–1)**. The default weight is 0.5; it must be strictly
   between 0 and 1 so that the NO map and both pressure maps contribute. Enter the
   nominal/fixed power and stage-1 split.
2. Enter bounds for H2 fraction, stage-1 phi and overall phi.
3. Select **Optimise thermal input** and/or **Optimise stage-1 fuel split** only
   when those variables should be searched. To set limits on bulk velocity
   instead of power, select **Search Stage 1 inlet bulk velocity** and check the
   **Stage 1 inlet area**. The area is prefilled from the Stage 1 inlet geometry
   declared on the combustion card and is then fixed for the campaign. Selecting
   velocity clears and disables the thermal-input option.
4. Enter a lower and upper bound for every selected optional variable. The
   nominal value must lie inside its selected bounds.
5. Choose the initial-design and candidate-pool sizes. The **initial design**
   is the set of measurements selected for coverage before the statistical
   model chooses points. A **candidate pool** is the finite set of possible
   points scored during one call to the optimiser; it is a numerical search
   set, not experimental data.
6. Enter the O2 reference, or click **Use current O₂** beside that field to copy
   the latest eligible analyser reading. Enter the measurement-window duration.
7. Confirm the reporting basis and the independently checked operating region.

The dynamic dialog is implemented at
[`qt_optimiser.py:456`](../flow_controller/ui/qt_optimiser.py#L456), with conversion
from percentages to fractions at
[`qt_optimiser.py:714`](../flow_controller/ui/qt_optimiser.py#L714).

**Use current O₂** copies one reading, not a time average. Let the chosen
baseline condition settle before using it. The reading must be fresh, real
(not simulated), validated and reported on an uncorrected dry basis, with
receiver logging enabled. If it fails these checks, the dialog shows an error
and leaves the reference unchanged. The copy and its acquisition time are
displayed by
[`qt_optimiser.py:764`](../flow_controller/ui/qt_optimiser.py#L764), using the
existing sample checks at
[`mexa_controller.py:195`](../flow_controller/core/mexa_controller.py#L195).

You can edit the copied value or click again before saving. The dialog passes
the field into `reference_o2` (the reporting oxygen percentage for the campaign)
at [`qt_optimiser.py:729`](../flow_controller/ui/qt_optimiser.py#L729). Saving fixes
this value for the campaign; later readings do not update it. The button sends
no flow commands and performs no calibration; measured O2 still varies with
each test condition.

Here $d$ is the number of selected search variables, so it is 3, 4, or 5. The
program requires at least $d+1$ completed initial points. This validation rule
is implemented at
[`bayesian.py:126`](../flow_controller/domain/bayesian.py#L126) and
[`bayesian.py:129`](../flow_controller/domain/bayesian.py#L129). It is only a
validity floor, not a claim that $d+1$ tests fully
characterise the response. Higher-dimensional searches normally need more
initial measurements and a larger candidate pool.

## 3. Flow-target equations

The pure flow calculation starts at
[`rql.py:90`](../flow_controller/domain/rql.py#L90). Every SLPM value in these
equations uses Alicat's default standard conditions: 25 °C and 14.696 psia.
Those constants are defined at
[`gas_properties.py:12`](../flow_controller/domain/gas_properties.py#L12) to
[`gas_properties.py:13`](../flow_controller/domain/gas_properties.py#L13),
following Alicat's [default STP
definition](https://www.alicat.com/support/what-is-mass-flow/).

Let $x_{NH3}$ be the NH3
volume fraction, dimensionless. Since the fuel contains only NH3 and H2,

$$
x_{NH3}=1-x_{H2}.
$$

Implemented at [`rql.py:103`](../flow_controller/domain/rql.py#L103) and
[`rql.py:104`](../flow_controller/domain/rql.py#L104).

Let $\rho_i$ be the density of gas $i$ at 25 °C and 14.696 psia, in
kg m$^{-3}$; the subscript `mix` denotes the NH3/H2 blend. The exact
[Alicat Gas Select 5.0
values](https://documents.alicat.com/specifications/Alicat_Preloaded-Gases-and-Properties_Rev0.pdf)
used by the program are $\rho_{NH3}=0.70352$, $\rho_{H2}=0.08235$,
$\rho_{CH4}=0.65688$, and $\rho_{Air}=1.18402$ kg m$^{-3}$. They are stored at
[`gas_properties.py:31`](../flow_controller/domain/gas_properties.py#L31) to
[`gas_properties.py:36`](../flow_controller/domain/gas_properties.py#L36).
The blend density is

$$
\rho_{mix}=x_{NH3}\rho_{NH3}+x_{H2}\rho_{H2}.
$$

Implemented at [`rql.py:109`](../flow_controller/domain/rql.py#L109), using the
shared density mapping exposed to combustion calculations at
[`combustion.py:69`](../flow_controller/domain/combustion.py#L69).

Let $y_i$ be the mass fraction of fuel component $i$, dimensionless, and
$LHV_i$ its lower heating value, in MJ kg$^{-1}$. Lower heating value (LHV) is
the heat released per unit fuel mass when water remains as vapour. Volume
fractions are converted to mass fractions because the stored LHVs are per unit
mass. The fixed mass-based values are 18.6 MJ kg$^{-1}$ for NH3,
120 MJ kg$^{-1}$ for H2, and 50 MJ kg$^{-1}$ for CH4, defined at
[`gas_properties.py:24`](../flow_controller/domain/gas_properties.py#L24) to
[`gas_properties.py:28`](../flow_controller/domain/gas_properties.py#L28):

$$
y_{NH3}=\frac{x_{NH3}\rho_{NH3}}{\rho_{mix}},\qquad
y_{H2}=\frac{x_{H2}\rho_{H2}}{\rho_{mix}},
$$

implemented at [`rql.py:110`](../flow_controller/domain/rql.py#L110) and
[`rql.py:111`](../flow_controller/domain/rql.py#L111), followed by

$$
LHV_{mix}=y_{NH3}LHV_{NH3}+y_{H2}LHV_{H2}.
$$

Implemented at [`rql.py:112`](../flow_controller/domain/rql.py#L112).

Let $LHV_i^{(V)}$ be volumetric LHV in MJ m$^{-3}$ on the same Alicat standard
basis. It is derived, not copied from a table with a potentially different
reference volume:

$$
LHV_i^{(V)}=\rho_iLHV_i.
$$

Implemented at
[`gas_properties.py:39`](../flow_controller/domain/gas_properties.py#L39) to
[`gas_properties.py:42`](../flow_controller/domain/gas_properties.py#L42). This
gives 13.085472 MJ m$^{-3}$ for NH3, 9.882 MJ m$^{-3}$ for H2, and
32.844 MJ m$^{-3}$ for CH4.

Let $\dot m$ be total fuel mass flow in kg s$^{-1}$ and $\dot V_{fuel}$ be
total standard volumetric fuel flow in SLPM (standard litres per minute). A dot
over a quantity means a rate per unit time. For requested thermal input $P$,

$$
\dot m=\frac{P}{1000LHV_{mix}},
$$

where the factor 1000 converts the LHV from MJ kg$^{-1}$ to kJ kg$^{-1}$,
consistent with $P$ in kJ s$^{-1}$ (kW). Implemented at
[`rql.py:114`](../flow_controller/domain/rql.py#L114). The corresponding standard
volumetric flow is

$$
\dot V_{fuel}=\frac{\dot m}{\rho_{mix}}\,(60\ \mathrm{s\,min^{-1}})
(1000\ \mathrm{L\,m^{-3}}).
$$

Implemented at [`rql.py:115`](../flow_controller/domain/rql.py#L115).

The Stage 1 fuel split $s$ gives

$$
\dot V_{fuel,1}=s\dot V_{fuel}, \qquad
\dot V_{fuel,2}=(1-s)\dot V_{fuel}.
$$

Implemented at [`rql.py:116`](../flow_controller/domain/rql.py#L116) and
[`rql.py:117`](../flow_controller/domain/rql.py#L117). Each stage total is then
multiplied by $x_{NH3}$ or $x_{H2}$ to obtain its individual gas targets at
[`rql.py:119`](../flow_controller/domain/rql.py#L119),
[`rql.py:120`](../flow_controller/domain/rql.py#L120),
[`rql.py:121`](../flow_controller/domain/rql.py#L121), and
[`rql.py:122`](../flow_controller/domain/rql.py#L122).

**Stoichiometric** means exactly enough oxygen for complete combustion under
the reaction model, with neither fuel nor oxygen left over. For an NH3/H2
stream, the model uses

$$
4NH_3+3O_2\rightarrow2N_2+6H_2O,\qquad
2H_2+O_2\rightarrow2H_2O.
$$

These reactions give 0.75 mol O2 per mol NH3 and 0.50 mol O2 per mol H2,
encoded at
[`combustion.py:46`](../flow_controller/domain/combustion.py#L46). Let
$\dot V_{O2,st}$ be the required stoichiometric oxygen flow and let
$x_{O2,air}=0.209390$ be the oxygen mole fraction in dry air. The program uses
the [NIST dry-air
composition](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=921756),
which also records $x_{N2,air}=0.780848$. The unnormalised residual is

$$
x_{other,air}=1-x_{O2,air}-x_{N2,air}=0.009762,
$$

implemented at
[`gas_properties.py:17`](../flow_controller/domain/gas_properties.py#L17) to
[`gas_properties.py:19`](../flow_controller/domain/gas_properties.py#L19).
This residual represents argon, CO2, and other trace gases. N2 is retained as a
shared constant at [`gas_properties.py:18`](../flow_controller/domain/gas_properties.py#L18)
and [`combustion.py:52`](../flow_controller/domain/combustion.py#L52), but only
the O2 fraction enters the current stoichiometric demand. Because all flows
share one standard-volume basis, their volume ratios equal their mole ratios:

$$
\dot V_{O2,st}=0.75\dot V_{NH3}+0.50\dot V_{H2},\qquad
\dot V_{air,st}=\frac{\dot V_{O2,st}}{x_{O2,air}}.
$$

The oxygen sum and division by the air oxygen fraction are implemented at
[`combustion.py:102`](../flow_controller/domain/combustion.py#L102),
[`combustion.py:104`](../flow_controller/domain/combustion.py#L104), and
[`combustion.py:106`](../flow_controller/domain/combustion.py#L106); the value
of $x_{O2,air}$ is at
[`gas_properties.py:17`](../flow_controller/domain/gas_properties.py#L17) and
is connected to the combustion calculation at
[`combustion.py:49`](../flow_controller/domain/combustion.py#L49).

An **equivalence ratio** $\phi$ is stoichiometric air demand divided by air
actually supplied. Thus $\phi=1$ is stoichiometric, $\phi>1$ is fuel-rich, and
$\phi<1$ is fuel-lean. The general implemented definition is

$$
\phi=\frac{\dot V_{air,st}}{\dot V_{air}}.
$$

Implemented at [`combustion.py:109`](../flow_controller/domain/combustion.py#L109),
[`combustion.py:119`](../flow_controller/domain/combustion.py#L119), and
[`combustion.py:120`](../flow_controller/domain/combustion.py#L120). Applying
$\phi_1$ to Stage 1 and $\phi_g$ to the total mixture gives

$$
\dot V_{air,1}=\frac{\dot V_{air,st,1}}{\phi_1},\qquad
\dot V_{air,total}=\frac{\dot V_{air,st,total}}{\phi_g},\qquad
\dot V_{air,2}=\dot V_{air,total}-\dot V_{air,1}.
$$

The two stoichiometric-air calculations are at
[`rql.py:124`](../flow_controller/domain/rql.py#L124) and
[`rql.py:125`](../flow_controller/domain/rql.py#L125); the three air-target
equations are at [`rql.py:126`](../flow_controller/domain/rql.py#L126),
[`rql.py:127`](../flow_controller/domain/rql.py#L127), and
[`rql.py:128`](../flow_controller/domain/rql.py#L128).

The target calculator and measured-power reconstruction use the same
25 °C, 14.696 psia densities. This makes requested power and power recovered
from the resulting fuel flows inverse calculations. If $\dot V_i$ is the
reported flow of fuel $i$ in SLPM, measured firing rate is

$$
P=\sum_i\frac{\dot V_iLHV_i^{(V)}}{60}.
$$

The matching per-SLPM factors are 0.218091 kW/SLPM for NH3, 0.164700 for H2,
and 0.547400 for CH4. Their calculation is implemented at
[`combustion.py:65`](../flow_controller/domain/combustion.py#L65) to
[`combustion.py:66`](../flow_controller/domain/combustion.py#L66), and measured
flows are summed at
[`combustion.py:123`](../flow_controller/domain/combustion.py#L123) to
[`combustion.py:132`](../flow_controller/domain/combustion.py#L132).

Do not combine densities or volumetric LHVs quoted at 0 °C (`Nm³`) with
25 °C SLPM. Doing so mixes standard-volume bases and biases mass flow, firing
rate, and generated flow targets. The calibration sheet for each controller is
authoritative: if its configured STP differs from Alicat's default, the software
gas-property basis must also be changed or made configurable before relying on
these calculations.

### Bulk velocity as the throughput variable

When a campaign searches bulk velocity, $U_1$ is the cold-flow velocity of
everything entering the Stage 1 inlet (Stage 1 NH3, Stage 1 H2 and Stage 1 air,
with the pilot off) at Alicat standard conditions. It is not a hot-gas velocity
and takes no account of preheat or chamber pressure. With $A_1$ the campaign's
Stage 1 inlet area in mm$^2$ and flows in SLPM,

$$
U_1=\frac{\dot V_{NH3,1}+\dot V_{H2,1}+\dot V_{air,1}}{60000\,A_1\times10^{-6}},
$$

implemented by `stage1_bulk_velocity()` at
[`rql.py:63`](../flow_controller/domain/rql.py#L63) and
[`rql.py:64`](../flow_controller/domain/rql.py#L64).

A requested velocity is converted to the thermal input that produces it, and
that power then goes through the unchanged flow-target equations above. Let
$a_1$ be the Stage 1 air required per unit Stage 1 fuel volume,

$$
a_1=\frac{0.75\,x_{NH3}+0.5\,x_{H2}}{x_{O2,air}\,\phi_1}.
$$

Implemented at [`rql.py:83`](../flow_controller/domain/rql.py#L83).
The Stage 1 inlet flow is the Stage 1 fuel plus that air, so

$$
\dot V_{f,1}=\frac{60000\,U_1A_1\times10^{-6}}{1+a_1},\qquad
\dot V_f=\frac{\dot V_{f,1}}{s},\qquad
P=\dot V_f\left(x_{NH3}k_{NH3}+x_{H2}k_{H2}\right),
$$

where $k_i$ is the thermal power per SLPM of fuel $i$ in kW. These four steps
are `power_for_stage1_velocity()` at
[`rql.py:67`](../flow_controller/domain/rql.py#L67) to
[`rql.py:87`](../flow_controller/domain/rql.py#L87). `SearchConfig.values()`
calls it at [`bayesian.py:188`](../flow_controller/domain/bayesian.py#L188), so
every consumer of an operating point (target fields, flow ceilings, records and
maps) sees the same derived power. Thermal input therefore rises in proportion
to $U_1$, and rises with $\phi_1$ because a richer Stage 1 carries less air per
unit of fuel through the same inlet. Overall phi does not change it: it only
sets the Stage 2 air.

The model is trained on the measured velocity, recomputed from the averaged
Stage 1 flows by `SearchConfig.observed_vector()` at
[`bayesian.py:206`](../flow_controller/domain/bayesian.py#L206). The fixed
thermal-input check on a saved window is skipped for these campaigns at
[`optimisation.py:1041`](../flow_controller/core/optimisation.py#L1041); the
measured velocity is checked against its own limits with the other variables at
[`optimisation.py:1067`](../flow_controller/core/optimisation.py#L1067).

## 4. Measurement and objective

Each trial represents a steady operating condition, not a continuous sweep.
During a measurement window, every fresh flow reading must remain within the
larger of 3% of its target or 0.05 SLPM. If $q_i$ is a measured flow and
$q_{i,target}$ is its target, both in SLPM, the accepted condition is

$$
|q_i-q_{i,target}|\leq
\max(0.05\ \mathrm{SLPM},\ 0.03q_{i,target}).
$$

The comparison is implemented at
[`optimisation.py:1259`](../flow_controller/core/optimisation.py#L1259) to
[`optimisation.py:1260`](../flow_controller/core/optimisation.py#L1260). The
window requires at least three fresh flow passes and the configured duration;
those checks are at
[`optimisation.py:1269`](../flow_controller/core/optimisation.py#L1269) to
[`optimisation.py:1270`](../flow_controller/core/optimisation.py#L1270) and
[`optimisation.py:1044`](../flow_controller/core/optimisation.py#L1044) to
[`optimisation.py:1046`](../flow_controller/core/optimisation.py#L1046).

The arithmetic mean of each flow is converted back into observed H2 fraction,
stage-1 phi, overall phi and thermal power at
[`optimisation.py:980`](../flow_controller/core/optimisation.py#L980) to
[`optimisation.py:990`](../flow_controller/core/optimisation.py#L990). Here the
subscript `obs` means reconstructed from the averaged measured flows. In
particular,

$$
x_{H2,obs}=\frac{\dot V_{H2,1}+\dot V_{H2,2}}
{\dot V_{NH3,1}+\dot V_{NH3,2}+\dot V_{H2,1}+\dot V_{H2,2}}.
$$

Implemented at [`optimisation.py:983`](../flow_controller/core/optimisation.py#L983),
[`optimisation.py:984`](../flow_controller/core/optimisation.py#L984), and
[`optimisation.py:987`](../flow_controller/core/optimisation.py#L987). Observed
Stage 1 fuel split is

$$
s_{obs}=\frac{\dot V_{NH3,1}+\dot V_{H2,1}}
{\dot V_{NH3,1}+\dot V_{H2,1}+\dot V_{NH3,2}+\dot V_{H2,2}},
$$

implemented at [`optimisation.py:993`](../flow_controller/core/optimisation.py#L993)
to [`optimisation.py:1000`](../flow_controller/core/optimisation.py#L1000). The model
uses these observed values rather than assuming that requested values were
reached exactly. `SearchConfig.observed_vector()` assembles the model input at
[`bayesian.py:201`](../flow_controller/domain/bayesian.py#L201).

In **Minimise NO** mode, the objective is raw dry NO corrected to a reference
oxygen concentration. Mapping mode uses the same corrected NO as one of its
two measured responses. Let $NO_{raw}$ be
the uncorrected dry nitric-oxide concentration in ppm, $NO_{corr}$ the corrected
concentration in ppm, $O_{2,meas}$ the measured dry oxygen concentration in
volume %, and $O_{2,ref}$ the reporting reference in volume %. Then

$$
NO_{corr}=NO_{raw}
\frac{20.9-O_{2,ref}}{20.9-O_{2,meas}}.
$$

Here 20.9% is the [EPA emissions-reporting
convention](https://www.epa.gov/sites/default/files/2017-09/documents/10-6200.pdf),
not the NIST physical dry-air value of 20.9390% used by the stoichiometric
calculation. The separate reporting constant is defined at
[`gas_properties.py:22`](../flow_controller/domain/gas_properties.py#L22), then
imported by the optimiser at
[`bayesian.py:23`](../flow_controller/domain/bayesian.py#L23) and
[`bayesian.py:28`](../flow_controller/domain/bayesian.py#L28). The correction
factor is calculated at
[`bayesian.py:243`](../flow_controller/domain/bayesian.py#L243), and applied at
[`bayesian.py:247`](../flow_controller/domain/bayesian.py#L247). If a manual NO
**standard error of the mean (SEM)** is supplied, it estimates the uncertainty
of the sample mean in ppm; the same correction factor is applied to it at
[`bayesian.py:247`](../flow_controller/domain/bayesian.py#L247). O2
uncertainty and systematic analyser/calibration bias are not propagated. The
objective is corrected NO concentration, not total NOx, NO2, NH3 slip, N2O,
combustion efficiency or emissions per unit energy.

Oxygen-reference normalization and the need to state dry/wet basis and the
reporting species are discussed for industrial combustion measurements by
Baukal and Eleazer [[R1](#ref-r1)]. Their paper concerns NOx reporting and also
warns that conventional oxygen correction can be misleading for
oxygen-enhanced combustion. This program applies the conventional dilution
correction to its measured dry **NO** channel; the citation does not turn that
channel into total NOx or a mass-per-energy emission factor.

Window validation recomputes the observed condition from saved mean flows and
checks all selected-variable bounds at
[`optimisation.py:1060`](../flow_controller/core/optimisation.py#L1060) to
[`optimisation.py:1069`](../flow_controller/core/optimisation.py#L1069). This makes
the saved flow record the source of the model coordinates.

### LabVIEW/TDMS capture workflow

Use the LabVIEW VI's existing recording controls and UDP `log`/`stop`
messages. The normal workflow requires no LabVIEW code changes, JSON messages,
exported request IDs or additional manifest files.

1. Install TDMS support in the flow application's Python environment if needed:

   ```powershell
   & "$env:USERPROFILE\.flow-controller-v3\venv\Scripts\python.exe" -m pip install -r requirements-pressure.txt
   ```

2. Create a **Map NO + pressure** campaign. In **Current test**, open
   **TDMS source…**, choose the recording folder, inspect a representative
   `.tdms` file, and select two distinct time-domain pressure channels. Assign
   clear labels for the two physical locations. The stable record IDs remain
   `pressure_1` and `pressure_2`. The folder must be readable by the flow PC;
   the search does not include subfolders.
3. Declare the units and calibration of the values returned by npTDMS for each
   transducer. Set **Known pressure units** to Pa or kPa, or enter a custom
   scale, then check its offset, calibration identifier and optional clipping
   bounds. Conversion is
   `pressure_pa = stored_value × scale_pa_per_unit + offset_pa`.
   npTDMS has already applied any NI scaling represented in the file. A group
   named `converted` does not establish physical units, and `FFT` channels
   are spectra rather than eligible time-domain waveforms.
4. Set **Minimum pressure recording (s)**, independently of the NO averaging
   duration. Its default is 1 s. Review the spectral band, segment length,
   overlap. These settings are shared; calibration and clipping remain per
   transducer. The default segment is 4096 samples
   with 2048-sample overlap; it must fit the pressure record. Use waveform
   timing where available. Enable the trigger-time fallback only if LabVIEW
   writes one new recording per trigger and the file lacks a start timestamp.
5. Suggest a test, load its target fields, review and apply the flows through
   the normal controls, and settle the burner. Start the LabVIEW UDP listener
   at `127.0.0.1:61557`. Confirm pilot off and settled burner/flows. For live
   NO/O2, enable automatic MEXA capture and receiver logging, and select the
   applicable response calibration.
6. Click **Arm LabVIEW trigger**, then start the LabVIEW recording that sends
   `log`. Arming snapshots the folder so automatic selection can distinguish
   a new or changed TDMS file. Stop recording in LabVIEW with its existing
   `stop` command after meeting the configured pressure duration.
7. Keep the operating condition steady after `stop`. The flow application
   continues collecting until the response delay, minimum NO duration and
   fresh flow/MEXA coverage are satisfied. It then saves the measurement
   window and processes the matching TDMS file in a background worker.
8. Review both pressure results and the saved NO/O2 averages, confirm their
   uncorrected dry basis, and click **Save result**. Manual NO/O2 capture
   requires entering the averages for the recorded window. A mapping test
   cannot be completed without valid data from both transducers. Attaching pressure does
   not automatically save the NO result or change flows.

Without local arming, `log` and `stop` retain their ordinary flow-CSV logging
behaviour. In **Minimise NO** mode, **Start window** and **Finish window** remain
available for the existing manual or live NO/O2 procedure. The mapping UI uses
the armed trigger route to record the physical pressure interval.

The trigger and completion paths are `arm_labview()`,
`_legacy_labview_command()`, `_legacy_ready()` and `finish_window()` in
[`optimiser_controller.py`](../flow_controller/core/optimiser_controller.py).
Here `legacy` names the existing plain-text LabVIEW command format; this is the
current TDMS workflow.

### Pressure interval, NO delay and file association

Let $t_0$ be receipt of `log`, $t_1$ receipt of `stop`, $d$ the selected live
response delay, and $W$ the campaign's minimum NO averaging duration, all in
seconds on their corresponding clocks. The planned collection deadline is

$$
t_{end,min}=\max(t_1+d,\ t_0+d+W).
$$

Implemented at [`optimiser_controller.py:435`](../flow_controller/core/optimiser_controller.py#L435).
Completion may occur later because the flow window and, for live capture, at
least three fresh MEXA records must provide the required time coverage. The
NO average uses eligible analyser records acquired from $t_0+d$ through the
final flow timestamp. Flow statistics cover the full capture from the initial
poll through completion. Pressure selection covers the physical $t_0$–$t_1$
recording interval, with a 2 s association tolerance, rather than the later
NO collection. Manual NO/O2 mode uses zero automatic response delay.

For example, a 2 s pressure recording, 10 s selected response delay and 30 s
NO averaging minimum require at least about 40 s from `log`, or 38 s after
`stop`, before sufficient sample coverage can finish the capture. A short
pressure recording can therefore accompany a longer NO window only while
the burner remains at the same condition. UDP receipt times and the
association tolerance do not provide hardware synchronisation. The one-hour
capture limit still applies; an excessive delay plus averaging duration can
reach that limit before completion.

Automatic file discovery waits for a new or changed file to remain unchanged
for 2 s, with a default 60 s search timeout after NO collection finishes.
It validates both channels, their common timing, calibrations and file
completeness. A single
recording that matches the trigger interval can be used whole; a continuous
waveform is cropped to the trigger interval. File modification time is never
used as the waveform's start timestamp. Missing, ambiguous, incomplete or
changed recordings are reported rather than silently replaced by the newest
file. After the worker finishes, **Choose TDMS file…** retries with the
operator-selected recording and the same waveform checks.

The saved pressure summary retains both channel assignments and labels, the
source path and SHA-256, selected sample offset, original sample count, timing
source and trigger association.
The source TDMS file is not modified. `find_tdms_capture()` and
`process_tdms_capture()` implement these rules in
[`tdms_capture.py`](../flow_controller/domain/tdms_capture.py).

### Pressure response definitions

For calibrated samples $p_i$ in Pa from either transducer, the processor removes
the complete selected record's mean, giving $p'_i=p_i-\bar p$. It saves:

| UI response | Saved field | Calculation |
| --- | --- | --- |
| RMS | `rms_pa` | $\sqrt{N^{-1}\sum_i (p'_i)^2}$, in Pa. |
| Peak excursion | `peak_abs_pa` | $\max_i \lvert p'_i\rvert$, in Pa; not peak-to-peak. |
| Dominant spectral amplitude | `dominant_amplitude_pa` | Square root of the strongest in-band Welch spectrum bin, in Pa RMS. |

Welch processing uses periodic flattop windows, constant segment detrending
and `scaling="spectrum"`. The frequency of the strongest bin is stored as
`dominant_frequency_hz`. The selected frequency band restricts that bin
search; it does not band-pass the RMS or peak calculation. Optional
`rms_window_sd_pa` is the population SD of RMS values from complete,
nonoverlapping segments of the mean-removed signal. It is zero with fewer
than two segments and is not a standard error for the pressure mean.

Which response is modelled depends on the campaign's **Pressure used to choose
tests** setting. With **Larger of the two transducers** (the default for new
mapping campaigns), the selected response, RMS by default, is read from each
transducer, and a third response is formed as the larger of the two readings at
each test. With **Both transducers equally**, each transducer's dominant
spectral amplitude is used, as in campaigns created before this option
existed. The selection is made by `dual_pressure_metric()` at
[`bayesian.py:282`](../flow_controller/domain/bayesian.py#L282).

Nonfinite samples and samples touching declared clipping limits are rejected.
Omitting clipping bounds leaves clipping unchecked. The first attached
pressure result fixes a comparison signature containing the shared sample rate
and, for each stable transducer ID, its label, channel, calibration identifier,
units and analysis settings. Record duration is
excluded from that signature. These calculations and checks are implemented
by `pressure_metrics()` and `pressure_signature()` in
[`pressure.py`](../flow_controller/domain/pressure.py).

## 5. NO response-time calibration

### What the test measures

The **NO response time** tab measures the response of the complete experimental
measurement path to a commanded combustion-condition change. That path includes
the flow-controller transition, burner response, transport through the sample
line, sample conditioning, MEXA processing and network acquisition. It is not an
intrinsic analyser specification. To measure analyser-only response, apply a
known calibration-gas step directly at the analyser inlet without changing the
burner. This distinction is stated by the detector itself at
[`analyser_response.py:1`](../flow_controller/domain/analyser_response.py#L1) to
[`analyser_response.py:5`](../flow_controller/domain/analyser_response.py#L5)
and in the response-test interface at
[`qt_optimiser.py:1196`](../flow_controller/ui/qt_optimiser.py#L1196) to
[`qt_optimiser.py:1200`](../flow_controller/ui/qt_optimiser.py#L1200).

Gas-analyser studies distinguish transport delay from analyser rise time and
show that sample tubing, connectors, internal gas exchange and acquisition
cycles can change the observed response [[R2](#ref-r2), [R3](#ref-r3)]. The
program therefore labels its result as the response of the complete
experimental path. That classification is an application of the published
measurement concepts, not a claim that either paper studied this MEXA/burner
arrangement.

The streamed channel used by this test is **NO**, meaning nitric oxide in ppm.
It is not total NOx. The controller passes `packet["no_ppm"]` to the detector at
[`analyser_response_controller.py:277`](../flow_controller/core/analyser_response_controller.py#L277)
to [`analyser_response_controller.py:280`](../flow_controller/core/analyser_response_controller.py#L280),
and the saved record identifies the source as the validated live MEXA NO stream
at [`analyser_response_controller.py:346`](../flow_controller/core/analyser_response_controller.py#L346)
to [`analyser_response_controller.py:348`](../flow_controller/core/analyser_response_controller.py#L348).

### Operating workflow

Create or open a campaign, connect and monitor the controllers, and enable the
received MEXA audit log. Then use the **NO response time** tab:

1. Establish a settled live condition A and select **Store live as A**.
2. Establish a different settled live condition B and select **Store live as B**.
3. Return the rig to condition A and allow every flow to settle.
4. Select **Start A → B response test**. Read the displayed transition, including
   each direct-step or ramp policy, and confirm it explicitly.
5. Leave the condition undisturbed while the program records the baseline,
   commands B, confirms the B flows and detects the final NO plateau.

The buttons and result panel are created at
[`qt_optimiser.py:1192`](../flow_controller/ui/qt_optimiser.py#L1192) to
[`qt_optimiser.py:1242`](../flow_controller/ui/qt_optimiser.py#L1242), and the
confirmation dialog is at
[`qt_optimiser.py:1244`](../flow_controller/ui/qt_optimiser.py#L1244) to
[`qt_optimiser.py:1259`](../flow_controller/ui/qt_optimiser.py#L1259).

A stored condition is a snapshot of settled target flows, measured flows,
controller assignments, capture time and, when available, the corresponding
flow-calculation request. Snapshot capture and tracking checks are implemented
at [`analyser_response_controller.py:106`](../flow_controller/core/analyser_response_controller.py#L106)
to [`analyser_response_controller.py:145`](../flow_controller/core/analyser_response_controller.py#L145).
Storing either endpoint clears selection of an earlier calibration because that
calibration belongs to the old A-to-B transition; persistence is implemented at
[`optimisation.py:288`](../flow_controller/core/optimisation.py#L288) to
[`optimisation.py:296`](../flow_controller/core/optimisation.py#L296).

Start is refused unless both conditions use the same assignments, at least one
target differs, the current rig has returned to A, live flow data are fresh and
the operator has confirmed the displayed transition. These checks are at
[`analyser_response_controller.py:147`](../flow_controller/core/analyser_response_controller.py#L147)
to [`analyser_response_controller.py:181`](../flow_controller/core/analyser_response_controller.py#L181)
and [`analyser_response_controller.py:218`](../flow_controller/core/analyser_response_controller.py#L218)
to [`analyser_response_controller.py:241`](../flow_controller/core/analyser_response_controller.py#L241).

Condition B is not sent until the detector has accepted a stable baseline
spanning at least 15 s. The command then uses the existing
`FlowSession.set_role_setpoint()` path at
[`analyser_response_controller.py:243`](../flow_controller/core/analyser_response_controller.py#L243)
to [`analyser_response_controller.py:265`](../flow_controller/core/analyser_response_controller.py#L265).
That path retains the controller assignment, command ceiling, zero lock and
configured ramp behaviour at
[`session.py:1513`](../flow_controller/core/session.py#L1513) to
[`session.py:1550`](../flow_controller/core/session.py#L1550). The final queue
also rechecks the ceiling and blocks nonzero commands while an operator or
watchdog zero lock is active at
[`session.py:1391`](../flow_controller/core/session.py#L1391) to
[`session.py:1401`](../flow_controller/core/session.py#L1401).

This is only a sharp step when the affected line is permitted to move directly.
If a ramp rate is configured, the software applies that ramp; if ramping is
declared off for a ramp-controlled role, the confirmation identifies a direct
step. The displayed policy is assembled at
[`analyser_response_controller.py:160`](../flow_controller/core/analyser_response_controller.py#L160)
to [`analyser_response_controller.py:181`](../flow_controller/core/analyser_response_controller.py#L181),
and configured ramps are executed at
[`session.py:2166`](../flow_controller/core/session.py#L2166) to
[`session.py:2200`](../flow_controller/core/session.py#L2200). A ramped test
therefore includes the commanded ramp duration and is not equivalent to an
instantaneous-input response test.

During the commanded transition, the program also enforces the frozen A-to-B
**transition envelope**. For role $i$, let $q_{i,A}$ and $q_{i,B}$ be the stored
A and B targets in SLPM and define

$$
\epsilon_i=\max\left(0.05\ \mathrm{SLPM},
0.03\max(|q_{i,A}|,|q_{i,B}|)\right).
$$

Implemented at [`analyser_response_controller.py:212`](../flow_controller/core/analyser_response_controller.py#L212).
Here $\epsilon_i$ is the allowed telemetry tolerance for the complete move.
Both measured flow $q_i$ and reported setpoint $SP_i$ must satisfy

$$
\min(q_{i,A},q_{i,B})-\epsilon_i
\leq q_i,SP_i\leq
\max(q_{i,A},q_{i,B})+\epsilon_i.
$$

The frozen-envelope calculation and comparison are implemented at
[`analyser_response_controller.py:200`](../flow_controller/core/analyser_response_controller.py#L200)
to [`analyser_response_controller.py:216`](../flow_controller/core/analyser_response_controller.py#L216).
Each fresh flow update applies that check at
[`analyser_response_controller.py:308`](../flow_controller/core/analyser_response_controller.py#L308)
to [`analyser_response_controller.py:309`](../flow_controller/core/analyser_response_controller.py#L309).
If any affected or unchanged role leaves the confirmed range, the response run
is cancelled rather than treating the excursion as part of the approved test.

### Baseline calculations and departure detection

For a window containing $n$ accepted NO samples, let $t_i$ be acquisition time
in seconds and $y_i$ be NO in ppm for sample $i$. Let $\bar t$ and $\bar y$ be
their arithmetic means. The detector calculates the baseline mean, sample
standard deviation and least-squares slope:

$$
\bar y=\frac{1}{n}\sum_{i=1}^{n}y_i,
$$

$$
s=\sqrt{\frac{1}{n-1}\sum_{i=1}^{n}(y_i-\bar y)^2},
$$

$$
m=\frac{\sum_{i=1}^{n}(t_i-\bar t)(y_i-\bar y)}
{\sum_{i=1}^{n}(t_i-\bar t)^2}.
$$

Here $n$ is the sample count, $s$ is sample standard deviation in ppm and $m$
is the fitted NO drift in ppm s$^{-1}$. The mean, sample-SD and slope statements
are implemented at
[`analyser_response.py:160`](../flow_controller/domain/analyser_response.py#L160)
to [`analyser_response.py:170`](../flow_controller/domain/analyser_response.py#L170).

The rolling baseline may retain up to 30 s of readings, but it cannot pass until
it contains at least six samples spanning 15 s. It is stable when

$$
|m|T_A\leq
\max\left(1\ \mathrm{ppm},\ 0.02\max(|\bar y_A|,10\ \mathrm{ppm})\right),
$$

where $T_A$ is baseline span in seconds and $\bar y_A$ is baseline mean NO in
ppm. The fixed criteria are declared at
[`analyser_response.py:19`](../flow_controller/domain/analyser_response.py#L19)
to [`analyser_response.py:28`](../flow_controller/domain/analyser_response.py#L28),
and the rolling-window and drift checks are at
[`analyser_response.py:319`](../flow_controller/domain/analyser_response.py#L319)
to [`analyser_response.py:334`](../flow_controller/domain/analyser_response.py#L334).

After commanding B, a sample departs from baseline when its difference from
$\bar y_A$ reaches the signed threshold

$$
D_A=\max\left(2\ \mathrm{ppm},\ 4s_A,\;
0.02\max(|\bar y_A|,10\ \mathrm{ppm})\right),
$$

where $D_A$ is the departure threshold in ppm and $s_A$ is the baseline sample
SD in ppm. The threshold is implemented at
[`analyser_response.py:225`](../flow_controller/domain/analyser_response.py#L225)
to [`analyser_response.py:234`](../flow_controller/domain/analyser_response.py#L234).
The detector requires three consecutive samples beyond $+D_A$ or three beyond
$-D_A$. It records the first sample of that sustained run as the detected-change
time. A reversal or a return inside the threshold resets the streak. This logic
is at [`analyser_response.py:336`](../flow_controller/domain/analyser_response.py#L336)
to [`analyser_response.py:357`](../flow_controller/domain/analyser_response.py#L357).

### Final plateau and crossing times

Let $\bar y_B$, $s_B$, $m_B$ and $T_B$ be the mean, sample SD, slope and time
span of the final trailing window. The window must contain at least six samples
and span the full 20 s. Its **signed amplitude** is

$$
\Delta=\bar y_B-\bar y_A,
$$

where positive $\Delta$ is a rising response and negative $\Delta$ is a falling
response. The amplitude must exceed $D_A$ and have the same sign as the sustained
departure. The final drift and SD bands are

$$
B_m=\max(1.5\ \mathrm{ppm},0.05|\Delta|),\qquad
B_s=\max(1\ \mathrm{ppm},0.05|\Delta|).
$$

Here $B_m$ is the maximum allowed change attributable to final-window slope and
$B_s$ is the maximum allowed final-window sample SD. The plateau passes only if

$$
|m_B|T_B\leq B_m\qquad\text{and}\qquad s_B\leq B_s.
$$

The trailing-window, amplitude and acceptance statements are implemented at
[`analyser_response.py:361`](../flow_controller/domain/analyser_response.py#L361)
to [`analyser_response.py:385`](../flow_controller/domain/analyser_response.py#L385).
The constants are at
[`analyser_response.py:29`](../flow_controller/domain/analyser_response.py#L29)
to [`analyser_response.py:34`](../flow_controller/domain/analyser_response.py#L34).
The six-sample minimum, 20 s window, 5% bands and absolute ppm floors are local
acceptance criteria. They were not taken from the cited response-time papers
and should be reassessed if the analyser rate, noise or experiment changes.

For $p\in\{0.1,0.5,0.9\}$, the crossing level is

$$
y_p=\bar y_A+p\Delta.
$$

Implemented at [`analyser_response.py:432`](../flow_controller/domain/analyser_response.py#L432).
The reported $t_{10}$, $t_{50}$ and $t_{90}$ are command-to-crossing times for
the first three-sample sustained crossings of $y_{0.1}$, $y_{0.5}$ and
$y_{0.9}$. The signed comparison works for rising and falling responses. The
10-to-90% rise time is

$$
t_{10\text{-}90}=t_{90}-t_{10}.
$$

Implemented at [`analyser_response.py:400`](../flow_controller/domain/analyser_response.py#L400).
Step-response work commonly reports a 90% response point and, in some fields,
the 10-to-90% rise interval. Horsley et al. explicitly use a 10-to-90% gas-
analyser rise time and show that sampling-system changes affect it
[[R3](#ref-r3)]. In this program, `t10_s`, `t50_s` and `t90_s` are each measured
from the command, while `rise_10_90_s` is the separate difference
$t_{90}-t_{10}$; those definitions must not be interchanged.

ISO 8178-1 defines gas-system delay, transformation and response points at 10%,
50% and 90%, with rise time $t_{90}-t_{10}$ [[R12](#ref-r12)]. Its reference
input is a rapid gas change at the sampling probe. This program instead uses a
combustion-condition command that may include a controller ramp, so its reported
times use related percentile vocabulary but are **not** an ISO 8178-1 analyser
response test.

Crossing detection is implemented at
[`analyser_response.py:432`](../flow_controller/domain/analyser_response.py#L432)
to [`analyser_response.py:440`](../flow_controller/domain/analyser_response.py#L440),
and elapsed and rise-time calculation is at
[`analyser_response.py:398`](../flow_controller/domain/analyser_response.py#L398)
to [`analyser_response.py:401`](../flow_controller/domain/analyser_response.py#L401).
Crossing times are quantised by the MEXA sampling interval.

The **stability onset**, `stable_window_start`, is the first sample of the
accepted final 20 s window. It is not the time at which the software can first
confirm stability. Confirmation becomes available only at `stable_window_end`,
after the complete window has elapsed and passed its tests. Both timestamps and
the command-to-stability value are stored at
[`analyser_response.py:415`](../flow_controller/domain/analyser_response.py#L415)
to [`analyser_response.py:418`](../flow_controller/domain/analyser_response.py#L418).

Final stability is not considered until condition B's measured flow and
setpoint for every stored role remain within

$$
\max(0.05\ \mathrm{SLPM},0.03|q_{i,B}|)
$$

for three seconds, where $q_{i,B}$ is role $i$'s stored B target. The tolerance
comparison is at
[`analyser_response_controller.py:183`](../flow_controller/core/analyser_response_controller.py#L183)
to [`analyser_response_controller.py:198`](../flow_controller/core/analyser_response_controller.py#L198),
and the three-second hold gate is at
[`analyser_response_controller.py:314`](../flow_controller/core/analyser_response_controller.py#L314)
to [`analyser_response_controller.py:324`](../flow_controller/core/analyser_response_controller.py#L324).
The detector receives permission to accept a plateau only after this gate at
[`analyser_response_controller.py:277`](../flow_controller/core/analyser_response_controller.py#L277)
to [`analyser_response_controller.py:280`](../flow_controller/core/analyser_response_controller.py#L280).

### Timing meanings and recommended delay

The saved timings have different reference points:

- **Detection delay**, `command_to_change_s`, runs from the B command time to
  the first sample of the sustained departure.
- **Command-to-stable time**, `command_to_stable_s`, runs from the B command to
  `stable_window_start`.
- **Flow-to-stable time**, `flow_to_stable_s`, runs from the start of the
  accepted three-second settled-flow hold to `stable_window_start`, with
  negative values clipped to zero.
- **Confirmation time** occurs at `stable_window_end`. It is later than stability
  onset by the accepted final-window span and is not used as the recommended
  pre-averaging delay.

These result fields are assigned at
[`analyser_response.py:415`](../flow_controller/domain/analyser_response.py#L415)
to [`analyser_response.py:418`](../flow_controller/domain/analyser_response.py#L418),
while the flow-relative timing is added at
[`analyser_response.py:118`](../flow_controller/domain/analyser_response.py#L118)
to [`analyser_response.py:145`](../flow_controller/domain/analyser_response.py#L145).

Let $t_s$ be `stable_window_start`, $t_f$ be `flow_reached_at`, and $\delta t$
be the median interval between accepted MEXA samples, all in seconds. Let
$\lceil z\rceil_5=5\lceil z/5\rceil$ mean round $z$ upward to the next multiple
of 5 s. The selected delay is

$$
t_{delay}=\min\left(3600,\max\left(5,
\left\lceil1.25\max\bigl(\max(0,t_s-t_f),\delta t\bigr)\right\rceil_5
\right)\right).
$$

Here $t_{delay}$ is the recommended pre-averaging delay; 1.25 is a margin
factor; $\max(0,t_s-t_f)$ is flow-to-stable time; and $\delta t$ prevents a
recommendation shorter than the measurement's time resolution before the
margin is applied. The raw value is rounded upward in 5 s increments and bounded
to 5 through 3600 s. The exact calculation is implemented at
[`analyser_response.py:130`](../flow_controller/domain/analyser_response.py#L130)
to [`analyser_response.py:145`](../flow_controller/domain/analyser_response.py#L145),
using the criteria at
[`analyser_response.py:38`](../flow_controller/domain/analyser_response.py#L38)
to [`analyser_response.py:41`](../flow_controller/domain/analyser_response.py#L41).
The 1.25 margin, upward 5 s rounding and 5-to-3600 s bounds are conservative
program settings, not constants supplied by a response-time paper or analyser
standard.

### Use in future optimiser windows

A successful run is selected for that campaign. For a **live** MEXA window
started with **Start window**, the minimum planned observation time is

$$
t_{total}=t_{pre-delay}+t_{averaging},
$$

where $t_{pre-delay}$ is the selected response calibration's recommended delay
and $t_{averaging}$ is the campaign's configured minimum averaging duration.
Fresh-sample coverage and the operator's **Finish window** action determine
the actual end. These properties are calculated
at [`optimisation.py:348`](../flow_controller/core/optimisation.py#L348) to
[`optimisation.py:354`](../flow_controller/core/optimisation.py#L354).

On this Start/Finish path, the controller waits through $t_{pre-delay}$ before
constructing `LiveWindow` and `MeasurementWindow`. It rechecks the MEXA link and
flow tracking throughout the wait, then starts the unchanged averaging window.
This sequence is implemented at
[`optimiser_controller.py:679`](../flow_controller/core/optimiser_controller.py#L679)
to [`optimiser_controller.py:722`](../flow_controller/core/optimiser_controller.py#L722)
and [`optimiser_controller.py:849`](../flow_controller/core/optimiser_controller.py#L849)
to [`optimiser_controller.py:869`](../flow_controller/core/optimiser_controller.py#L869).

Transient samples received during the delay remain in the receiver's JSONL and
CSV audit logs, whose write path is at
[`records.py:128`](../mexa_bridge/records.py#L128) to
[`records.py:156`](../mexa_bridge/records.py#L156). They are excluded
from the optimiser NO/O2 mean because `LiveWindow` is created only after the
delay. Its accepted samples and arithmetic channel means are implemented at
[`records.py:237`](../mexa_bridge/records.py#L237) to
[`records.py:319`](../mexa_bridge/records.py#L319). Manual-input windows
do not use the automatic delay because `start_window()` assigns zero delay unless
live capture is selected at
[`optimiser_controller.py:679`](../flow_controller/core/optimiser_controller.py#L679)
to [`optimiser_controller.py:705`](../flow_controller/core/optimiser_controller.py#L705).

The armed LabVIEW path starts flow capture at `log` and retains it through the
post-`stop` collection. It filters the saved NO/O2 average to exclude the
initial delay, then finishes automatically once its deadline and sample
coverage are satisfied. Its timing is recorded under `labview_capture`,
including `delay_s`, `no_start` and `no_end`. See
[pressure interval and NO delay](#pressure-interval-no-delay-and-file-association)
for the deadline equation; do not apply the Start/Finish timing fields to that
physical pressure interval.

The campaign JSON retains conditions A and B, all retained response runs, the
selected-run ID, criteria, timings, baseline/final statistics, source ID,
receiver-log provenance and accepted raw NO samples. Record construction and
persistence are at
[`analyser_response_controller.py:328`](../flow_controller/core/analyser_response_controller.py#L328)
to [`analyser_response_controller.py:366`](../flow_controller/core/analyser_response_controller.py#L366)
and [`optimisation.py:303`](../flow_controller/core/optimisation.py#L303) to
[`optimisation.py:338`](../flow_controller/core/optimisation.py#L338). Each live
trial window also stores `pre_window_delay_s`, `response_run_id` and
`total_observation_s` at
[`optimiser_controller.py:769`](../flow_controller/core/optimiser_controller.py#L769)
to [`optimiser_controller.py:772`](../flow_controller/core/optimiser_controller.py#L772).
CSV export includes those fields plus the MEXA source, sequence range, sample
count, channel SDs and receiver audit-log path at
[`optimisation.py:503`](../flow_controller/core/optimisation.py#L503) to
[`optimisation.py:675`](../flow_controller/core/optimisation.py#L675).

### Rejection, timeout and cancellation

Every accepted response sample must retain the same non-empty source ID, advance
its sequence by exactly one, have a strictly increasing timestamp with no gap
over 5 s, and contain finite NO from 0 to 5000 ppm. A run is limited to 4000
samples. These detector checks are at
[`analyser_response.py:290`](../flow_controller/domain/analyser_response.py#L290)
to [`analyser_response.py:312`](../flow_controller/domain/analyser_response.py#L312),
with limits declared at
[`analyser_response.py:35`](../flow_controller/domain/analyser_response.py#L35)
to [`analyser_response.py:37`](../flow_controller/domain/analyser_response.py#L37).
The controller additionally requires a valid, non-simulated, validated, dry,
uncorrected live reading and the same receiver log. The MEXA validity and basis
checks are at [`records.py:184`](../mexa_bridge/records.py#L184) to
[`records.py:204`](../mexa_bridge/records.py#L204), and the response
controller applies them at
[`analyser_response_controller.py:267`](../flow_controller/core/analyser_response_controller.py#L267)
to [`analyser_response_controller.py:298`](../flow_controller/core/analyser_response_controller.py#L298).

The baseline phase is limited to 120 s. This is the maximum time allowed to
obtain the stable 15 s baseline; it is not a measured response time. The limit
is declared at
[`analyser_response_controller.py:20`](../flow_controller/core/analyser_response_controller.py#L20),
started with the baseline at
[`analyser_response_controller.py:218`](../flow_controller/core/analyser_response_controller.py#L218)
to [`analyser_response_controller.py:241`](../flow_controller/core/analyser_response_controller.py#L241),
and enforced at
[`analyser_response_controller.py:380`](../flow_controller/core/analyser_response_controller.py#L380)
to [`analyser_response_controller.py:393`](../flow_controller/core/analyser_response_controller.py#L393).

After B is commanded, the response phase has its own 900 s timeout. It is
declared at
[`analyser_response.py:36`](../flow_controller/domain/analyser_response.py#L36)
and enforced at
[`analyser_response.py:242`](../flow_controller/domain/analyser_response.py#L242)
to [`analyser_response.py:249`](../flow_controller/domain/analyser_response.py#L249).
Loss of valid MEXA data, fresh flow telemetry or flow tracking also discards the
run. Assignment, mode, connection, monitoring, communication, sequence, flow
ceiling, ramp-policy or zero-operation changes cancel it through the signal
guards at
[`analyser_response_controller.py:55`](../flow_controller/core/analyser_response_controller.py#L55)
to [`analyser_response_controller.py:60`](../flow_controller/core/analyser_response_controller.py#L60)
and [`analyser_response_controller.py:395`](../flow_controller/core/analyser_response_controller.py#L395)
to [`analyser_response_controller.py:397`](../flow_controller/core/analyser_response_controller.py#L397).

Cancellation stops and discards the measurement state. It deliberately sends no
automatic return-to-A command and does not zero the rig. This avoids introducing
an unreviewed recovery transition; it also means the operator remains responsible
for the physical condition after a partial or completed A-to-B move. The warning
is explicit in the start dialog at
[`qt_optimiser.py:1250`](../flow_controller/ui/qt_optimiser.py#L1250) to
[`qt_optimiser.py:1257`](../flow_controller/ui/qt_optimiser.py#L1257), and cancel
behaviour is implemented at
[`analyser_response_controller.py:403`](../flow_controller/core/analyser_response_controller.py#L403)
to [`analyser_response_controller.py:416`](../flow_controller/core/analyser_response_controller.py#L416).
The ordinary ramp, manual setpoint, zero and emergency controls remain available
for the established operator recovery procedure.

## 6. Initial space-filling design

With no previous recordings, `suggest()` creates a scrambled **Sobol sequence**:
a deterministic low-discrepancy sequence constructed for equidistribution over
a multi-dimensional box. Scrambling randomises that sequence while retaining
its digital-net coverage properties. The first
$2^{\lceil\log_2 n\rceil}$ points form the candidate pool, where $n$ is the
requested pool size and $\lceil\cdot\rceil$ means round upward to an integer.
Sobol' introduced the underlying low-discrepancy sequence [[R4](#ref-r4)], and
Owen analysed randomized scrambling of Sobol' and related digital nets
[[R5](#ref-r5)]. Those papers establish the sequence and scrambling ideas; using
the points as a finite feasible candidate pool, then applying the centre and
maximin rules below, is this program's experimental-design policy.
This rounding and generation are implemented at
[`bayesian.py:460`](../flow_controller/domain/bayesian.py#L460) and
[`bayesian.py:461`](../flow_controller/domain/bayesian.py#L461).

Let $u=(u_1,\ldots,u_d)$ be a normalized candidate, so each $u_j$ is a
dimensionless coordinate from 0 to 1. The **unit hypercube** is the set of all
such vectors. For variable $j$, let $l_j$ be its physical lower bound, $h_j$
its physical upper bound, and $x_j$ its physical value. Conversion from
normalized to physical coordinates is

$$
x_j=l_j+u_j(h_j-l_j),
$$

implemented by forming the lower-bound and span arrays at
[`bayesian.py:458`](../flow_controller/domain/bayesian.py#L458) and
[`bayesian.py:459`](../flow_controller/domain/bayesian.py#L459), then applying
the equation at [`bayesian.py:487`](../flow_controller/domain/bayesian.py#L487).

Candidates that violate a current controller flow ceiling are removed. Already
tried points, including invalid trials, are also removed so that the optimiser
does not immediately offer them again. The first proposal is closest to the
centre of the normalized region. Later initial points maximise their minimum
Euclidean distance from every prior trial:

$$
u^*=\arg\max_u\min_{v\in T}\lVert u-v\rVert_2.
$$

Here $u^*$ is the selected candidate, $T$ is the set of normalized previously
tried points, $v$ is one member of $T$, and $\lVert u-v\rVert_2$ is Euclidean
distance: the square root of the sum of squared coordinate differences.
Distances to tried points are calculated at
[`bayesian.py:531`](../flow_controller/domain/bayesian.py#L531), and the largest
minimum distance is selected at
[`bayesian.py:532`](../flow_controller/domain/bayesian.py#L532). For the very
first point, distance from the centre vector $(0.5,\ldots,0.5)$ is minimised at
[`bayesian.py:534`](../flow_controller/domain/bayesian.py#L534). Invalid tests
occupy their attempted location but are never assigned a fabricated emissions
result and never enter the Gaussian-process fit.

This space-filling rule is used while the number of completed tests is less
than the configured initial-design size. During that period the program does
not fit the Matérn GP or calculate NEI. The branch and returned method are
implemented at [`bayesian.py:529`](../flow_controller/domain/bayesian.py#L529)
to [`bayesian.py:536`](../flow_controller/domain/bayesian.py#L536). The initial
tests therefore establish coverage of the search space before the optimiser
has enough recordings to make model-based proposals.

The configured pool size is rounded upward to the next power of two for Sobol
generation. In NO minimisation mode, once completed data exist, 128 extra
candidates are sampled near the best observed point. Mapping uses the Sobol
pool without this concentration around the lowest-NO observation. A denser pool provides better numerical coverage but
does not add experimental information.

## 7. Gaussian-process surrogate

A **Gaussian process (GP) surrogate** is a probability model over possible
response functions. It supplies a predicted corrected NO and uncertainty at
unmeasured inputs. This section describes the NO minimisation model; mapping
fits two independent response models as described below. After the configured number of completed initial tests, the
program normalises every selected input coordinate. If $x_{ij}$ is the physical
value of variable $j$ in completed test $i$, then

$$
u_{ij}=\frac{x_{ij}-l_j}{h_j-l_j}.
$$

Stochastic-process surrogates for expensive response functions were developed
in the computer-experiment literature [[R6](#ref-r6)]. Jones, Schonlau and Welch
combined a kriging/GP-style surrogate with expected improvement in Efficient
Global Optimization [[R7](#ref-r7)]. The present application extends that
general expensive-black-box framework to noisy physical combustion tests; the
papers do not establish the physical safety or validity of this search region.
Williams and Rasmussen give the standard GP conditioning equations for
predictive mean and variance in their Eqs. 1–2 [[R10](#ref-r10)].

Implemented at [`bayesian.py:607`](../flow_controller/domain/bayesian.py#L607)
and [`bayesian.py:608`](../flow_controller/domain/bayesian.py#L608).

Let $y_i$ be corrected NO for test $i$, in ppm; $\bar y$ its arithmetic mean;
and $s_y=\max(\operatorname{std}(y),1\ \mathrm{ppm})$ its output scale. The
model is fitted to the dimensionless standardized response

$$
\tilde y_i=\frac{y_i-\bar y}{s_y}.
$$

The mean and scale are calculated at
[`bayesian.py:610`](../flow_controller/domain/bayesian.py#L610), and the
standardization is applied during fitting at
[`bayesian.py:618`](../flow_controller/domain/bayesian.py#L618).

A **kernel** is the GP covariance function: it specifies how strongly two input
points are statistically related. The surrogate uses a constant-amplitude
Matérn-5/2 kernel plus fitted white observation noise:

$$
k(u,u')=C\left(1+\sqrt5r+\frac{5r^2}{3}\right)e^{-\sqrt5r}
+\sigma_w^2\delta_{u,u'},
$$

where $k(u,u')$ is covariance between normalized points $u$ and $u'$; $C$ is a
fitted positive covariance amplitude; $r$ is their scaled distance;
$\sigma_w$ is the fitted standard deviation of independent residual observation
noise; and $\delta_{u,u'}$ is 1 when its two arguments refer to the same
observation and 0 otherwise. The kernel classes and numerical bounds are
selected at [`bayesian.py:612`](../flow_controller/domain/bayesian.py#L612) and
[`bayesian.py:613`](../flow_controller/domain/bayesian.py#L613).

Snoek, Larochelle and Adams give the ARD Matérn-5/2 covariance in this form in
their Section 3.1 and Eq. 5, and motivate it as a less extremely smooth
alternative to the squared-exponential kernel for practical Bayesian
optimisation [[R8](#ref-r8)]. This program uses
that covariance family and separate length scales, but optimises point estimates
of its kernel hyperparameters; it does not implement their fully Bayesian
hyperparameter marginalisation. Handcock and Stein give a primary statistical
treatment of the general Matérn covariance family in their Section 3
[[R11](#ref-r11)]; the polynomial-exponential form above is its
smoothness-$5/2$ specialization.

The distance uses **automatic relevance determination (ARD)**, meaning each
input dimension has its own fitted length scale:

$$
r^2=\sum_{j=1}^{d}\frac{(u_j-u'_j)^2}{\ell_j^2}.
$$

Here $\ell_j$ is the positive, dimensionless length scale for variable $j$.
There is one $\ell_j$ per selected variable because the code passes an array of
$d$ initial scales to `Matern` at
[`bayesian.py:612`](../flow_controller/domain/bayesian.py#L612) and
[`bayesian.py:613`](../flow_controller/domain/bayesian.py#L613). A short length
scale permits rapid variation along that coordinate; a long fitted scale
indicates a smoother or weakly identified effect over the declared range.

The Matérn part of the kernel is a covariance, or similarity, between two
operating conditions. It does **not** directly state the expected difference
between their corrected-NO values. A large Matérn covariance says that the
underlying NO responses at the two normalized conditions are expected to be
strongly related. A small covariance says that an observation at one condition
provides less information about the other. Neither value by itself is a
prediction of NO, an NO difference, or the next experiment.

The GP combines the kernel relationships with **all** completed observations.
For an unmeasured normalized condition $u_*$, its standardized posterior mean
is

$$
\mu(u_*)=k_{*X}\boldsymbol{\beta},
$$

where $u_*$ is the candidate condition; $X$ is the matrix of normalized
completed conditions; $k_{*X}$ is the covariance row between $u_*$ and every
condition in $X$; $\boldsymbol{\beta}$ is the fitted vector of observation
weights stored as `gp.alpha_` (the scikit-learn attribute name); and $\mu(u_*)$
is the GP's posterior mean of the standardized corrected-NO response. This
calculation is implemented at
[`bayesian.py:255`](../flow_controller/domain/bayesian.py#L255) to
[`bayesian.py:257`](../flow_controller/domain/bayesian.py#L257).

The candidate's latent posterior variance is

$$
\sigma^2(u_*)=k(u_*,u_*)-v_*^T v_*,
$$

where $\sigma^2(u_*)$ is uncertainty about the underlying noise-free response;
$k(u_*,u_*)$ is the fitted signal-kernel variance before conditioning on the
data; $v_*$ is the triangular-solve vector produced from the training
covariance; and the superscript $T$ denotes vector transpose. The triangular
solve is at [`bayesian.py:256`](../flow_controller/domain/bayesian.py#L256), and the
candidate variances are calculated at
[`bayesian.py:261`](../flow_controller/domain/bayesian.py#L261). Thus the GP,
rather than the Matérn covariance alone, produces the two quantities used by
the acquisition calculation: predicted mean $\mu(u_*)$ and latent standard
deviation $\sigma(u_*)=\sqrt{\sigma^2(u_*)}$.

For a supplied corrected-NO SEM $s_i$ in ppm, the regressor receives a
dimensionless per-observation variance

$$
\alpha_i=\left(\frac{s_i}{s_y}\right)^2+10^{-8},
$$

where $\alpha_i$ is the diagonal variance added for observation $i$. The SEM is
divided by $s_y$ at [`bayesian.py:611`](../flow_controller/domain/bayesian.py#L611),
then squared and given the $10^{-8}$ numerical floor at
[`bayesian.py:614`](../flow_controller/domain/bayesian.py#L614). The separate
`WhiteKernel` fits residual noise not supplied by the per-test SEM values.

## 8. Noisy expected improvement

This acquisition rule applies to **Minimise NO**. **Map NO + pressure** uses
the variance-reduction rule at the end of this section.

**Expected improvement (EI)** scores a candidate by the expected amount that
its objective will beat the current best value. Ordinary EI treats the best
observation as exact. **Noisy expected improvement (NEI)** instead averages EI
over uncertainty in the earlier measurements. A **latent value** is the GP's
inferred noise-free corrected NO at an input, rather than a noisy analyser
observation.

The analytic EI criterion follows the Efficient Global Optimization treatment
of Jones, Schonlau and Welch [[R7](#ref-r7)]. Letham et al. derive noisy EI by
integrating over posterior uncertainty in the latent values of noisy earlier
observations in Sections 3.2–4 [[R9](#ref-r9)]. This implementation uses that
posterior-integration idea for a sequential, unconstrained objective, with 128
ordinary Monte Carlo draws and a finite feasible candidate pool. It does not
reproduce Letham et al.'s constrained or quasi-Monte Carlo algorithm in full.

This is a minimisation problem: lower corrected NO is better. NEI therefore
balances **exploitation**, proposing a condition with a low posterior-mean NO,
against **exploration**, proposing a condition whose uncertainty leaves a
meaningful chance of a lower result. For example, suppose the GP reports:

| Candidate | Posterior-mean corrected NO (ppm) | Latent standard deviation (ppm) |
| --- | ---: | ---: |
| A | 80 | 2 |
| B | 74 | 4 |
| C | 78 | 12 |

Candidate B has the lowest predicted mean, while C has the greatest
uncertainty. These two columns alone do not establish which candidate wins:
the NEI calculation also integrates over the uncertain latent values at the
baseline conditions. The detailed calculation below assigns the acquisition
score.

The latent posterior excludes the fitted WhiteKernel observation-noise term.
Here a **posterior** is the GP distribution after conditioning on completed
data; a **baseline** is a previously measured input used to define the best
value; and $K_{BB}$ is the latent covariance matrix between all baseline
points. The implementation deliberately selects only the non-white-noise part
of the fitted kernel at
[`bayesian.py:253`](../flow_controller/domain/bayesian.py#L253). The posterior
mean and baseline/candidate covariance blocks are assembled at
[`bayesian.py:254`](../flow_controller/domain/bayesian.py#L254) to
[`bayesian.py:261`](../flow_controller/domain/bayesian.py#L261).

For numerical stability the implementation adds **jitter**, a very small
positive value $\epsilon$, to the covariance diagonal:

$$
\widetilde K_{BB}=K_{BB}+\epsilon I,\qquad
\epsilon=\max\left(10^{-10},10^{-8}\max_i(K_{BB})_{ii}\right),
$$

where $I$ is the identity matrix and $(K_{BB})_{ii}$ is baseline variance $i$.
The jitter and Cholesky factorization are implemented at
[`bayesian.py:270`](../flow_controller/domain/bayesian.py#L270) and
[`bayesian.py:271`](../flow_controller/domain/bayesian.py#L271).

**Monte Carlo integration** approximates an expectation by averaging random
draws. For each of $M=128$ draws, the program samples one plausible latent
baseline vector

$$
f_B^{(m)}\sim\mathcal N(\mu_B,\widetilde K_{BB}).
$$

Here $f_B^{(m)}$ is draw $m$ of all baseline latent values; $\mathcal N$ denotes
a multivariate normal distribution; $\mu_B$ is the baseline posterior-mean
vector; and $\widetilde K_{BB}$ is the jittered posterior covariance. The draws
are formed from its Cholesky factor at
[`bayesian.py:271`](../flow_controller/domain/bayesian.py#L271) to
[`bayesian.py:272`](../flow_controller/domain/bayesian.py#L272). The default
$M=128$ is declared at [`bayesian.py:265`](../flow_controller/domain/bayesian.py#L265).

Each draw is a **fantasy**, a plausible noise-free history used only inside the
acquisition calculation. The program conditions the candidate distribution on
that fantasy. The conditional mean is calculated as

$$
\mu_c^{(m)}=\mu_c+
\left(f_B^{(m)}-\mu_B\right)^T \widetilde K_{BB}^{-1}K_{Bc},
$$

where $\mu_c$ is the candidate posterior mean before conditioning and $K_{Bc}$
is covariance between baseline points and candidate $c$. The linear solve and
conditional mean are implemented at
[`bayesian.py:273`](../flow_controller/domain/bayesian.py#L273) and
[`bayesian.py:274`](../flow_controller/domain/bayesian.py#L274). The candidate's
conditional standard deviation is

$$
\sigma_c=\sqrt{K_{cc}-K_{cB}\widetilde K_{BB}^{-1}K_{Bc}},
$$

where $K_{cc}$ is the candidate latent variance and $K_{cB}=K_{Bc}^T$.
Implemented at [`bayesian.py:275`](../flow_controller/domain/bayesian.py#L275),
with a $10^{-12}$ lower variance floor to avoid division by zero.

Let the fantasy's best, meaning lowest, latent baseline be
$b^{(m)}=\min f_B^{(m)}$. Let $\Delta^{(m)}$ be improvement relative to the
conditional mean and let $z^{(m)}$ express that improvement in conditional
standard-deviation units:

$$
\Delta^{(m)}=b^{(m)}-\mu_c^{(m)},\qquad
z^{(m)}=\frac{\Delta^{(m)}}{\sigma_c}.
$$

These two quantities are implemented at
[`bayesian.py:276`](../flow_controller/domain/bayesian.py#L276) and
[`bayesian.py:277`](../flow_controller/domain/bayesian.py#L277). The conditional
analytic expected improvement for minimisation is

$$
EI^{(m)}=\Delta^{(m)}\Phi(z^{(m)})+
\sigma_c\varphi(z^{(m)}),
$$

where $\Phi$ is the standard-normal cumulative distribution function (CDF),
the probability that a standard-normal variable is no greater than its
argument, and $\varphi$ is the standard-normal probability density function
(PDF). This closed-form expression is the minimisation form of Eq. 15 in Jones,
Schonlau and Welch [[R7](#ref-r7)]. The `ndtr` call
implements $\Phi$ and the exponential term implements
$\varphi$ at
[`bayesian.py:278`](../flow_controller/domain/bayesian.py#L278). The noisy
expected improvement is the Monte Carlo mean

$$
NEI=\frac1M\sum_{m=1}^{M}EI^{(m)},\qquad M=128.
$$

The mean and non-negative clipping are implemented at
[`bayesian.py:279`](../flow_controller/domain/bayesian.py#L279). The feasible
candidate with the largest NEI is selected:

$$
u_{\mathrm{next}}=\arg\max_{u\in\mathcal F}NEI(u),
$$

where $u_{\mathrm{next}}$ is the next normalized operating condition;
$\mathcal F$ is the finite set of feasible, untried candidates; $u$ is one
candidate in that set; and $\arg\max$ means the candidate at which NEI is
largest. This selection is implemented at
[`bayesian.py:624`](../flow_controller/domain/bayesian.py#L624). The displayed
`predicted_no` is the latent posterior mean, `latent_sd` ($\sigma$ in the UI
description) is its latent standard deviation, and `expected_improvement` is
NEI. All three are converted from the standardized model scale back to ppm at
[`bayesian.py:650`](../flow_controller/domain/bayesian.py#L650) to
[`bayesian.py:652`](../flow_controller/domain/bayesian.py#L652).

Consequently, the program does not select the largest predicted NO, the
largest Matérn-kernel value, the most distant condition, or necessarily the
lowest posterior-mean NO. After initialization, it selects the feasible
candidate with the largest NEI because that score represents the best current
balance between possible lower NO and uncertainty.

### Mapping acquisition and operating-space maps

After the completed initial design, a new **Map NO + pressure** campaign fits
three independent Matérn-5/2 Gaussian processes: one for corrected dry NO and
one for each transducer's dominant in-band spectral amplitude. All three use
measured flow-derived input coordinates. Each
response is centred and scaled by its own sample-set standard deviation
(`ddof=0`); a constant response uses `max(abs(mean), 1)` as its scale. This
differs from the NO-only fit's minimum scale of 1 ppm. Optional corrected NO
SEM supplies known observation variance. Pressure observation noise is fitted;
block-RMS spread is not treated as a pressure SEM. The implementation is
`_fit_mapping_models()` in [`bayesian.py`](../flow_controller/domain/bayesian.py).

The candidate score measures the fractional reduction in average latent
variance across a common reference set. For response $j$, let $C_j(r,x)$ be
the fitted latent posterior covariance between reference point $r$ and
candidate $x$, $v_j(x)$ its latent variance, and $\sigma^2_{n,j}$ the fitted
white-noise variance, all in that model's standardised units. The implemented
score is

$$
R_j(x)=
\frac{\frac{1}{M}\sum_{r\in\mathcal R} C_j(r,x)^2}
{\left(v_j(x)+\sigma^2_{n,j}+10^{-8}\right)
 \max\left(\frac{1}{M}\sum_{r\in\mathcal R}v_j(r),10^{-12}\right)}.
$$

Implemented at [`bayesian.py:392`](../flow_controller/domain/bayesian.py#L392) to
[`bayesian.py:395`](../flow_controller/domain/bayesian.py#L395).
Here $\mathcal R$ contains up to 256 feasible points from the Sobol pool and
$M$ is its size. It is fixed across candidates for one suggestion, chosen
before previously attempted points are removed from the candidate set, and
independent of measured response values. Negative numerical scores are
clamped to zero. The selected next test maximises

$$
S(x)=wR_{NO}(x)+\frac{1-w}{2}\left(R_{P1}(x)+R_{P2}(x)\right),
\qquad 0<w<1.
$$

Implemented at [`bayesian.py:550`](../flow_controller/domain/bayesian.py#L550) to
[`bayesian.py:551`](../flow_controller/domain/bayesian.py#L551).
The default $w=0.5$ assigns half the acquisition score to NO and divides the
other half equally between the two pressure maps. Standardising each response
before fitting prevents the channel with the larger numerical pressure scale
from receiving more weight merely because of its units or calibration range.
Changing the weight does not define a combined emissions/pressure performance
objective. A mapping proposal may have higher NO or pressure than a previous
test because it is selected for information. The calculations are implemented
by `integrated_variance_reduction()` and the mapping branch of `suggest()` in
[`bayesian.py`](../flow_controller/domain/bayesian.py). New dual-transducer
suggestions store algorithm ID `fcbo-matern52-no-dual-pressure-ivr-v1`;
legacy one-transducer campaigns retain `fcbo-matern52-no-pressure-ivr-v1`.

In a **larger of the two transducers** campaign four models are fitted: NO,
each transducer's selected response, and $P_{max}=\max(P_1,P_2)$ formed per
test at [`bayesian.py:364`](../flow_controller/domain/bayesian.py#L364). Only
the last competes with NO for the next test,

$$
S(x)=wR_{NO}(x)+(1-w)R_{P_{max}}(x),
$$

at [`bayesian.py:548`](../flow_controller/domain/bayesian.py#L548) and
[`bayesian.py:551`](../flow_controller/domain/bayesian.py#L551). The two
per-transducer models are fitted for the maps and the saved record only. These
suggestions store algorithm ID `fcbo-matern52-no-larger-pressure-ivr-v1`.
$P_{max}$ has its own model, so its map is not required to equal the larger of
the two per-transducer maps at every cell, and it has a crease wherever the
louder transducer changes.

After the initial completed design, use **Operating-space maps**, select
**Horizontal** and **Vertical** variables, then click **Refresh maps**. The
plots evaluate a 20 × 20 slice. Other variables remain at the selected
completed test's measured condition, or at their bounds midpoint. **Show
uncertainty (latent SD)** switches between predicted means and latent standard
deviations. Infeasible cells are blank, and changing the campaign, slice or
flow ceilings requires refreshing the predictions. New campaigns show three
plots: corrected NO in ppm and each labelled transducer's spectral amplitude in
Pa RMS. Larger-transducer campaigns add a fourth plot for the larger of the two
readings, and bulk-velocity campaigns add a thermal-input plot. Thermal input is
calculated at each cell from the flow equations at
[`qt_optimiser.py:429`](../flow_controller/ui/qt_optimiser.py#L429), not fitted,
so it has no uncertainty view.

`predict_mapping()` uses deterministic seed-0 fits for these displays; no
candidate is selected and no flow command is sent. Rendering and worker
lifetime are handled in [`qt_optimiser.py`](../flow_controller/ui/qt_optimiser.py).
These maps do not classify flame stability or establish a safe operating
region, and the application does not automatically choose a stopping point.

## 9. Experiment records and export

An experiment is stored as `.fcbo.json`. New campaigns use campaign schema 5,
which adds the throughput choice, Stage 1 inlet area and pressure-acquisition
setting to the configuration;
the loader also accepts schema-1 to schema-4 campaigns. Files without
an objective mode retain NO minimisation, and old one-transducer mapping records
keep their flat source, pressure and prediction fields. Keep a copy before
modifying a campaign with this version: earlier applications cannot read schema
4. The campaign JSON is the
authoritative scientific record. A **condition log** is the self-contained audit
record stored in a finalised trial under `condition_log`. Every newly completed
or invalid condition receives condition-log schema 1 before the campaign is
saved. Completion and invalidation attach the log at
[`optimisation.py:474`](../flow_controller/core/optimisation.py#L474) to
[`optimisation.py:501`](../flow_controller/core/optimisation.py#L501). The whole
campaign snapshot is written to a temporary file, flushed to disk and atomically
replaced at [`optimisation.py:102`](../flow_controller/core/optimisation.py#L102) to
[`optimisation.py:116`](../flow_controller/core/optimisation.py#L116); **atomic**
here means that a failed replacement leaves the preceding complete JSON file in
place rather than a partly written condition.

The condition log freezes the information needed to interpret one requested
condition:

- trial identity, repeat relationship and suggestion method, including the seed,
  candidate-pool counts, training-data hash and fitted-model provenance when the
  suggestion came from the Bayesian stage;
- requested variable names and values, target flows, O2 reference, objective and
  pilot requirement;
- the measurement window, including target flows, mean controller setpoints and
  their per-role statistics, mean measured flows and their separate per-role
  statistics, assignments, start/end rig boundary contexts and the continuous
  flow audit-log path. A **rig boundary context** is the compact snapshot of assigned units,
  declared limits, prepared targets and live operating metadata taken at the
  start or end of the averaging window; its capture is implemented at
  [`optimiser_controller.py:309`](../flow_controller/core/optimiser_controller.py#L309)
  to [`optimiser_controller.py:323`](../flow_controller/core/optimiser_controller.py#L323);
- all available MEXA channel and acquisition-cycle statistics, receiver-time
  bounds, states, alarms, warnings, sequence provenance and receiver audit-log
  path for live capture;
- pressure results and their channel, calibration, analysis settings, source
  file/hash and timing association; plain-trigger windows also retain
  `labview_capture` with physical start/stop, NO start/end, response delay,
  minimum pressure duration and the TDMS source profile;
- the corrected result and notes for a completed condition, or the operator's
  invalid reason for an invalid condition; and
- the response-time calibration selected for that window, frozen as a bounded
  summary rather than linked only to mutable campaign-level history.

Record construction is at
[`optimisation.py:766`](../flow_controller/core/optimisation.py#L766) to
[`optimisation.py:807`](../flow_controller/core/optimisation.py#L807), and loading
checks that the log agrees with the saved trial, window and outcome at
[`optimisation.py:810`](../flow_controller/core/optimisation.py#L810) to
[`optimisation.py:836`](../flow_controller/core/optimisation.py#L836). MEXA
`NO` and `O2` are validated for optimisation. Auxiliary channels such as CO,
CO2, HC, AFR, lambda, RPM, oil temperature and PEF are stored when available but
are explicitly informational and are not separately validated; missing values
are omitted from that channel's statistics rather than filled. That distinction
is recorded at [`records.py:271`](../mexa_bridge/records.py#L271) to
[`records.py:273`](../mexa_bridge/records.py#L273).

The requested **target flow** is the desired value loaded for a role. A
**setpoint** is the controller's commanded value read back during the window,
while a **measured flow** is its reported process value. The log keeps the
setpoint and measured-flow series separate: `mean_setpoints` and
`setpoint_statistics` describe commands, while `mean_flows` and
`flow_statistics` describe measured delivery. Their collection from the same
polling pass is shown at
[`optimiser_controller.py:278`](../flow_controller/core/optimiser_controller.py#L278)
to [`optimiser_controller.py:307`](../flow_controller/core/optimiser_controller.py#L307).

For either setpoints, measured flows or an available MEXA channel, let the
retained values be $x_1,\ldots,x_n$. The saved arithmetic mean, sample standard
deviation, minimum and maximum are

$$
\bar{x}=\frac{1}{n}\sum_{i=1}^{n}x_i,\qquad
s=\sqrt{\frac{1}{n-1}\sum_{i=1}^{n}(x_i-\bar{x})^2},\qquad
x_{\min}=\min_i x_i,\quad x_{\max}=\max_i x_i.
$$

The same summary function calculates setpoint and measured-flow statistics with
`numpy.mean`, `numpy.std(..., ddof=1)`, `numpy.min` and `numpy.max` at
[`optimisation.py:1272`](../flow_controller/core/optimisation.py#L1272) to
[`optimisation.py:1282`](../flow_controller/core/optimisation.py#L1282). MEXA uses
`statistics.mean`, `statistics.stdev`, `min` and `max` at
[`records.py:249`](../mexa_bridge/records.py#L249) to
[`records.py:252`](../mexa_bridge/records.py#L252). Here `ddof=1` and
`statistics.stdev` both use the $n-1$ sample-SD denominator. When an auxiliary
MEXA channel has only one retained value, its SD is saved as `null`; the MEXA
summary saves each channel's own sample count because auxiliary channels can be
absent from some packets.

The NO result remains the oxygen-corrected mean defined in Section 4:

$$
NO_{corr}=NO_{raw}\frac{20.9-O_{2,ref}}{20.9-O_{2,meas}}.
$$

Range checks, the correction factor and optional NO-SEM scaling are implemented
at [`bayesian.py:239`](../flow_controller/domain/bayesian.py#L239) to
[`bayesian.py:247`](../flow_controller/domain/bayesian.py#L247). The live MEXA
path first calculates arithmetic channel means at
[`records.py:264`](../mexa_bridge/records.py#L264) to
[`records.py:269`](../mexa_bridge/records.py#L269); it does not average
already corrected samples.

A **SHA-256 digest** is a fixed-length identifier calculated from bytes. For
provenance the program uses

$$
h=\operatorname{SHA256}(\text{canonical JSON encoded as UTF-8}),
$$

where canonical JSON here means sorted keys, finite values and compact
separators. Each suggestion stores the digest of the exact training payload—trial
ID, observed coordinates, corrected NO and corrected SEM—at
[`bayesian.py:494`](../flow_controller/domain/bayesian.py#L494) to
[`bayesian.py:521`](../flow_controller/domain/bayesian.py#L521). The later
Matérn-5/2/NEI stage also stores the fitted kernel, signal variance, length
scales, white-noise level, response normalization and Monte Carlo draw count at
[`bayesian.py:637`](../flow_controller/domain/bayesian.py#L637) to
[`bayesian.py:647`](../flow_controller/domain/bayesian.py#L647). This connects a
condition to the Matérn Gaussian-process and noisy expected improvement (NEI)
calculation described in Section 8.

A selected response calibration can contain thousands of high-frequency raw
samples. Its frozen condition-log summary therefore stores `raw_sample_count`
and `raw_samples_sha256` instead of duplicating the `raw_samples` array. The
count and digest are produced at
[`optimisation.py:758`](../flow_controller/core/optimisation.py#L758) to
[`optimisation.py:760`](../flow_controller/core/optimisation.py#L760). The digest
supports identity and change detection; it does not reconstruct the omitted
samples.

CSV is an on-demand view, not the authoritative record. Export starts at
[`optimisation.py:503`](../flow_controller/core/optimisation.py#L503). It now
includes flattened trial, suggestion, model, flow, rig and MEXA provenance
fields, separate JSON cells for setpoint and measured-flow summaries, other JSON
cells for nested structures, and the complete compact
`condition_log_json` cell. Stable JSON-cell encoding is implemented at
[`optimisation.py:683`](../flow_controller/core/optimisation.py#L683) to
[`optimisation.py:686`](../flow_controller/core/optimisation.py#L686), and the
expanded rows are assembled at
[`optimisation.py:551`](../flow_controller/core/optimisation.py#L551) to
[`optimisation.py:675`](../flow_controller/core/optimisation.py#L675). Operator
notes that begin like spreadsheet formulas are prefixed to prevent formula
execution when the CSV is opened.

Raw high-frequency data remain in separate acquisition files. The condition log
retains their audit paths and bounded summaries: the flow window stores its
continuous-log reference at
[`optimisation.py:1304`](../flow_controller/core/optimisation.py#L1304) to
[`optimisation.py:1305`](../flow_controller/core/optimisation.py#L1305), while the
MEXA receiver writes each packet to separate JSONL and CSV logs at
[`records.py:128`](../mexa_bridge/records.py#L128) to
[`records.py:166`](../mexa_bridge/records.py#L166). Keep those raw files
with the campaign JSON when packet-level or flow-pass reconstruction is required.
The flow audit path is absent when continuous flow logging was not active; live
MEXA capture, by contrast, requires its receiver audit log before a window can
start.

For mapping, each trial also stores a `capture_id` and a validated `pressure`
summary. New summaries contain two transducer entries under the stable IDs
`pressure_1` and `pressure_2`; the TDMS file identity and selected interval are
shared. The campaign keeps its `tdms_source` profile and the comparison
`pressure_signature` fixed by the first attached pressure result. A saved NO
window can wait for a pressure-file retry without losing its averages.
`Experiment.attach_pressure()` accepts an identical retry but rejects an
attempt to replace an existing result with different pressure data. Mapping
completion and model fitting require valid pressure for every completed test.

CSV export retains the legacy pressure columns and adds fixed pressure-1 and
pressure-2 label, dominant-amplitude, dominant-frequency, suggested-mean and
latent-SD columns. It also includes the objective mode, capture ID, shared
raw-file reference, full pressure summary JSON and mapping score. Keep
the raw TDMS files alongside the flow and MEXA audit logs to reconstruct the
measurement from source samples.

The existing `flow-pressure-v1` JSON summary and file-ready routes remain for
advanced integrations. **Export LabVIEW request…** and the payloads under
[`docs/examples`](examples) support those routes; they are optional and are
not prerequisites for the plain `log`/`stop` TDMS procedure.

## 10. Choosing the dimensionality

Adding a variable expands the region the optimiser must cover. Four nominal
levels correspond to 64 combinations in three dimensions, 256 in four and
1,024 in five. Bayesian optimisation does not enumerate that grid, but data
sparsity still increases with dimension.

Add power or split only when:

- the variable can be controlled and reconstructed from measurements;
- all combinations inside the rectangular bounds are meaningful and approved;
- its effect on corrected NO is part of the scientific question;
- the available number of experiments can support the larger search.

Do not add two coordinates for quantities that must always follow one prescribed
relationship. Such a relationship is a lower-dimensional path and requires a
dedicated path parameter or explicit constraint; the present dialog searches a
rectangular box in the selected variables.

The initial design defaults to the minimum: four completed tests for three
variables, five for four variables, or six for five variables. The dialog updates
this default when optional variables are selected or cleared. A larger initial
design can still be entered. These minimum counts do not guarantee model accuracy. Increase
the initial design and candidate pool when broad bounds, interactions or short
length scales make the response harder to resolve. Reserve experiments for
reference-condition repeats and confirmation of the apparent optimum.

## 11. Interpretation and stopping

The history's lowest corrected NO is the lowest observation, not a certified
global minimum. Before accepting a result:

1. repeat the proposed best point;
2. repeat a reference condition to check drift;
3. inspect raw NO, O2, flow tracking and analyser provenance;
4. check whether the optimum lies on a bound, which can indicate that the
   declared region truncated the response;
5. compare the improvement with repeatability and measurement uncertainty;
6. assess outcomes the optimiser does not measure, including NH3 slip, NO2,
   N2O, combustion efficiency and flame behaviour.

The program never stops a campaign by itself. The campaign plan and the
convergence readout below are a guide; the operator decides whether the
expected improvement, uncertainty, repeats and experimental budget justify
another trial. No method can prove from a finite number of tests that a global
minimum has been found, and a **Converged** readout is not such proof. For
**Minimise NO**, the readout warns when the model's best point is at a bound
(item 4) and when replicates have not yet measured the noise (item 5).

For mapping campaigns, assess uncertainty across the relevant operating region
and repeat selected conditions to check both NO and pressure reproducibility.
The mapping score describes information gain under the fitted models; it is
not a burner-performance ranking. Pressure amplitudes alone are not an
automatic stable/unstable classification.

### Campaign plan and convergence readout

A campaign can carry a planned number of completed tests and the settings of a
convergence readout. The readout is a stopping guide. No method can prove from
a finite number of tests that a global minimum has been found. **Converged**
means that the model's best point is the best found so far and that, given the
tests run so far and a model whose own uncertainty is trusted, there is no
evidence of a better region. The readout never stops the campaign by itself.

The settings are chosen when the campaign is created:

| Setting (file key) | Symbol | Default | Allowed values |
| --- | --- | --- | --- |
| Planned completed tests (`planned_tests`) | $N$ | 10 per searched variable in the dialog | blank (`null`), or a whole number from the initial-design size to 500 and at least $n_{min}$ |
| Minimum completed tests before convergence (`min_tests`) | $n_{min}$ | 5 per searched variable, at least the initial-design size | whole number from the initial-design size to 500 |
| Smallest NO improvement that matters (`min_improvement_ppm`) | $\Delta_{min}$ | 1 ppm | $0<\Delta_{min}\le 5000$ ppm; Minimise NO only |
| Mapping convergence threshold (`gain_fraction`) | $g$ | 0.02 (2%) | $0<g\le 1$; Map NO + pressure only |
| Consecutive suggestions meeting the criteria (`consecutive`) | $m$ | 3 | whole number from 1 to 20; 3 to 5 is recommended |

The fixed constants are the lower-confidence-bound multiplier $\kappa=2$ and
the exclusion radius $\rho=0.15$ at
[`bayesian.py:53`](../flow_controller/domain/bayesian.py#L53) and
[`bayesian.py:54`](../flow_controller/domain/bayesian.py#L54); the Matérn
length-scale bounds $0.03\le\ell_j\le 3.0$, used by every GP model, at
[`bayesian.py:50`](../flow_controller/domain/bayesian.py#L50); and the location
tolerance 0.05 and bound tolerance 0.02 at
[`convergence.py:36`](../flow_controller/domain/convergence.py#L36) and
[`convergence.py:38`](../flow_controller/domain/convergence.py#L38).

#### Plan and extension

Let $n_c$ be the number of completed tests. The plan is reached when

$$
N\ne\text{null}\quad\text{and}\quad n_c\ge N.
$$

Implemented at [`optimisation.py:245`](../flow_controller/core/optimisation.py#L245).

While the plan is reached, **Suggest next test** is refused until the operator
extends the plan; repeats, completion of a pending test and invalidation are
unaffected. **Extend plan** (**Set plan** for an open-ended campaign) adds
$k\ge1$ tests:

$$
N'=\max(N,n_c)+k,\qquad N'\le 500,
$$

with $N=0$ for an open-ended campaign. Implemented at
[`optimisation.py:260`](../flow_controller/core/optimisation.py#L260) and
[`optimisation.py:261`](../flow_controller/core/optimisation.py#L261). The new
plan is then validated, so $N'$ must also be at least $n_{min}$.

#### Diagnostics stored with each suggestion

For **Minimise NO**, `suggest()` stores four diagnostics with each model
suggestion. They are calculated after the NEI scores, with no further random
draws, so they do not change which point is suggested. Let $U$ be the set of
distinct normalized completed conditions and $\mu(u)$, $\sigma(u)$ the latent
posterior mean and standard deviation of Section 7. The model's best point and
its best estimate, in ppm, are

$$
u_b=\arg\min_{u\in U}\mu(u),\qquad
\hat y_b=\bar y+s_y\,\mu(u_b),
$$

stored as `best_model_point` (in physical units, $l_j+u_{b,j}(h_j-l_j)$) and
`best_model_no`. Implemented at
[`bayesian.py:625`](../flow_controller/domain/bayesian.py#L625),
[`bayesian.py:630`](../flow_controller/domain/bayesian.py#L630) and
[`bayesian.py:631`](../flow_controller/domain/bayesian.py#L631). The best point
is therefore always a condition that has been tested.

The lowest lower confidence bound away from that point is

$$
L=\min_{u\in\mathcal F,\ \max_j|u_j-u_{b,j}|>\rho}
\left[\bar y+s_y\left(\mu(u)-\kappa\,\sigma(u)\right)\right],
$$

stored as `lowest_lcb_elsewhere` in ppm, or `null` when no candidate in the
pool $\mathcal F$ of Section 8 lies outside the radius. Implemented at
[`bayesian.py:627`](../flow_controller/domain/bayesian.py#L627),
[`bayesian.py:628`](../flow_controller/domain/bayesian.py#L628) and
[`bayesian.py:632`](../flow_controller/domain/bayesian.py#L632).

The model's fitted measurement noise, in ppm, is

$$
\hat\sigma_{fit}=s_y\sqrt{\sigma_w^2},
$$

stored as `noise_sd_ppm`. Implemented at
[`bayesian.py:635`](../flow_controller/domain/bayesian.py#L635). The suggestion
also stores $\kappa$ (`lcb_multiplier`) and $\rho$ (`elsewhere_radius`).
Suggestions made before this version have none of these fields.

#### Noise estimate and threshold

A replicate group is an original completed test together with its completed
repeats; a repeat of a repeat joins the original's group. For group $r$ with
$n_r$ completed corrected-NO values $y_{r,i}$ and mean $\bar y_r$, the pooled
replicate standard deviation is

$$
\hat\sigma_{rep}=\sqrt{\frac{\sum_r\sum_{i=1}^{n_r}(y_{r,i}-\bar y_r)^2}{\nu}},
\qquad \nu=\sum_r(n_r-1).
$$

Implemented at [`convergence.py:144`](../flow_controller/domain/convergence.py#L144).
The noise used by the readout is

$$
\hat\sigma=
\begin{cases}
\hat\sigma_{rep}, & \nu\ge 2,\\
\hat\sigma_{fit}\ \text{of the latest considered suggestion}, & \text{otherwise}.
\end{cases}
$$

Implemented at [`convergence.py:261`](../flow_controller/domain/convergence.py#L261)
and [`convergence.py:264`](../flow_controller/domain/convergence.py#L264). A
single repeated pair ($\nu=1$) is not enough to replace the fitted value. The
threshold, in ppm, is

$$
T=\max(\Delta_{min},\hat\sigma),
$$

with $\hat\sigma=0$ when neither estimate is available. Implemented at
[`convergence.py:266`](../flow_controller/domain/convergence.py#L266). An
improvement smaller than the measurement noise could not be confirmed by
another test.

#### Minimise NO checks

The readout is not assessed until $n_c$ reaches the initial-design size. After
that, it considers the trials, of any status, that carry `expected_improvement`
and the three diagnostics above with finite values (a non-negative expected
improvement, and `lowest_lcb_elsewhere` either finite or `null`). Repeats,
initial-design tests and suggestions made before this version carry no
diagnostics and are skipped. With fewer than $m$ such trials the readout is not
yet assessable. Otherwise let $i=1,\ldots,m$ index the last $m$ of them, with
$i=m$ the latest. The campaign reads **Converged** only when all four checks
pass:

1. Expected improvement:

   $$
   NEI_i\le T\quad\text{for all } i.
   $$

   Implemented at [`convergence.py:277`](../flow_controller/domain/convergence.py#L277).

2. No better region elsewhere. The check fails when

   $$
   L_i<\hat y_{b,i}-T\quad\text{for any } i;
   $$

   a suggestion with $L_i$ `null` passes. Implemented at
   [`convergence.py:281`](../flow_controller/domain/convergence.py#L281).

3. Stable best location. With $x_{b,i}$ the stored best point of suggestion
   $i$,

   $$
   D=\max_i\max_j\frac{|x_{b,i,j}-x_{b,m,j}|}{h_j-l_j}\le 0.05.
   $$

   Implemented at [`convergence.py:296`](../flow_controller/domain/convergence.py#L296)
   and [`convergence.py:299`](../flow_controller/domain/convergence.py#L299).

4. Minimum completed tests:

   $$
   n_c\ge n_{min}.
   $$

   Implemented at [`convergence.py:152`](../flow_controller/domain/convergence.py#L152).

Values exactly at a threshold pass. When converged, the detail line gives the
latest $\hat y_b$ and $x_b$ and the lowest observed corrected NO; otherwise it
gives the text of each failed check.

#### Map NO + pressure checks

The gains $S_1,\ldots,S_m$ are the stored `mapping_score` values (Section 8)
of the last $m$ trials whose suggestion recorded a finite non-negative score.
The campaign reads **Converged** when both checks pass:

1. Gain:

   $$
   S_i\le g\quad\text{for all } i.
   $$

   Implemented at [`convergence.py:227`](../flow_controller/domain/convergence.py#L227).

2. Minimum completed tests, $n_c\ge n_{min}$, as in check 4 above.

#### Warnings

Warnings are shown as **Note:** lines under the readout and never change its
state. They are produced for **Minimise NO** from the latest considered
suggestion.

- The best point is at a search bound, for any variable $j$, when

  $$
  \frac{x_{b,m,j}-l_j}{h_j-l_j}\le 0.02\quad\text{or}\quad
  \frac{h_j-x_{b,m,j}}{h_j-l_j}\le 0.02.
  $$

  Implemented at [`convergence.py:316`](../flow_controller/domain/convergence.py#L316)
  and [`convergence.py:319`](../flow_controller/domain/convergence.py#L319). The
  true minimum may lie outside the declared region.

- A variable is treated as having almost no effect when its fitted length
  scale is at the upper bound:

  $$
  \ell_j\ge 0.999\times 3.0.
  $$

  Implemented at [`convergence.py:326`](../flow_controller/domain/convergence.py#L326).
  The model's uncertainty along that variable may then be too small.

- Measurement noise has not been measured when replicates do not supply the
  noise estimate:

  $$
  \nu<2.
  $$

  Implemented at [`convergence.py:329`](../flow_controller/domain/convergence.py#L329).
  Repeat one completed test 3 to 5 times.

- When the readout is **Converged**, a reminder to repeat the best test 2 to 3
  times and to run a few tests away from it. Implemented at
  [`convergence.py:331`](../flow_controller/domain/convergence.py#L331).

#### Limitations

Every check uses the values the model computed when each test was suggested.
They are not recomputed after later data, so they can be out of date once
later tests change the fit. A model that is wrong about the response can be
confident that further tests will gain little and that no other region is
better, for example after an unlucky set of early tests or when a variable's
length scale is at its upper bound. **Converged** is therefore a stopping guide,
not proof that the optimum or the map is correct. Confirm the result with the
checks listed above.

#### Stored plan

New campaigns store a top-level `plan` object in the `.fcbo.json` file; the
schema number stays at 5:

```json
"plan": {
  "planned_tests": 30,
  "min_tests": 15,
  "min_improvement_ppm": 1.0,
  "gain_fraction": 0.02,
  "consecutive": 3,
  "extensions": [
    {"at": "2026-11-27T10:00:00+00:00", "from": 30, "to": 35,
     "completed": 30, "state": "not_converged", "detail": "Expected improvement of ..."}
  ]
}
```

The values must lie in the ranges in the settings table, and `planned_tests`,
when set, cannot be fewer than `min_tests`. Each extension records when it was
made, the previous and new planned counts, the completed count and the
convergence state and detail at that moment; a campaign allows at most 100
extensions. Files without `plan` load as open-ended with the default settings.
Unknown fields, missing fields and malformed entries are rejected on load. The
plan is not exported to CSV.

## 12. Literature and standards basis

<a id="ref-r1"></a>**R1.** C. E. Baukal and P. B. Eleazer, “Quantifying NOx
for Industrial Combustion Processes,” *Journal of the Air & Waste Management
Association*, 48(1), 52–58, 1998.
[doi:10.1080/10473289.1998.10463664](https://doi.org/10.1080/10473289.1998.10463664).

<a id="ref-r2"></a>**R2.** M. Takriti, P. M. Wynn, D. M. O. Elias, S. E.
Ward, S. Oakley and N. P. McNamara, “Mobile methane measurements: effects of
instrument specifications on data interpretation, reproducibility, and
isotopic precision,” *Atmospheric Environment*, 246, 118067, 2021.
[doi:10.1016/j.atmosenv.2020.118067](https://doi.org/10.1016/j.atmosenv.2020.118067).

<a id="ref-r3"></a>**R3.** A. Horsley, K. Macleod, R. Gupta, N. Goddard and
N. Bell, “Enhanced Photoacoustic Gas Analyser Response Time and Impact on
Accuracy at Fast Ventilation Rates during Multiple Breath Washout,” *PLOS ONE*,
9(6), e98487, 2014.
[doi:10.1371/journal.pone.0098487](https://doi.org/10.1371/journal.pone.0098487).

<a id="ref-r4"></a>**R4.** I. M. Sobol', “On the distribution of points in a
cube and the approximate evaluation of integrals,” *USSR Computational
Mathematics and Mathematical Physics*, 7(4), 86–112, 1967.
[doi:10.1016/0041-5553(67)90144-9](https://doi.org/10.1016/0041-5553(67)90144-9).

<a id="ref-r5"></a>**R5.** A. B. Owen, “Scrambling Sobol' and
Niederreiter–Xing Points,” *Journal of Complexity*, 14(4), 466–489, 1998.
[doi:10.1006/jcom.1998.0487](https://doi.org/10.1006/jcom.1998.0487).

<a id="ref-r6"></a>**R6.** J. Sacks, W. J. Welch, T. J. Mitchell and H. P.
Wynn, “Design and Analysis of Computer Experiments,” *Statistical Science*,
4(4), 409–423, 1989.
[doi:10.1214/ss/1177012413](https://doi.org/10.1214/ss/1177012413).

<a id="ref-r7"></a>**R7.** D. R. Jones, M. Schonlau and W. J. Welch,
“Efficient Global Optimization of Expensive Black-Box Functions,” *Journal of
Global Optimization*, 13, 455–492, 1998.
[doi:10.1023/A:1008306431147](https://doi.org/10.1023/A:1008306431147).

<a id="ref-r8"></a>**R8.** J. Snoek, H. Larochelle and R. P. Adams,
“Practical Bayesian Optimization of Machine Learning Algorithms,” *Advances in
Neural Information Processing Systems 25*, 2012.
[Official paper](https://proceedings.neurips.cc/paper/2012/hash/05311655a15b75fab86956663e1819cd-Abstract.html).

<a id="ref-r9"></a>**R9.** B. Letham, B. Karrer, G. Ottoni and E. Bakshy,
“Constrained Bayesian Optimization with Noisy Experiments,” *Bayesian
Analysis*, 14(2), 495–519, 2019.
[doi:10.1214/18-BA1110](https://doi.org/10.1214/18-BA1110).

<a id="ref-r10"></a>**R10.** C. K. I. Williams and C. E. Rasmussen,
“Gaussian Processes for Regression,” *Advances in Neural Information Processing
Systems 8*, 514–520, 1996.
[Official paper](https://proceedings.neurips.cc/paper_files/paper/1995/hash/7cce53cf90577442771720a370c3c723-Abstract.html).

<a id="ref-r11"></a>**R11.** M. S. Handcock and M. L. Stein, “A Bayesian
Analysis of Kriging,” *Technometrics*, 35(4), 403–410, 1993.
[doi:10.1080/00401706.1993.10485354](https://doi.org/10.1080/00401706.1993.10485354).

<a id="ref-r12"></a>**R12.** ISO 8178-1:2020, *Reciprocating internal
combustion engines — Exhaust emission measurement — Part 1: Test-bed
measurement systems of gaseous and particulate emissions*, fourth edition,
2020. [Official standard record](https://www.iso.org/standard/79330.html).

These sources support the named general methods and reporting concepts. The
code links throughout the manual remain the authority for the exact equations,
thresholds and workflow implemented by this program.

## 13. Verification map

Numerical and persistence coverage is in
[`tests/test_bayesian.py`](../tests/test_bayesian.py). It includes legacy-file
loading, five-dimensional proposals, measured power/split reconstruction,
candidate-pool validation, CSV export and synthetic optimisation. Qt/controller
coverage is in
[`tests/test_qt_optimiser.py`](../tests/test_qt_optimiser.py), including dynamic
dialog configuration, field-only target loading and an optimised-power
measurement round trip.

Mapping acquisition, pressure-unit scaling and separate model predictions are
covered by [`tests/test_pressure_mapping.py`](../tests/test_pressure_mapping.py).
Pressure metrics and payload checks are covered by
[`tests/test_pressure.py`](../tests/test_pressure.py). TDMS timing, calibration,
file completeness and ambiguous-file handling are covered by
[`tests/test_tdms_capture.py`](../tests/test_tdms_capture.py). Trigger arming,
delayed collection, saved NO windows, pressure attachment and recovery are
covered by [`tests/test_pressure_integration.py`](../tests/test_pressure_integration.py).
The source dialog, map controls and background-worker lifetime are covered by
[`tests/test_qt_pressure_mapping.py`](../tests/test_qt_pressure_mapping.py).

The campaign plan and convergence readout are covered by
[`tests/test_campaign_plan.py`](../tests/test_campaign_plan.py). It checks that
all four Minimise NO checks passing gives **Converged** and that each failing
alone does not, with values exactly at a threshold passing; that a `null` lower
bound passes; that older suggestions, initial-design tests and too few
suggestions are not assessed; the pooled replicate noise and the fallback to the
fitted noise; each warning; the two mapping checks; and the settings
validation. It runs the real `suggest()` for three- and five-variable
campaigns to check that the new diagnostics are present, deterministic and
JSON-safe, and that the best point is a tested condition. It also covers plan
creation, defaults, reload and rejection of malformed plans, plan extension and
its record, the controller's refusal to suggest once the plan is reached, the
plan line and notes in the window, and the creation-dialog fields and their
defaults.

These tests use synthetic records and hardware-free sessions. They do not
replace checks against the actual LabVIEW channel, calibration, clocks,
analyser and burner installation.
