# `models/` — astrostack

Reconstruction pipelines for astro-photos: many heterogeneous photographs of
the same patch of sky in, one better image out, with the provenance and the
audit that make "better" a claim you can check.

Everything here follows the roadmap fixed by
[`docs/research/multi-image-astro-reconstruction.md`](../docs/research/multi-image-astro-reconstruction.md).
Read section 5 of that document before writing any product copy that touches
this code; the short version is at the bottom of this file under
[Physical limits](#physical-limits-what-this-cannot-do).

---

## Quick start

```bash
/usr/bin/python3.12 -m venv models/.venv
models/.venv/bin/pip install -U pip
models/.venv/bin/pip install -e "models[dev]"        # no torch, on purpose

models/.venv/bin/ruff check models
models/.venv/bin/pytest models/tests -q
models/.venv/bin/python -m astrostack.cli --help
```

Running a pipeline over a directory of images, or over a `manifest.json`
exported by the backend:

```bash
models/.venv/bin/python -m astrostack.cli run \
    models/configs/classical-stack-v1.yaml \
    --inputs /path/to/frames \
    --out /tmp/m42
```

No data to hand? The test suite's generator will make you a corpus with known
truth — there are no binary fixtures in this repository, everything is
generated from a seed:

```bash
cd models && ../models/.venv/bin/python -c "
from tests.synthetic import make_corpus, write_corpus
field = make_corpus(n_frames=12, shape=(256, 256), n_stars=40, seed=1,
                    dither_pixels=1.5, sky_gradient=0.3, n_cosmic_rays=6,
                    trail_frames=(3,))
print(write_corpus(field, '/tmp/demo-frames')[1])
"
models/.venv/bin/python -m astrostack.cli run \
    models/configs/classical-stack-v1.yaml \
    --inputs /tmp/demo-frames/manifest.json --out /tmp/demo-out
```

The output directory contains:

| file | what it is |
| --- | --- |
| `coadd.fits` | linear **float32** FITS: `SCI` + `WEIGHT` + `UNCERT` + `PSF`, with a full WCS |
| `preview.png` | asinh-stretched 8-bit preview, for humans only |
| `provenance.json` | every input id and checksum, effective weights, params, git sha, per-stage record |
| `ATTRIBUTION.md` | every contributor, their licence, and their effective weight |

Two other commands:

```bash
astrostack inspect frame.fits     # metadata, astrometry, measured quality
astrostack metrics a.fits b.fits  # PSNR, SSIM, flux consistency, FWHM
astrostack ops                    # the stage vocabulary a config may use
astrostack validate config.yaml   # type-check a config and print its order
```

**No AWS and no GPU are required anywhere.** The GPU accelerates Tier B; it
does not enable anything.

---

## What is implemented, and what is not

### Tier A — classical stacking. **Complete and executable.**

| module | what it does | status |
| --- | --- | --- |
| `io/` | RAW (CFA-split, no demosaic), FITS, TIFF, JPEG with tone-curve inversion; `Frame` with variance, saturation mask, WCS, full metadata | done |
| `calibrate/` | bias/dark/flat, L.A.Cosmic cosmic rays, `Background2D` sky model | done |
| `align/` | plate solving (astrometry.net / ASTAP / NoOp), `sep` detection, analytic-Moffat or photutils-ePSF characterisation, field-mapped FWHM/ellipticity, WCS reprojection with astroalign cross-validation | done |
| `stack/` | mean/median/sigma-clip/winsorised baseline, drizzle, **Zackay & Ofek optimal coaddition**, trail and cosmic-ray rejection | done |
| `enhance/` | bounded Richardson-Lucy and Wiener with a measured PSF; HDR compositing | done |
| `metrics/` | FWHM, SNR gain, flux conservation, PSNR/SSIM, flux consistency, **synthetic-source injection and recovery curves** | done |
| `pipelines/` | declarative YAML graph, deterministic runner, `provenance.json`, `ATTRIBUTION.md` | done |

Not yet built at Tier A: photometric calibration against **Gaia DR3 synthetic
photometry** (section 6 of the research note). `FrameQuality.zero_point`,
`color_term` and `extinction_coeff` exist in the schema and flow through the
pipeline, but nothing fills them yet, so `transparency` defaults to 1.0 for
every frame. Until that lands, the Zackay-Ofek weights are driven by variance
alone and cross-camera colour terms are uncorrected. Also absent: the ZOGY
difference-imaging layer, and multi-scale ("feathering") fusion of very
different focal lengths.

### Tier B — learned multi-frame SR. **Scaffolding, untrained.**

`sr/` contains the real interfaces and the architecture, not placeholders:

* `wcs_burst.py` — an RBSR-style **recurrent** burst network (so `N` can vary
  between targets without retraining) whose alignment stage has been *deleted*
  and replaced by WCS-driven warping. `wcs_warp_stack()` works today, with no
  torch. Each frame's PSF, sigma map, zero point, background, airmass and
  photometric-reliability flag enter as explicit conditioning channels.
* `losses.py` — flux consistency (STAR/FISR), shape moments (ShapeNet), and
  data fidelity evaluated **through the forward model** against the original
  frames. NumPy reference implementations are unit-tested; the torch versions
  are built lazily.
* `uncertainty.py` — aleatoric/epistemic decomposition, confidence mask, and
  the prior-contribution measurement from the diffusion4astro protocol.
* `training/` — dataset construction from a deep Tier A coadd as pseudo-truth
  (with `allow_ai_training` as a hard gate), a training loop that logs every
  step, and an adversarial evaluation that can *fail* a model.

**No weights are shipped and none are trained.** `WCSBurstSR.enhance()`
refuses to run without a checkpoint rather than emitting a plausible-looking
image from random parameters. `configs/burst-sr-v1.yaml` therefore exits
non-zero out of the box; that is the intended behaviour.

### Tier C — a joint Bayesian sky model. **Designed, not built.**

The design, from section 9 of the research note, for when Tier B is in
production:

1. **One latent sky, many forward models.** Represent the sky as a continuous
   field — an implicit neural representation à la SuperF, or a
   multi-resolution basis on a tangent plane, HEALPix-tiled for whole-sky
   coverage. Give every contributed photograph its own explicit forward
   operator: WCS with distortion, spatially varying PSF (including field
   rotation and differential chromatic refraction), spectral response,
   throughput, background, sensor non-linearity and saturation.
2. **A Thresher-style objective at repository scale.** Maximise a proper
   likelihood summed over all images plus regularisers — structurally
   identical to eht-imaging's RML `Imager` (total variation, TSV, entropy,
   starlet L1), but in image space rather than visibility space. This is
   online multi-frame blind deconvolution: it gets lucky imaging's angular
   resolution *and* the SNR of using every frame, instead of choosing.
