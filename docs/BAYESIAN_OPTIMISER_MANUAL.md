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
[`roles.py:10`](../flow_controller/domain/roles.py#L10), and the selected pilot fuel is
included in the Stage 1 and global live combustion balances in
[`session.py:1443`](../flow_controller/core/session.py#L1443). The optimiser deliberately
excludes pilot flow from its NH3/H2 search mixture: every assigned pilot line
must read zero before a measurement window can start. That check is implemented
by `_checked_readings()` in
[`optimiser_controller.py:187`](../flow_controller/core/optimiser_controller.py#L187).

The separation is visible in the controller:

- suggestion calculation runs in a background worker at
  [`optimiser_controller.py:17`](../flow_controller/core/optimiser_controller.py#L17);
- `ask()` requests and saves one proposal at
  [`optimiser_controller.py:103`](../flow_controller/core/optimiser_controller.py#L103);
- `prepare_targets()` fills fields without sending commands at
  [`optimiser_controller.py:171`](../flow_controller/core/optimiser_controller.py#L171);
- capture and completion require separate operator actions at
  [`optimiser_controller.py:216`](../flow_controller/core/optimiser_controller.py#L216)
  and [`optimiser_controller.py:298`](../flow_controller/core/optimiser_controller.py#L298).

## 2. Search variables

Every campaign searches these three base variables:

1. H2 volume fraction in the NH3/H2 fuel blend, $x_{H2}$;
2. stage-1 equivalence ratio, $\phi_1$;
3. overall equivalence ratio, $\phi_g$.

New campaigns may add either or both of:

4. total thermal input, $P$, in kW;
5. fraction of total fuel sent to stage 1, $s$.

The variable registry is at
[`bayesian.py:20`](../flow_controller/domain/bayesian.py#L20). Configuration,
dimensionality, bound validation and backward-compatible defaults are implemented
by `SearchConfig` at
[`bayesian.py:46`](../flow_controller/domain/bayesian.py#L46). Old experiment
files omit the two optimisation flags and therefore reopen as the original
three-variable campaigns.

The order of the saved `point` array is:

```text
[h2_fraction, phi_stage1, phi_overall, power_kw?, split_rich?]
```

The optional coordinates appear only when their corresponding configuration
flag is true. `SearchConfig.values()` validates and names this array at
[`bayesian.py:119`](../flow_controller/domain/bayesian.py#L119), and
`SearchConfig.request()` converts it into a flow-calculation request at
[`bayesian.py:131`](../flow_controller/domain/bayesian.py#L131).

### Creating a campaign

In **New Bayesian experiment**:

1. Enter the nominal/fixed power and stage-1 split.
2. Enter bounds for H2 fraction, stage-1 phi and overall phi.
3. Select **Optimise thermal input** and/or **Optimise stage-1 fuel split** only
   when those variables should be searched.
4. Enter a lower and upper bound for every selected optional variable. The
   nominal value must lie inside its selected bounds.
5. Choose the initial-design and candidate-pool sizes.
6. Enter the O2 reference and measurement-window duration.
7. Confirm the reporting basis and the independently checked operating region.

The dynamic dialog is implemented at
[`qt_optimiser.py:40`](../flow_controller/ui/qt_optimiser.py#L40), with conversion
from percentages to fractions at
[`qt_optimiser.py:119`](../flow_controller/ui/qt_optimiser.py#L119).

For $d$ variables, the program requires at least $d+1$ completed initial
points. This is only a validity floor, not a claim that $d+1$ tests fully
characterise the response. Higher-dimensional searches normally need more
initial measurements and a larger candidate pool.

## 3. Flow-target equations

The pure flow calculation starts at
[`rql.py:52`](../flow_controller/domain/rql.py#L52). Let $x_{H2}$ be the H2
volume fraction and $x_{NH3}=1-x_{H2}$. At the common standard-volume
condition, the blend density is

$$
\rho_{mix}=x_{NH3}\rho_{NH3}+x_{H2}\rho_{H2}.
$$

Volume fractions are converted to mass fractions because lower heating values
are expressed per unit mass:

$$
y_i=\frac{x_i\rho_i}{\rho_{mix}}, \qquad
LHV_{mix}=y_{NH3}LHV_{NH3}+y_{H2}LHV_{H2}.
$$

For requested thermal input $P$ in kW,

$$
\dot m=\frac{P}{1000LHV_{mix}}, \qquad
\dot V_{fuel}=\frac{\dot m}{\rho_{mix}}\,60\,1000
\quad\text{SLPM}.
$$

The stage fuel split gives

$$
\dot V_{fuel,1}=s\dot V_{fuel}, \qquad
\dot V_{fuel,2}=(1-s)\dot V_{fuel}.
$$

For an NH3/H2 stream, stoichiometric oxygen demand is based on

$$
4NH_3+3O_2\rightarrow2N_2+6H_2O,\qquad
2H_2+O_2\rightarrow2H_2O.
$$

The program calculates stoichiometric air for stage 1 and the total mixture,
then applies the two equivalence ratios:

$$
\dot V_{air,1}=\frac{\dot V_{air,st,1}}{\phi_1},\qquad
\dot V_{air,total}=\frac{\dot V_{air,st,total}}{\phi_g},\qquad
\dot V_{air,2}=\dot V_{air,total}-\dot V_{air,1}.
$$

The target calculator and the measured-power reconstruction now use the same
25 °C standard-volume densities. This makes requested power and power recovered
from the resulting fuel flows inverse calculations.

## 4. Measurement and objective

Each trial represents a steady operating condition, not a continuous sweep.
During a measurement window, every fresh flow reading must remain within the
larger of 3% of its target or 0.05 SLPM. The window requires at least three fresh
flow passes and the configured duration. The implementation is in
`MeasurementWindow` at
[`optimisation.py:320`](../flow_controller/core/optimisation.py#L320), with
per-pass tracking at
[`optimisation.py:330`](../flow_controller/core/optimisation.py#L330).

The arithmetic mean of each flow is converted back into observed H2 fraction,
stage-1 phi, overall phi and thermal power at
[`optimisation.py:225`](../flow_controller/core/optimisation.py#L225). Observed
stage-1 fuel split is

$$
s_{obs}=\frac{\dot V_{NH3,1}+\dot V_{H2,1}}
{\dot V_{NH3,1}+\dot V_{H2,1}+\dot V_{NH3,2}+\dot V_{H2,2}},
$$

implemented at
[`optimisation.py:238`](../flow_controller/core/optimisation.py#L238). The model
uses these observed values rather than assuming that requested values were
reached exactly. `SearchConfig.observed_vector()` assembles the model input at
[`bayesian.py:137`](../flow_controller/domain/bayesian.py#L137).

The objective is raw dry NO corrected to reference oxygen:

$$
NO_{corr}=NO_{raw}
\frac{20.9-O_{2,ref}}{20.9-O_{2,meas}}.
$$

This is implemented by `corrected_no()` at
[`bayesian.py:163`](../flow_controller/domain/bayesian.py#L163). If a manual NO
standard error is supplied, the same correction factor is applied to it. O2
uncertainty and systematic analyser/calibration bias are not propagated. The
objective is corrected NO concentration, not total NOx, NO2, NH3 slip, N2O,
combustion efficiency or emissions per unit energy.

Window validation recomputes the observed condition from saved mean flows and
checks all selected-variable bounds at
[`optimisation.py:248`](../flow_controller/core/optimisation.py#L248). This makes
the saved flow record the source of the model coordinates.

## 5. Initial space-filling design

With no previous recordings, `suggest()` creates a scrambled Sobol candidate
pool in $d$ dimensions. Every normalized candidate $u$ lies in the unit
hypercube and is converted to physical coordinates using

$$
x_j=l_j+u_j(h_j-l_j),
$$

where $l_j$ and $h_j$ are the lower and upper bounds for variable $j$.

Candidates that violate a current controller flow ceiling are removed. Already
tried points, including invalid trials, are also removed so that the optimiser
does not immediately offer them again. The first proposal is closest to the
centre of the normalized region. Later initial points maximise their minimum
Euclidean distance from every prior trial:

$$
u^*=\arg\max_u\min_{v\in T}\lVert u-v\rVert_2.
$$

Candidate generation starts at
[`bayesian.py:234`](../flow_controller/domain/bayesian.py#L234), and the maximin
initial-design selection is at
[`bayesian.py:255`](../flow_controller/domain/bayesian.py#L255). Invalid tests
occupy their attempted location but are never assigned a fabricated emissions
result and never enter the Gaussian-process fit.

The configured pool size is rounded upward to the next power of two for Sobol
generation. Once completed data exist, 128 extra candidates are sampled near
the best observed point. A denser pool provides better numerical coverage but
does not add experimental information.

## 6. Gaussian-process surrogate

After the configured number of completed initial tests, the program normalises
every selected input coordinate:

$$
u_{ij}=\frac{x_{ij}-l_j}{h_j-l_j}.
$$

Corrected-NO values are centred and scaled by their sample standard deviation
(with a minimum scale of 1 ppm). The surrogate is a constant-amplitude
Matérn-5/2 Gaussian process plus fitted white observation noise:

$$
k(u,u')=C\left(1+\sqrt5r+\frac{5r^2}{3}\right)e^{-\sqrt5r}
+\sigma_w^2\delta_{u,u'},
$$

with automatic relevance determination distance

$$
r^2=\sum_{j=1}^{d}\frac{(u_j-u'_j)^2}{\ell_j^2}.
$$

There is one fitted length scale $\ell_j$ per selected variable. A short
length scale permits rapid variation along that coordinate; a long fitted scale
indicates a smoother or weakly identified effect over the declared range.

For a supplied corrected-NO standard error $s_i$, the regressor receives

$$
\alpha_i=\left(\frac{s_i}{s_y}\right)^2+10^{-8},
$$

where $s_y$ is the output scale. The separate WhiteKernel fits residual noise
not supplied by per-test SEM values. Model construction and fitting are in the
Bayesian fit in `suggest()` beginning at
[`bayesian.py:263`](../flow_controller/domain/bayesian.py#L263).

## 7. Noisy expected improvement

Ordinary expected improvement treats the current best observation as exact.
This program instead integrates over uncertainty in the latent, noise-free
values at previously measured locations.

The latent posterior excludes the fitted WhiteKernel observation-noise term.
Its baseline/candidate covariance blocks are assembled by
`_latent_posterior()` at
[`bayesian.py:183`](../flow_controller/domain/bayesian.py#L183).

For each of 128 Monte Carlo draws, the program samples one plausible latent
baseline vector

$$
f_B^{(m)}\sim\mathcal N(\mu_B,K_{BB}).
$$

It conditions the candidate distribution on that fantasy. If the fantasy's
best latent baseline is $b^{(m)}=\min f_B^{(m)}$, candidate conditional mean is
$\mu_c^{(m)}$, and conditional standard deviation is $\sigma_c$, define

$$
\Delta^{(m)}=b^{(m)}-\mu_c^{(m)},\qquad
z^{(m)}=\frac{\Delta^{(m)}}{\sigma_c}.
$$

The conditional analytic expected improvement for minimisation is

$$
EI^{(m)}=\Delta^{(m)}\Phi(z^{(m)})+
\sigma_c\varphi(z^{(m)}),
$$

where $\Phi$ and $\varphi$ are the standard-normal CDF and PDF. The noisy
expected improvement is the Monte Carlo mean

$$
NEI=\frac1{128}\sum_{m=1}^{128}EI^{(m)}.
$$

The implementation is at
[`bayesian.py:198`](../flow_controller/domain/bayesian.py#L198). The feasible
candidate with the largest NEI becomes the next suggestion. The displayed
`predicted_no` is the latent posterior mean, `latent_sd` is its latent standard
deviation, and `expected_improvement` is NEI, all converted back to ppm.

## 8. Experiment records and export

An experiment is stored as `.fcbo.json`. New campaigns use schema version 2;
the loader also accepts legacy schema-1 campaigns. The file contains immutable campaign
configuration, ordered trials, requested coordinates, measurement-window mean
flows, reconstructed observed coordinates, raw analyser means, corrected NO,
uncertainty metadata and provenance. `Experiment` loads and validates the
record at [`optimisation.py:49`](../flow_controller/core/optimisation.py#L49).

Changes are written to a temporary file, flushed and atomically replaced, so a
failed write retains the previous complete snapshot. Only one trial may be
pending. A completed trial must have a valid saved window and internally
consistent NO correction.

CSV export starts at
[`optimisation.py:177`](../flow_controller/core/optimisation.py#L177). It writes
requested and observed power and split values separately. Operator notes that
begin like spreadsheet formulas are prefixed to prevent formula execution when
the CSV is opened.

## 9. Choosing the dimensionality

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

## 10. Interpretation and stopping

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

## 11. Verification map

Numerical and persistence coverage is in
[`tests/test_bayesian.py`](../tests/test_bayesian.py). It includes legacy-file
loading, five-dimensional proposals, measured power/split reconstruction,
candidate-pool validation, CSV export and synthetic optimisation. Qt/controller
coverage is in
[`tests/test_qt_optimiser.py`](../tests/test_qt_optimiser.py), including dynamic
dialog configuration, field-only target loading and an optimised-power
measurement round trip.
