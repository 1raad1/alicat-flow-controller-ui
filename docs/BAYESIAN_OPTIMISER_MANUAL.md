# Bayesian optimiser technical manual

This manual explains the Bayesian experiment workflow implemented by Flow
Controller v3. It covers the numerical techniques, equations, records, new
variable controls and the boundaries between calculation, operator approval and
actuation. Code links point to the implementation at the time this manual was
written; use the named symbol if later edits move a line.

## 1. Purpose and operating boundary

The optimiser searches for low dry NO, corrected to a fixed dry-O2 reference,
in a pilot-off NH3/H2 rich-quench-lean experiment. It proposes one operating
point at a time. A proposal does **not** send a controller command. The operator
must review the calculated flows, load them into the existing target fields,
apply them through the normal controls, wait for the rig and analyser to settle,
and explicitly start a measurement window.

Parameter bounds and controller MAX FLOW values are feasibility constraints.
They are not learned flame-stability limits and they do not establish that a
transition between two feasible endpoints is safe.

The staged rig may use NH3, H2, or CH4 for its single pilot assignment. Those
assignments are registered in
[`roles.py:12`](../flow_controller/domain/roles.py#L12), and the selected pilot fuel is
included in the Stage 1 and global live combustion balances in
[`session.py:1443`](../flow_controller/core/session.py#L1443). The optimiser deliberately
excludes pilot flow from its NH3/H2 search mixture: every assigned pilot line
must read zero before a measurement window can start. That check is implemented
by `_checked_readings()` in
[`optimiser_controller.py:195`](../flow_controller/core/optimiser_controller.py#L195).

The separation is visible in the controller:

- suggestion calculation runs in a background worker at
  [`optimiser_controller.py:19`](../flow_controller/core/optimiser_controller.py#L19);
- `ask()` requests and saves one proposal at
  [`optimiser_controller.py:111`](../flow_controller/core/optimiser_controller.py#L111);
- `prepare_targets()` fills fields without sending commands at
  [`optimiser_controller.py:179`](../flow_controller/core/optimiser_controller.py#L179);
- capture and completion require separate operator actions at
  [`optimiser_controller.py:224`](../flow_controller/core/optimiser_controller.py#L224)
  and [`optimiser_controller.py:358`](../flow_controller/core/optimiser_controller.py#L358).

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
[`bayesian.py:21`](../flow_controller/domain/bayesian.py#L21). Configuration,
dimensionality, bound validation and backward-compatible defaults are implemented
by `SearchConfig` at
[`bayesian.py:47`](../flow_controller/domain/bayesian.py#L47). Old experiment
files omit the two optimisation flags and therefore reopen as the original
three-variable campaigns.

The order of the saved `point` array is:

```text
[h2_fraction, phi_stage1, phi_overall, power_kw?, split_rich?]
```

The optional coordinates appear only when their corresponding configuration
flag is true. `SearchConfig.values()` validates and names this array at
[`bayesian.py:120`](../flow_controller/domain/bayesian.py#L120), and
`SearchConfig.request()` converts it into a flow-calculation request at
[`bayesian.py:132`](../flow_controller/domain/bayesian.py#L132).

### Creating a campaign

In **New Bayesian experiment**:

1. Enter the nominal/fixed power and stage-1 split.
2. Enter bounds for H2 fraction, stage-1 phi and overall phi.
3. Select **Optimise thermal input** and/or **Optimise stage-1 fuel split** only
   when those variables should be searched.
4. Enter a lower and upper bound for every selected optional variable. The
   nominal value must lie inside its selected bounds.
5. Choose the initial-design and candidate-pool sizes. The **initial design**
   is the set of measurements selected for coverage before the statistical
   model chooses points. A **candidate pool** is the finite set of possible
   points scored during one call to the optimiser; it is a numerical search
   set, not experimental data.
6. Enter the O2 reference and measurement-window duration.
7. Confirm the reporting basis and the independently checked operating region.

The dynamic dialog is implemented at
[`qt_optimiser.py:41`](../flow_controller/ui/qt_optimiser.py#L41), with conversion
from percentages to fractions at
[`qt_optimiser.py:126`](../flow_controller/ui/qt_optimiser.py#L126).

Here $d$ is the number of selected search variables, so it is 3, 4, or 5. The
program requires at least $d+1$ completed initial points. This validation rule
is implemented at
[`bayesian.py:72`](../flow_controller/domain/bayesian.py#L72) and
[`bayesian.py:75`](../flow_controller/domain/bayesian.py#L75). It is only a
validity floor, not a claim that $d+1$ tests fully
characterise the response. Higher-dimensional searches normally need more
initial measurements and a larger candidate pool.

## 3. Flow-target equations

The pure flow calculation starts at
[`rql.py:52`](../flow_controller/domain/rql.py#L52). Every SLPM value in these
equations uses Alicat's default standard conditions: 25 °C and 14.696 psia.
Those constants are defined at
[`gas_properties.py:11`](../flow_controller/domain/gas_properties.py#L11) to
[`gas_properties.py:13`](../flow_controller/domain/gas_properties.py#L13),
following Alicat's [default STP
definition](https://www.alicat.com/support/what-is-mass-flow/).

Let $x_{NH3}$ be the NH3
volume fraction, dimensionless. Since the fuel contains only NH3 and H2,

$$
x_{NH3}=1-x_{H2}.
$$

Implemented at [`rql.py:65`](../flow_controller/domain/rql.py#L65) and
[`rql.py:66`](../flow_controller/domain/rql.py#L66).

Let $\rho_i$ be the density of gas $i$ at 25 °C and 14.696 psia, in
kg m$^{-3}$; the subscript `mix` denotes the NH3/H2 blend. The exact
[Alicat Gas Select 5.0
values](https://documents.alicat.com/specifications/Alicat_Preloaded-Gases-and-Properties_Rev0.pdf)
used by the program are $\rho_{NH3}=0.70352$, $\rho_{H2}=0.08235$,
$\rho_{CH4}=0.65688$, and $\rho_{Air}=1.18402$ kg m$^{-3}$. They are stored at
[`gas_properties.py:30`](../flow_controller/domain/gas_properties.py#L30) to
[`gas_properties.py:36`](../flow_controller/domain/gas_properties.py#L36).
The blend density is

$$
\rho_{mix}=x_{NH3}\rho_{NH3}+x_{H2}\rho_{H2}.
$$

Implemented at [`rql.py:71`](../flow_controller/domain/rql.py#L71), using the
shared density mapping exposed to combustion calculations at
[`combustion.py:68`](../flow_controller/domain/combustion.py#L68) and
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

implemented at [`rql.py:72`](../flow_controller/domain/rql.py#L72) and
[`rql.py:73`](../flow_controller/domain/rql.py#L73), followed by

$$
LHV_{mix}=y_{NH3}LHV_{NH3}+y_{H2}LHV_{H2}.
$$

Implemented at [`rql.py:74`](../flow_controller/domain/rql.py#L74).

Let $LHV_i^{(V)}$ be volumetric LHV in MJ m$^{-3}$ on the same Alicat standard
basis. It is derived, not copied from a table with a potentially different
reference volume:

$$
LHV_i^{(V)}=\rho_iLHV_i.
$$

Implemented at
[`gas_properties.py:38`](../flow_controller/domain/gas_properties.py#L38) to
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
[`rql.py:76`](../flow_controller/domain/rql.py#L76). The corresponding standard
volumetric flow is

$$
\dot V_{fuel}=\frac{\dot m}{\rho_{mix}}\,(60\ \mathrm{s\,min^{-1}})
(1000\ \mathrm{L\,m^{-3}}).
$$

Implemented at [`rql.py:77`](../flow_controller/domain/rql.py#L77).

The Stage 1 fuel split $s$ gives

$$
\dot V_{fuel,1}=s\dot V_{fuel}, \qquad
\dot V_{fuel,2}=(1-s)\dot V_{fuel}.
$$

Implemented at [`rql.py:78`](../flow_controller/domain/rql.py#L78) and
[`rql.py:79`](../flow_controller/domain/rql.py#L79). Each stage total is then
multiplied by $x_{NH3}$ or $x_{H2}$ to obtain its individual gas targets at
[`rql.py:81`](../flow_controller/domain/rql.py#L81),
[`rql.py:82`](../flow_controller/domain/rql.py#L82),
[`rql.py:83`](../flow_controller/domain/rql.py#L83), and
[`rql.py:84`](../flow_controller/domain/rql.py#L84).

**Stoichiometric** means exactly enough oxygen for complete combustion under
the reaction model, with neither fuel nor oxygen left over. For an NH3/H2
stream, the model uses

$$
4NH_3+3O_2\rightarrow2N_2+6H_2O,\qquad
2H_2+O_2\rightarrow2H_2O.
$$

These reactions give 0.75 mol O2 per mol NH3 and 0.50 mol O2 per mol H2,
encoded at [`combustion.py:40`](../flow_controller/domain/combustion.py#L40) to
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
[`gas_properties.py:15`](../flow_controller/domain/gas_properties.py#L15) to
[`gas_properties.py:19`](../flow_controller/domain/gas_properties.py#L19).
This residual represents argon, CO2, and other trace gases. N2 is retained as a
shared constant at [`combustion.py:51`](../flow_controller/domain/combustion.py#L51)
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
[`combustion.py:48`](../flow_controller/domain/combustion.py#L48) and
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
[`rql.py:86`](../flow_controller/domain/rql.py#L86) and
[`rql.py:87`](../flow_controller/domain/rql.py#L87); the three air-target
equations are at [`rql.py:88`](../flow_controller/domain/rql.py#L88),
[`rql.py:89`](../flow_controller/domain/rql.py#L89), and
[`rql.py:90`](../flow_controller/domain/rql.py#L90).

The target calculator and measured-power reconstruction use the same
25 °C, 14.696 psia densities. This makes requested power and power recovered
from the resulting fuel flows inverse calculations. If $\dot V_i$ is the
reported flow of fuel $i$ in SLPM, measured firing rate is

$$
P=\sum_i\frac{\dot V_iLHV_i^{(V)}}{60}.
$$

The matching per-SLPM factors are 0.218091 kW/SLPM for NH3, 0.164700 for H2,
and 0.547400 for CH4. Their calculation is implemented at
[`combustion.py:62`](../flow_controller/domain/combustion.py#L62) to
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
[`optimisation.py:636`](../flow_controller/core/optimisation.py#L636) to
[`optimisation.py:639`](../flow_controller/core/optimisation.py#L639). The
window requires at least three fresh flow passes and the configured duration;
those checks are at
[`optimisation.py:644`](../flow_controller/core/optimisation.py#L644) to
[`optimisation.py:647`](../flow_controller/core/optimisation.py#L647) and
[`optimisation.py:553`](../flow_controller/core/optimisation.py#L553) to
[`optimisation.py:555`](../flow_controller/core/optimisation.py#L555).

The arithmetic mean of each flow is converted back into observed H2 fraction,
stage-1 phi, overall phi and thermal power at
[`optimisation.py:517`](../flow_controller/core/optimisation.py#L517) to
[`optimisation.py:527`](../flow_controller/core/optimisation.py#L527). Here the
subscript `obs` means reconstructed from the averaged measured flows. In
particular,

$$
x_{H2,obs}=\frac{\dot V_{H2,1}+\dot V_{H2,2}}
{\dot V_{NH3,1}+\dot V_{NH3,2}+\dot V_{H2,1}+\dot V_{H2,2}}.
$$

Implemented at [`optimisation.py:520`](../flow_controller/core/optimisation.py#L520),
[`optimisation.py:521`](../flow_controller/core/optimisation.py#L521), and
[`optimisation.py:524`](../flow_controller/core/optimisation.py#L524). Observed
Stage 1 fuel split is

$$
s_{obs}=\frac{\dot V_{NH3,1}+\dot V_{H2,1}}
{\dot V_{NH3,1}+\dot V_{H2,1}+\dot V_{NH3,2}+\dot V_{H2,2}},
$$

implemented at [`optimisation.py:530`](../flow_controller/core/optimisation.py#L530)
to [`optimisation.py:537`](../flow_controller/core/optimisation.py#L537). The model
uses these observed values rather than assuming that requested values were
reached exactly. `SearchConfig.observed_vector()` assembles the model input at
[`bayesian.py:138`](../flow_controller/domain/bayesian.py#L138).

The **objective** is the scalar quantity the optimiser minimises. Here it is
raw dry NO corrected to a reference oxygen concentration. Let $NO_{raw}$ be
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
[`gas_properties.py:21`](../flow_controller/domain/gas_properties.py#L21) and
[`gas_properties.py:22`](../flow_controller/domain/gas_properties.py#L22), then
imported by the optimiser at
[`bayesian.py:16`](../flow_controller/domain/bayesian.py#L16) and
[`bayesian.py:20`](../flow_controller/domain/bayesian.py#L20). The correction
factor is calculated at
[`bayesian.py:177`](../flow_controller/domain/bayesian.py#L177), and applied at
[`bayesian.py:181`](../flow_controller/domain/bayesian.py#L181). If a manual NO
**standard error of the mean (SEM)** is supplied, it estimates the uncertainty
of the sample mean in ppm; the same correction factor is applied to it at
[`bayesian.py:178`](../flow_controller/domain/bayesian.py#L178) and
[`bayesian.py:181`](../flow_controller/domain/bayesian.py#L181). O2
uncertainty and systematic analyser/calibration bias are not propagated. The
objective is corrected NO concentration, not total NOx, NO2, NH3 slip, N2O,
combustion efficiency or emissions per unit energy.

Window validation recomputes the observed condition from saved mean flows and
checks all selected-variable bounds at
[`optimisation.py:569`](../flow_controller/core/optimisation.py#L569) to
[`optimisation.py:580`](../flow_controller/core/optimisation.py#L580). This makes
the saved flow record the source of the model coordinates.

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
[`qt_optimiser.py:283`](../flow_controller/ui/qt_optimiser.py#L283) to
[`qt_optimiser.py:291`](../flow_controller/ui/qt_optimiser.py#L291).

The streamed channel used by this test is **NO**, meaning nitric oxide in ppm.
It is not total NOx. The controller passes `packet["no_ppm"]` to the detector at
[`analyser_response_controller.py:267`](../flow_controller/core/analyser_response_controller.py#L267)
to [`analyser_response_controller.py:280`](../flow_controller/core/analyser_response_controller.py#L280),
and the saved record identifies the source as the validated live MEXA NO stream
at [`analyser_response_controller.py:344`](../flow_controller/core/analyser_response_controller.py#L344)
to [`analyser_response_controller.py:349`](../flow_controller/core/analyser_response_controller.py#L349).

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
[`qt_optimiser.py:295`](../flow_controller/ui/qt_optimiser.py#L295) to
[`qt_optimiser.py:333`](../flow_controller/ui/qt_optimiser.py#L333), and the
confirmation dialog is at
[`qt_optimiser.py:335`](../flow_controller/ui/qt_optimiser.py#L335) to
[`qt_optimiser.py:350`](../flow_controller/ui/qt_optimiser.py#L350).

A stored condition is a snapshot of settled target flows, measured flows,
controller assignments, capture time and, when available, the corresponding
flow-calculation request. Snapshot capture and tracking checks are implemented
at [`analyser_response_controller.py:106`](../flow_controller/core/analyser_response_controller.py#L106)
to [`analyser_response_controller.py:142`](../flow_controller/core/analyser_response_controller.py#L142).
Storing either endpoint clears selection of an earlier calibration because that
calibration belongs to the old A-to-B transition; persistence is implemented at
[`optimisation.py:148`](../flow_controller/core/optimisation.py#L148) to
[`optimisation.py:156`](../flow_controller/core/optimisation.py#L156).

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
to [`analyser_response_controller.py:264`](../flow_controller/core/analyser_response_controller.py#L264).
That path retains the controller assignment, command ceiling, zero lock and
configured ramp behaviour at
[`session.py:1392`](../flow_controller/core/session.py#L1392) to
[`session.py:1429`](../flow_controller/core/session.py#L1429). The final queue
also rechecks the ceiling and blocks nonzero commands while an operator or
watchdog zero lock is active at
[`session.py:1335`](../flow_controller/core/session.py#L1335) to
[`session.py:1360`](../flow_controller/core/session.py#L1360).

This is only a sharp step when the affected line is permitted to move directly.
If a ramp rate is configured, the software applies that ramp; if ramping is
declared off for a ramp-controlled role, the confirmation identifies a direct
step. The displayed policy is assembled at
[`analyser_response_controller.py:160`](../flow_controller/core/analyser_response_controller.py#L160)
to [`analyser_response_controller.py:181`](../flow_controller/core/analyser_response_controller.py#L181),
and configured ramps are executed at
[`session.py:2029`](../flow_controller/core/session.py#L2029) to
[`session.py:2062`](../flow_controller/core/session.py#L2062). A ramped test
therefore includes the commanded ramp duration and is not equivalent to an
instantaneous-input response test.

During the commanded transition, the program also enforces the frozen A-to-B
**transition envelope**. For role $i$, let $q_{i,A}$ and $q_{i,B}$ be the stored
A and B targets in SLPM and define

$$
\epsilon_i=\max\left(0.05\ \mathrm{SLPM},
0.03\max(|q_{i,A}|,|q_{i,B}|)\right).
$$

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
[`analyser_response_controller.py:300`](../flow_controller/core/analyser_response_controller.py#L300)
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

For $p\in\{0.1,0.5,0.9\}$, the crossing level is

$$
y_p=\bar y_A+p\Delta.
$$

The reported $t_{10}$, $t_{50}$ and $t_{90}$ are command-to-crossing times for
the first three-sample sustained crossings of $y_{0.1}$, $y_{0.5}$ and
$y_{0.9}$. The signed comparison works for rising and falling responses. The
10-to-90% rise time is

$$
t_{10\text{-}90}=t_{90}-t_{10}.
$$

Crossing detection is implemented at
[`analyser_response.py:432`](../flow_controller/domain/analyser_response.py#L432)
to [`analyser_response.py:440`](../flow_controller/domain/analyser_response.py#L440),
and elapsed and rise-time calculation is at
[`analyser_response.py:393`](../flow_controller/domain/analyser_response.py#L393)
to [`analyser_response.py:401`](../flow_controller/domain/analyser_response.py#L401).
Crossing times are quantised by the MEXA sampling interval.

The **stability onset**, `stable_window_start`, is the first sample of the
accepted final 20 s window. It is not the time at which the software can first
confirm stability. Confirmation becomes available only at `stable_window_end`,
after the complete window has elapsed and passed its tests. Both timestamps and
the command-to-stability value are stored at
[`analyser_response.py:415`](../flow_controller/domain/analyser_response.py#L415)
to [`analyser_response.py:424`](../flow_controller/domain/analyser_response.py#L424).

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
to [`analyser_response.py:424`](../flow_controller/domain/analyser_response.py#L424),
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

### Use in future optimiser windows

A successful run is selected for that campaign. For a future **live** MEXA
window, total observation time is

$$
t_{total}=t_{pre-delay}+t_{averaging},
$$

where $t_{pre-delay}$ is the selected response calibration's recommended delay
and $t_{averaging}$ is the campaign's configured averaging-window duration. The
averaging duration is not lengthened or replaced. These properties are calculated
at [`optimisation.py:199`](../flow_controller/core/optimisation.py#L199) to
[`optimisation.py:213`](../flow_controller/core/optimisation.py#L213).

When a live window starts, the controller waits through $t_{pre-delay}$ before
constructing `LiveWindow` and `MeasurementWindow`. It rechecks the MEXA link and
flow tracking throughout the wait, then starts the unchanged averaging window.
This sequence is implemented at
[`optimiser_controller.py:224`](../flow_controller/core/optimiser_controller.py#L224)
to [`optimiser_controller.py:260`](../flow_controller/core/optimiser_controller.py#L260)
and [`optimiser_controller.py:330`](../flow_controller/core/optimiser_controller.py#L330)
to [`optimiser_controller.py:350`](../flow_controller/core/optimiser_controller.py#L350).

Transient samples received during the delay remain in the receiver's JSONL and
CSV audit logs, whose write path is at
[`records.py:123`](../flow_controller/mexa/records.py#L123) to
[`records.py:151`](../flow_controller/mexa/records.py#L151). They are excluded
from the optimiser NO/O2 mean because `LiveWindow` is created only after the
delay. Its accepted samples and arithmetic channel means are implemented at
[`records.py:232`](../flow_controller/mexa/records.py#L232) to
[`records.py:290`](../flow_controller/mexa/records.py#L290). Manual-input windows
do not use the automatic delay because `start_window()` assigns zero delay unless
live capture is selected at
[`optimiser_controller.py:224`](../flow_controller/core/optimiser_controller.py#L224)
to [`optimiser_controller.py:247`](../flow_controller/core/optimiser_controller.py#L247).

The campaign JSON retains conditions A and B, all retained response runs, the
selected-run ID, criteria, timings, baseline/final statistics, source ID,
receiver-log provenance and accepted raw NO samples. Record construction and
persistence are at
[`analyser_response_controller.py:328`](../flow_controller/core/analyser_response_controller.py#L328)
to [`analyser_response_controller.py:366`](../flow_controller/core/analyser_response_controller.py#L366)
and [`optimisation.py:163`](../flow_controller/core/optimisation.py#L163) to
[`optimisation.py:204`](../flow_controller/core/optimisation.py#L204). Each live
trial window also stores `pre_window_delay_s`, `response_run_id` and
`total_observation_s` at
[`optimiser_controller.py:284`](../flow_controller/core/optimiser_controller.py#L284)
to [`optimiser_controller.py:300`](../flow_controller/core/optimiser_controller.py#L300).
CSV export includes those fields plus the MEXA source, sequence range, sample
count, channel SDs and receiver audit-log path at
[`optimisation.py:270`](../flow_controller/core/optimisation.py#L270) to
[`optimisation.py:319`](../flow_controller/core/optimisation.py#L319).

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
checks are at [`records.py:179`](../flow_controller/mexa/records.py#L179) to
[`records.py:199`](../flow_controller/mexa/records.py#L199), and the response
controller applies them at
[`analyser_response_controller.py:267`](../flow_controller/core/analyser_response_controller.py#L267)
to [`analyser_response_controller.py:298`](../flow_controller/core/analyser_response_controller.py#L298).

The baseline phase is limited to 120 s. This is the maximum time allowed to
obtain the stable 15 s baseline; it is not a measured response time. The limit
is declared at
[`analyser_response_controller.py:19`](../flow_controller/core/analyser_response_controller.py#L19)
and [`analyser_response_controller.py:20`](../flow_controller/core/analyser_response_controller.py#L20),
started with the baseline at
[`analyser_response_controller.py:218`](../flow_controller/core/analyser_response_controller.py#L218)
to [`analyser_response_controller.py:241`](../flow_controller/core/analyser_response_controller.py#L241),
and enforced at
[`analyser_response_controller.py:380`](../flow_controller/core/analyser_response_controller.py#L380)
to [`analyser_response_controller.py:393`](../flow_controller/core/analyser_response_controller.py#L393).

After B is commanded, the response phase has its own 900 s timeout. It is
declared at
[`analyser_response.py:35`](../flow_controller/domain/analyser_response.py#L35)
to [`analyser_response.py:37`](../flow_controller/domain/analyser_response.py#L37)
and enforced at
[`analyser_response.py:242`](../flow_controller/domain/analyser_response.py#L242)
to [`analyser_response.py:249`](../flow_controller/domain/analyser_response.py#L249).
Loss of valid MEXA data, fresh flow telemetry or flow tracking also discards the
run. Assignment, mode, connection, monitoring, communication, sequence, flow
ceiling, ramp-policy or zero-operation changes cancel it through the signal
guards at
[`analyser_response_controller.py:51`](../flow_controller/core/analyser_response_controller.py#L51)
to [`analyser_response_controller.py:60`](../flow_controller/core/analyser_response_controller.py#L60)
and [`analyser_response_controller.py:380`](../flow_controller/core/analyser_response_controller.py#L380)
to [`analyser_response_controller.py:401`](../flow_controller/core/analyser_response_controller.py#L401).

Cancellation stops and discards the measurement state. It deliberately sends no
automatic return-to-A command and does not zero the rig. This avoids introducing
an unreviewed recovery transition; it also means the operator remains responsible
for the physical condition after a partial or completed A-to-B move. The warning
is explicit in the start dialog at
[`qt_optimiser.py:341`](../flow_controller/ui/qt_optimiser.py#L341) to
[`qt_optimiser.py:350`](../flow_controller/ui/qt_optimiser.py#L350), and cancel
behaviour is implemented at
[`analyser_response_controller.py:403`](../flow_controller/core/analyser_response_controller.py#L403)
to [`analyser_response_controller.py:416`](../flow_controller/core/analyser_response_controller.py#L416).
The ordinary ramp, manual setpoint, zero and emergency controls remain available
for the established operator recovery procedure.

## 6. Initial space-filling design

With no previous recordings, `suggest()` creates a scrambled **Sobol sequence**:
a deterministic low-discrepancy sequence designed to cover a multi-dimensional
box more evenly than independent random draws. Scrambling randomises that
sequence while retaining its coverage properties. The first
$2^{\lceil\log_2 n\rceil}$ points form the candidate pool, where $n$ is the
requested pool size and $\lceil\cdot\rceil$ means round upward to an integer.
This rounding and generation are implemented at
[`bayesian.py:235`](../flow_controller/domain/bayesian.py#L235) and
[`bayesian.py:236`](../flow_controller/domain/bayesian.py#L236).

Let $u=(u_1,\ldots,u_d)$ be a normalized candidate, so each $u_j$ is a
dimensionless coordinate from 0 to 1. The **unit hypercube** is the set of all
such vectors. For variable $j$, let $l_j$ be its physical lower bound, $h_j$
its physical upper bound, and $x_j$ its physical value. Conversion from
normalized to physical coordinates is

$$
x_j=l_j+u_j(h_j-l_j),
$$

implemented by forming the lower-bound and span arrays at
[`bayesian.py:233`](../flow_controller/domain/bayesian.py#L233) and
[`bayesian.py:234`](../flow_controller/domain/bayesian.py#L234), then applying
the equation at [`bayesian.py:249`](../flow_controller/domain/bayesian.py#L249).

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
[`bayesian.py:258`](../flow_controller/domain/bayesian.py#L258), and the largest
minimum distance is selected at
[`bayesian.py:259`](../flow_controller/domain/bayesian.py#L259). For the very
first point, distance from the centre vector $(0.5,\ldots,0.5)$ is minimised at
[`bayesian.py:261`](../flow_controller/domain/bayesian.py#L261). Invalid tests
occupy their attempted location but are never assigned a fabricated emissions
result and never enter the Gaussian-process fit.

This space-filling rule is used while the number of completed tests is less
than the configured initial-design size. During that period the program does
not fit the Matérn GP or calculate NEI. The branch and returned method are
implemented at [`bayesian.py:256`](../flow_controller/domain/bayesian.py#L256)
to [`bayesian.py:262`](../flow_controller/domain/bayesian.py#L262). The initial
tests therefore establish coverage of the search space before the optimiser
has enough recordings to make model-based proposals.

The configured pool size is rounded upward to the next power of two for Sobol
generation. Once completed data exist, 128 extra candidates are sampled near
the best observed point. A denser pool provides better numerical coverage but
does not add experimental information.

## 7. Gaussian-process surrogate

A **Gaussian process (GP) surrogate** is a probability model over possible
response functions. It supplies a predicted corrected NO and uncertainty at
unmeasured inputs. After the configured number of completed initial tests, the
program normalises every selected input coordinate. If $x_{ij}$ is the physical
value of variable $j$ in completed test $i$, then

$$
u_{ij}=\frac{x_{ij}-l_j}{h_j-l_j}.
$$

Implemented at [`bayesian.py:264`](../flow_controller/domain/bayesian.py#L264)
and [`bayesian.py:265`](../flow_controller/domain/bayesian.py#L265).

Let $y_i$ be corrected NO for test $i$, in ppm; $\bar y$ its arithmetic mean;
and $s_y=\max(\operatorname{std}(y),1\ \mathrm{ppm})$ its output scale. The
model is fitted to the dimensionless standardized response

$$
\tilde y_i=\frac{y_i-\bar y}{s_y}.
$$

The mean and scale are calculated at
[`bayesian.py:266`](../flow_controller/domain/bayesian.py#L266) and
[`bayesian.py:267`](../flow_controller/domain/bayesian.py#L267), and the
standardization is applied during fitting at
[`bayesian.py:275`](../flow_controller/domain/bayesian.py#L275).

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
selected at [`bayesian.py:269`](../flow_controller/domain/bayesian.py#L269) and
[`bayesian.py:270`](../flow_controller/domain/bayesian.py#L270).

The distance uses **automatic relevance determination (ARD)**, meaning each
input dimension has its own fitted length scale:

$$
r^2=\sum_{j=1}^{d}\frac{(u_j-u'_j)^2}{\ell_j^2}.
$$

Here $\ell_j$ is the positive, dimensionless length scale for variable $j$.
There is one $\ell_j$ per selected variable because the code passes an array of
$d$ initial scales to `Matern` at
[`bayesian.py:269`](../flow_controller/domain/bayesian.py#L269) and
[`bayesian.py:270`](../flow_controller/domain/bayesian.py#L270). A short length
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
[`bayesian.py:188`](../flow_controller/domain/bayesian.py#L188) to
[`bayesian.py:191`](../flow_controller/domain/bayesian.py#L191).

The candidate's latent posterior variance is

$$
\sigma^2(u_*)=k(u_*,u_*)-v_*^T v_*,
$$

where $\sigma^2(u_*)$ is uncertainty about the underlying noise-free response;
$k(u_*,u_*)$ is the fitted signal-kernel variance before conditioning on the
data; $v_*$ is the triangular-solve vector produced from the training
covariance; and the superscript $T$ denotes vector transpose. The triangular
solve is at [`bayesian.py:190`](../flow_controller/domain/bayesian.py#L190), and the
candidate variances are calculated at
[`bayesian.py:195`](../flow_controller/domain/bayesian.py#L195). Thus the GP,
rather than the Matérn covariance alone, produces the two quantities used by
the acquisition calculation: predicted mean $\mu(u_*)$ and latent standard
deviation $\sigma(u_*)=\sqrt{\sigma^2(u_*)}$.

For a supplied corrected-NO SEM $s_i$ in ppm, the regressor receives a
dimensionless per-observation variance

$$
\alpha_i=\left(\frac{s_i}{s_y}\right)^2+10^{-8},
$$

where $\alpha_i$ is the diagonal variance added for observation $i$. The SEM is
divided by $s_y$ at [`bayesian.py:268`](../flow_controller/domain/bayesian.py#L268),
then squared and given the $10^{-8}$ numerical floor at
[`bayesian.py:271`](../flow_controller/domain/bayesian.py#L271). The separate
`WhiteKernel` fits residual noise not supplied by the per-test SEM values.

## 8. Noisy expected improvement

**Expected improvement (EI)** scores a candidate by the expected amount that
its objective will beat the current best value. Ordinary EI treats the best
observation as exact. **Noisy expected improvement (NEI)** instead averages EI
over uncertainty in the earlier measurements. A **latent value** is the GP's
inferred noise-free corrected NO at an input, rather than a noisy analyser
observation.

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
[`bayesian.py:187`](../flow_controller/domain/bayesian.py#L187). The posterior
mean and baseline/candidate covariance blocks are assembled at
[`bayesian.py:188`](../flow_controller/domain/bayesian.py#L188) to
[`bayesian.py:196`](../flow_controller/domain/bayesian.py#L196).

For numerical stability the implementation adds **jitter**, a very small
positive value $\epsilon$, to the covariance diagonal:

$$
\widetilde K_{BB}=K_{BB}+\epsilon I,\qquad
\epsilon=\max\left(10^{-10},10^{-8}\max_i(K_{BB})_{ii}\right),
$$

where $I$ is the identity matrix and $(K_{BB})_{ii}$ is baseline variance $i$.
The jitter and Cholesky factorization are implemented at
[`bayesian.py:204`](../flow_controller/domain/bayesian.py#L204) and
[`bayesian.py:205`](../flow_controller/domain/bayesian.py#L205).

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
[`bayesian.py:205`](../flow_controller/domain/bayesian.py#L205) to
[`bayesian.py:206`](../flow_controller/domain/bayesian.py#L206). The default
$M=128$ is declared at [`bayesian.py:199`](../flow_controller/domain/bayesian.py#L199).

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
[`bayesian.py:207`](../flow_controller/domain/bayesian.py#L207) and
[`bayesian.py:208`](../flow_controller/domain/bayesian.py#L208). The candidate's
conditional standard deviation is

$$
\sigma_c=\sqrt{K_{cc}-K_{cB}\widetilde K_{BB}^{-1}K_{Bc}},
$$

where $K_{cc}$ is the candidate latent variance and $K_{cB}=K_{Bc}^T$.
Implemented at [`bayesian.py:209`](../flow_controller/domain/bayesian.py#L209),
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
[`bayesian.py:210`](../flow_controller/domain/bayesian.py#L210) and
[`bayesian.py:211`](../flow_controller/domain/bayesian.py#L211). The conditional
analytic expected improvement for minimisation is

$$
EI^{(m)}=\Delta^{(m)}\Phi(z^{(m)})+
\sigma_c\varphi(z^{(m)}),
$$

where $\Phi$ is the standard-normal cumulative distribution function (CDF),
the probability that a standard-normal variable is no greater than its
argument, and $\varphi$ is the standard-normal probability density function
(PDF). The `ndtr` call implements $\Phi$ and the exponential term implements
$\varphi$ at [`bayesian.py:202`](../flow_controller/domain/bayesian.py#L202) and
[`bayesian.py:212`](../flow_controller/domain/bayesian.py#L212). The noisy
expected improvement is the Monte Carlo mean

$$
NEI=\frac1M\sum_{m=1}^{M}EI^{(m)},\qquad M=128.
$$

The mean and non-negative clipping are implemented at
[`bayesian.py:213`](../flow_controller/domain/bayesian.py#L213). The feasible
candidate with the largest NEI is selected:

$$
u_{\mathrm{next}}=\arg\max_{u\in\mathcal F}NEI(u),
$$

where $u_{\mathrm{next}}$ is the next normalized operating condition;
$\mathcal F$ is the finite set of feasible, untried candidates; $u$ is one
candidate in that set; and $\arg\max$ means the candidate at which NEI is
largest. This selection is implemented at
[`bayesian.py:276`](../flow_controller/domain/bayesian.py#L276) and
[`bayesian.py:277`](../flow_controller/domain/bayesian.py#L277). The displayed
`predicted_no` is the latent posterior mean, `latent_sd` ($\sigma$ in the UI
description) is its latent standard deviation, and `expected_improvement` is
NEI. All three are converted from the standardized model scale back to ppm at
[`bayesian.py:278`](../flow_controller/domain/bayesian.py#L278) to
[`bayesian.py:282`](../flow_controller/domain/bayesian.py#L282).

Consequently, the program does not select the largest predicted NO, the
largest Matérn-kernel value, the most distant condition, or necessarily the
lowest posterior-mean NO. After initialization, it selects the feasible
candidate with the largest NEI because that score represents the best current
balance between possible lower NO and uncertainty.

## 9. Experiment records and export

An experiment is stored as `.fcbo.json`. New campaigns use schema version 2;
the loader also accepts legacy schema-1 campaigns. The file contains immutable campaign
configuration, ordered trials, requested coordinates, measurement-window mean
flows, reconstructed observed coordinates, raw analyser means, corrected NO,
uncertainty metadata and provenance. `Experiment` loads and validates the
record at [`optimisation.py:55`](../flow_controller/core/optimisation.py#L55).

Changes are written to a temporary file, flushed and atomically replaced, so a
failed write retains the previous complete snapshot. Only one trial may be
pending. A completed trial must have a valid saved window and internally
consistent NO correction.

CSV export starts at
[`optimisation.py:270`](../flow_controller/core/optimisation.py#L270). It writes
requested and observed power and split values separately. Operator notes that
begin like spreadsheet formulas are prefixed to prevent formula execution when
the CSV is opened.

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

The default 16-point initial design remains available for three to five
variables. It is a starting allocation, not a statistical guarantee. Increase
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

The implementation has no automatic statistical stopping rule. The operator
decides whether the expected improvement, uncertainty, repeats and experimental
budget justify another trial.

## 12. Verification map

Numerical and persistence coverage is in
[`tests/test_bayesian.py`](../tests/test_bayesian.py). It includes legacy-file
loading, five-dimensional proposals, measured power/split reconstruction,
candidate-pool validation, CSV export and synthetic optimisation. Qt/controller
coverage is in
[`tests/test_qt_optimiser.py`](../tests/test_qt_optimiser.py), including dynamic
dialog configuration, field-only target loading and an optimised-power
measurement round trip.