3. **Self-calibration.** Refine each image's nuisance parameters (PSF, gains,
   astrometry) jointly with the sky. This is the one EHT technique that
   genuinely transfers.
4. **Posterior sampling.** Add a score-based/diffusion prior and sample the
   posterior, following diffusion4astro's protocol of measuring *and
   publishing* how much structure came from the prior.
   `sr/uncertainty.prior_contribution()` already computes that number.
5. **4D.** A time-varying sky model over a decade of contributions gives
   variable-star light curves, proper motions, asteroid recovery and transient
   discovery, using a Zackay-Ofek proper coadd as the reference and ZOGY as
   the differencing operator. This, not prettier pictures, is where a photo
   repository produces publishable science.

The existing code is built so this is an addition rather than a rewrite: the
per-frame forward model already exists in pieces (`PSFModel`, the variance
map, the WCS, `sr/losses.forward_model_fidelity_*`), and `CoaddResult` already
carries the uncertainty map a posterior would populate.

---

## The interesting parts

### Zackay & Ofek coaddition (`stack/optimal.py`)

The reason this package exists rather than shelling out to a stacker. For
images of unequal quality the optimal combination is **not** a weighted
average: you must first matched-filter each image with **its own PSF**, then
sum with weights proportional to transparency over variance. The module
implements papers I and II (ApJ 836:187 and 836:188) with the equations in the
docstring and every departure from them declared.

Measured on synthetic corpora (`tests/test_optimal_coadd.py`), against a
sigma-clipped **inverse-variance-weighted** mean filtered with its own true
effective PSF — a real baseline, not a straw man:

| corpus | SNR gain over the baseline |
| --- | --- |
| identical frames | 1.00x (as theory requires — it *reduces* to the weighted mean) |
| seeing varying 3x, equal noise | ~1.15x |
| good seeing under bright sky vs bad seeing under dark sky | ~1.3x, i.e. ~0.3 mag |

The last row is the realistic case, and it is where a scalar inverse-variance
weight actively picks the *wrong* frames.

### Rejection that does not eat the data (`stack/reject.py`)

Three corrections that each cost real depth when missing, all found by tests
in this repository:

* the MAD of 8 frames underestimates sigma by 11% (Croux & Rousseeuw
  finite-sample correction), turning a nominal 3-sigma clip into 2.7 sigma;
* the residual `x - median(x)` is wider than the noise because the median is
  estimated from the same samples;
* **most importantly**, our frames have *different PSFs*, so a sharp frame's
  star core legitimately sits far above the stack median. A plain sigma clip
  reads that as an outlier and recovers only ~60% of a star's flux. The
  threshold therefore carries drizzlepac's `driz_cr` structure term.

### Drizzle (`stack/drizzle.py`)

Fruchter & Hook (2002), with the overlap integral evaluated by deterministic
quadrature rather than exact polygon clipping. Flux conservation is **exact at
any subsample count** (numerator and weight are deposited together); only the
drop's shape converges. The docstring says precisely what is approximate and
what is not.

### PSF characterisation defaults to analytic, not empirical

Measured end to end on a synthetic corpus, photutils' ePSF gave an
injection-recovery slope of **0.875** and lost 0.29 dB against the baseline,
while an analytic Moffat gave **0.970** and reached parity. On sparse fields
`EPSFBuilder` has too few clean stars — and it succeeds on some frames and
falls back on others, so the coadd combines PSFs measured two different ways.
The pipeline warns when it detects that mixture. Use `psf_model: epsf` on
crowded fields, and check `psf_source` on every frame afterwards.

### The audit (`metrics/injection.py`)

Inject sources of known flux at known positions, each convolved with the
frame's own PSF, stack, and fit `recovered = slope * injected + intercept`. A
slope of 1 means the pipeline measures. A slope above 1 or a positive
intercept means it is manufacturing flux, which in astronomy is a false
discovery, not a cosmetic defect. `classical-stack-v1.yaml` runs this audit on
every job and writes the result into `provenance.json`.

The complement is `tests/test_empty_field.py`: pure noise in, nothing
detectable out — for the coadd, for drizzle *and* for the deconvolution.

### Reproducibility

Hard rule 3 of `CLAUDE.md`. Two runs of the same job produce byte-identical
FITS. What makes that true:

* inputs sorted by `photo_id`, never by filesystem order (float addition is
  not associative);
* stages executed in a deterministic topological order, ties broken by
  declaration index;
* no `random` and no legacy `np.random` global state — every stochastic step
  derives from the run seed and the stage name through BLAKE2b, because
  CPython's `hash()` is salted per process;
* no timestamp in any FITS header;
* `provenance.json` split into a checksummed **deterministic** block and a
  **volatile** one (timestamps, host, library versions, input paths).

`tests/test_reproducibility.py` asserts all of it, including that poisoning
the global RNG between two runs changes nothing.

---

## Writing a pipeline

A pipeline is a YAML graph of stages. `astrostack ops` lists the vocabulary.

```yaml
pipeline: my-pipeline
seed: 20240101
stages:
  - id: load
    op: io.load
    params: {channel: G}
  - id: solve
    op: align.platesolve
    needs: [load]
    params: {solver: astrometry.net}
  - id: coadd
    op: stack.optimal
    needs: [solve]
```

Stages receive their dependencies through type-directed accessors
(`inputs.frames`, `inputs.grid`, `inputs.coadd`), so ordering in `needs` does
not matter. Add a new op with `@register_op("namespace.verb")` in
`astrostack/pipelines/stages.py`.

Shipped configs:

* `classical-stack-v1.yaml` — the full Tier A pipeline, ending in Zackay-Ofek
  coaddition plus the injection audit.
* `drizzle-v1.yaml` — for undersampled wide-field contributions; may go up to
  2x finer, but *only* if the measured dither diversity earns it.
* `burst-sr-v1.yaml` — Tier A plus the optional, labelled Tier B layer.
  Requires `pip install 'astrostack[torch]'` and a trained checkpoint.

---

## Optional dependencies

The base install is CPU-only and covers all of Tier A. Everything else
degrades with an actionable message rather than an `ImportError` from three
frames down:

| package | needed for | without it |
| --- | --- | --- |
| `torch` (extra `[torch]`) | Tier B | `sr/` raises `MissingDependencyError` with the install command; Tier A unaffected |
| `rawpy` | camera RAW decode | other formats still load |
| `sep` | fast source extraction | falls back to `photutils` segmentation, slower, same measurements |
| `astroalign` | registration fallback when plate solving fails | WCS path still works |
| `solve-field` / `astap` binaries | blind plate solving | use `solver: noop` on already-solved FITS |

`tests/test_sr_scaffolding.py` starts a *fresh interpreter* to assert that
importing `astrostack` never pulls in torch — an in-process check would be
fooled by an earlier import.

---

## Physical limits: what this cannot do

Summarising section 5 of the research note. These are not conservative
engineering estimates, they are physics, and any product copy that contradicts
them is a bug (hard rule 1 of `CLAUDE.md`).

**You cannot synthesise an aperture from a repository of photographs.** Not
with more photos, not with better software, not ever. VLBI achieves resolution
`lambda/B` because each station records the complex electric field — amplitude
*and phase* — against an atomic clock. A camera records intensity, `|E|^2`,
destroying phase at the moment of detection; it integrates over 10^12 to 10^17
wave periods; and at 550 nm one wave period is 1.8 femtoseconds. Two people
1000 km apart photographing Andromeda do not form a 1000 km telescope. Their
images combine **incoherently** — adding intensities, which improves
statistics, not adding fields, which would improve resolution.

*(The one honest exception is intensity interferometry, which needs only
nanosecond timing and has been demonstrated on Sirius with 0.25 m amateur
telescopes — but it requires single-photon detectors, GPS-disciplined
time-taggers and simultaneous observation. It is a hardware programme, and it
cannot be applied retroactively to stored photographs.)*

**You cannot resolve past the diffraction limit of the best contributing
optic.** The optical transfer function is exactly zero beyond `lambda/D`, so
anything reconstructed there is prior, not measurement.
`FrameMetadata.diffraction_limit_arcsec()` computes the wall and
`enhance/deconv.py` flags any output that crosses it as `prior_dominated`.

**There is no parallax or 3D depth outside the solar system.** Earth's
diameter gives about a degree of parallax on the Moon and arcminutes on
near-Earth asteroids. Beyond the solar system it is identically zero.

**No unflagged generative output.** Every learned result carries a per-pixel
uncertainty map, a prior-contribution number, and the visible label from
`SRResult.label`. There is no code path that produces a Tier B image without
one.

### What you *do* get, and it is a lot

| gain | achievable | how much |
| --- | --- | --- |
| depth / SNR | **yes, reliably** | `sqrt(sum t_i * throughput_i)`; hundreds of contributions go several magnitudes deeper than any single frame |
| recovering aliased detail (sub-pixel sampling) | **yes** | the real super-resolution win. Consumer wide-field setups are badly undersampled (a 50 mm lens on 4 um pixels gives ~16 arcsec/pixel against a ~2 arcsec optical limit), so drizzle/MFSR can deliver 1.5-3x effective linear sampling given genuine sub-pixel diversity — which independent observers supply for free |
| deconvolution sharpening | **partially** | ~1.5-2x FWHM reduction at high SNR with a well-measured PSF; prior-dependent beyond that, and flagged |
| "lucky" seeing selection | **yes** | a large corpus has a superb tail, and per-frame-PSF weighted fusion beats both naive averaging and hard frame selection |
| dynamic range / HDR | **yes** | unsaturated cores plus deep outskirts, from the exposure spread |
| time domain | **yes, and it is the most valuable output** | variables, transients, asteroids, proper motion — via ZOGY differencing against our own proper coadd |
| angular resolution beyond `lambda/D` | **no** | physically excluded |

The honest headline is: *several magnitudes deeper than any single
contribution, with recovered detail that no individual frame contains, and
calibrated photometry.* That is true, verifiable, and already a strong
product.

---

## Layout

```
models/
  astrostack/
    io/          ingest, linearisation, tone curves, manifests, FITS output
    calibrate/   masters, cosmic rays, spatially varying sky
    align/       plate solving, source/PSF characterisation, reprojection
    stack/       simple baseline, drizzle, Zackay-Ofek, rejection
    enhance/     bounded deconvolution, HDR
    metrics/     quality, comparison, injection audit
    sr/          Tier B interfaces, WCS-warped burst SR, scientific losses
    pipelines/   declarative graph, runner, provenance
    cli.py
  configs/       classical-stack-v1, drizzle-v1, burst-sr-v1
  training/      dataset snapshots, training loop, adversarial evaluation
  tests/         synthetic generator + the physics tests
  Dockerfile     CPU by default; --target gpu adds torch/CUDA
```

## Key references

* Fruchter & Hook 2002, *Drizzle*, PASP 114:144
* Zackay & Ofek 2017, *How to COAAD Images I & II*, ApJ 836:187 and 836:188
* Zackay, Ofek & Gal-Yam 2016, *Proper Image Subtraction (ZOGY)*, ApJ 830:27
* van Dokkum 2001, *Cosmic-Ray Rejection by Laplacian Edge Detection*, PASP 113:1420
* Lang et al. 2010, *Astrometry.net*, AJ 139:1782
* Hitchcock et al. 2022, *The Thresher*, MNRAS 511:5372
* Nammour et al. 2022, *ShapeNet*, A&A 663:A69
* Wu et al. 2025, *STAR*, NeurIPS D&B
* Wu et al. 2023, *RBSR*, PRCV

Full bibliography in
[`docs/research/multi-image-astro-reconstruction.md`](../docs/research/multi-image-astro-reconstruction.md).
