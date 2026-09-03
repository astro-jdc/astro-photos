"""The declarative graph, the runner, provenance and attribution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from astrostack.errors import PipelineConfigError
from astrostack.pipelines.graph import PipelineConfig, StageSpec, load_pipeline, topological_order
from astrostack.pipelines.provenance import (
    ProvenanceRecorder,
    StageRecord,
    write_attribution,
)
from astrostack.pipelines.runner import run_pipeline
from astrostack.pipelines.stages import OPS, StageInputs
from tests.synthetic import make_corpus, write_corpus

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
def test_every_shipped_config_uses_known_ops():
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = load_pipeline(path)
        for stage in cfg.stages:
            assert stage.op in OPS, f"{path.name}: unknown op {stage.op!r}"


def test_unknown_dependency_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pipeline": "x",
                "stages": [{"id": "a", "op": "io.load", "needs": ["ghost"]}],
            }
        )
    )
    with pytest.raises(PipelineConfigError, match="unknown stages"):
        load_pipeline(path)


def test_duplicate_stage_ids_are_rejected(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pipeline": "x",
                "stages": [{"id": "a", "op": "io.load"}, {"id": "a", "op": "io.load"}],
            }
        )
    )
    with pytest.raises(PipelineConfigError, match="duplicate"):
        load_pipeline(path)


def test_unknown_key_is_rejected(tmp_path):
    """``extra: forbid`` — a typo in a config must fail loudly, not silently."""
    path = tmp_path / "typo.yaml"
    path.write_text(
        yaml.safe_dump(
            {"pipeline": "x", "stages": [{"id": "a", "op": "io.load", "paramss": {}}]}
        )
    )
    with pytest.raises(PipelineConfigError):
        load_pipeline(path)


def test_cycles_are_detected():
    stages = [
        StageSpec(id="a", op="io.load", needs=["b"]),
        StageSpec(id="b", op="io.load", needs=["a"]),
    ]
    with pytest.raises(PipelineConfigError, match="cycle"):
        topological_order(stages)


def test_disabled_stages_are_pruned():
    cfg = PipelineConfig(
        pipeline="x",
        stages=[
            StageSpec(id="a", op="io.load"),
            StageSpec(id="b", op="io.load", needs=["a"], enabled=False),
            StageSpec(id="c", op="io.load", needs=["a", "b"]),
        ],
    )
    active = cfg.active_stages()
    assert [s.id for s in active] == ["a", "c"]
    assert active[1].needs == ["a"]


def test_defaults_are_merged_under_stage_params():
    cfg = PipelineConfig(
        pipeline="x",
        defaults={"channel": "G", "threshold_sigma": 5.0},
        stages=[StageSpec(id="a", op="io.load", params={"channel": "R"})],
    )
    merged = cfg.effective_params(cfg.stages[0])
    assert merged == {"channel": "R", "threshold_sigma": 5.0}


def test_stage_inputs_are_type_directed():
    import numpy as np

    from astrostack.align.platesolve import make_tangent_wcs
    from astrostack.align.register import OutputGrid
    from astrostack.io.frame import Frame, FrameMetadata
    from astrostack.stack.base import CoaddResult

    frame = Frame(
        frame_id="f", data=np.zeros((8, 8), dtype=np.float32), meta=FrameMetadata(photo_id="f")
    )
    grid = OutputGrid(
        wcs=make_tangent_wcs(0.0, 0.0, 1.0, (8, 8)), shape=(8, 8),
        pixel_scale_arcsec=1.0, oversample=1.0, dither_score=0.0,
    )  # fmt: skip
    coadd = CoaddResult(
        image=np.zeros((8, 8), dtype=np.float32), weight=np.ones((8, 8), dtype=np.float32),
        method="test", n_frames=1,
    )  # fmt: skip

    inputs = StageInputs({"frames": [frame], "grid": grid, "coadd": coadd})
    assert inputs.frames[0] is frame
    assert inputs.grid is grid
    assert inputs.coadd is coadd
    assert StageInputs({}).optional_coadd() is None
    with pytest.raises(PipelineConfigError, match="a list of frames"):
        _ = StageInputs({"grid": grid}).frames


# --------------------------------------------------------------------------
# Runner behaviour
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipe")
    field = make_corpus(
        n_frames=5, shape=(96, 96), n_stars=14, seed=606,
        fwhm_pixels=3.0, sky_level=200.0, dither_pixels=1.0,
    )  # fmt: skip
    _, manifest = write_corpus(field, root / "inputs")
    return field, Path(manifest)


def test_every_stage_declares_its_flux_behaviour(corpus, tmp_path):
    """Rule 2: each stage says what it does to photometry, in the record."""
    _, manifest = corpus
    run_pipeline(CONFIG_DIR / "classical-stack-v1.yaml", manifest, tmp_path / "o")
    payload = json.loads((tmp_path / "o" / "provenance.json").read_text())
    stages = {s["stage_id"]: s for s in payload["deterministic"]["stages"]}

    for stage_id in ("background", "cosmic", "register", "coadd"):
        assert stages[stage_id]["flux_preserving"] is True, stage_id
    assert all(s["op"] in OPS for s in stages.values())


def test_disabled_stage_is_skipped_by_the_runner(corpus, tmp_path):
    _, manifest = corpus
    cfg = load_pipeline(CONFIG_DIR / "classical-stack-v1.yaml")
    stages = [s.model_copy(update={"enabled": False}) if s.id == "audit" else s for s in cfg.stages]
    trimmed = cfg.model_copy(update={"stages": stages})

    run = run_pipeline(trimmed, manifest, tmp_path / "o")
    assert "audit" not in run.results
    assert "injection_audit" not in run.provenance.metrics


def test_mixed_psf_measurement_is_warned_about(corpus, tmp_path):
    """`epsf` may succeed on some frames and fall back on others; say so."""
    _, manifest = corpus
    run = run_pipeline(
        CONFIG_DIR / "classical-stack-v1.yaml",
        manifest,
        tmp_path / "o",
        overrides={"characterise": {"psf_model": "epsf"}},
    )
    sources = {f.extra.get("psf_source") for f in run.results["characterise"]}
    if len(sources) > 1:
        assert any("inconsistently" in w for w in run.provenance.warnings), sources
    else:
        assert not any("inconsistently" in w for w in run.provenance.warnings)


def test_runner_records_rejected_inputs(corpus, tmp_path):
    """A dropped frame leaves a row with a reason, per reconstruction_inputs."""
    from astrostack.io.manifest import load_manifest

    _, manifest = corpus
    entries = json.loads(manifest.read_text())
    entries[0]["license"] = "CC-BY-ND-4.0"
    bad = manifest.parent / "with_nd.json"
    bad.write_text(json.dumps(entries))

    run = run_pipeline(CONFIG_DIR / "classical-stack-v1.yaml", bad, tmp_path / "o")
    rejected = run.provenance.rejected_inputs
    assert len(rejected) == 1
    assert "forbids derivative works" in rejected[0]["rejection_reason"]
    assert len(load_manifest(bad, strict_licenses=False)) == 4


def test_scratch_frames_can_bypass_disk(corpus, tmp_path):
    """``io.frames`` lets the training harness feed in-memory frames."""
    field, manifest = corpus
    cfg = load_pipeline(CONFIG_DIR / "classical-stack-v1.yaml")
    stages = [
        s.model_copy(update={"op": "io.frames", "params": {}}) if s.id == "load" else s
        for s in cfg.stages
    ]
    run = run_pipeline(
        cfg.model_copy(update={"stages": stages}),
        manifest,
        tmp_path / "o",
        scratch={"frames": field.frames},
    )
    assert run.coadd is not None
    assert run.coadd.n_frames == len(field.frames)


# --------------------------------------------------------------------------
# Provenance and attribution
# --------------------------------------------------------------------------
def test_provenance_records_weights_and_checksums():
    recorder = ProvenanceRecorder("p", {"a": 1}, seed=7)
    recorder.add_input("photo-1", None, "CC0-1.0", "Ada", data_sha256="abc")
    recorder.add_input("photo-0", None, "CC-BY-4.0", "Bob", data_sha256="def")
    recorder.set_weights({"photo-1": 0.6, "photo-0": 0.4})
    recorder.add_stage(StageRecord("s", "io.load", {}, [], 0.1, {"n": 2}, True))
    recorder.add_output("fits", "/tmp/out/coadd.fits", "sha")
    recorder.warn("something to know")

    det = recorder.deterministic_block()
    assert [row["photo_id"] for row in det["inputs"]] == ["photo-0", "photo-1"]
    assert det["effective_weights"] == {"photo-0": 0.4, "photo-1": 0.6}
    assert det["outputs"]["fits"]["path"] == "coadd.fits"
    assert det["warnings"] == ["something to know"]
    assert recorder.run_checksum() == recorder.run_checksum()

    # Paths live in the volatile block only.
    assert "input_locations" in recorder.volatile_block()


def test_attribution_is_written_even_for_cc0(tmp_path):
    """Rule 5 of the licence table: credits always, even when not required."""
    path = tmp_path / "ATTRIBUTION.md"
    write_attribution(
        path,
        "classical-stack-v1",
        [
            {"photo_id": "b", "author": "Bob", "license": "CC0-1.0"},
            {"photo_id": "a", "author": "Ada", "license": "CC0-1.0"},
        ],
        weights={"a": 0.7, "b": 0.3},
        output_license="CC0-1.0",
    )
    text = path.read_text()
    assert "Ada" in text and "Bob" in text
    assert "CC0-1.0" in text
    assert "0.700000" in text
    # Ordered by photo_id, so the file is stable between runs.
    assert text.index("`a`") < text.index("`b`")


def test_jsonable_hashes_large_arrays_instead_of_inlining_them():
    import numpy as np

    from astrostack.pipelines.provenance import _jsonable

    small = _jsonable(np.arange(4))
    assert small == [0, 1, 2, 3]

    big = _jsonable(np.arange(1000))
    assert big["__array__"] is True
    assert big["shape"] == [1000]
    assert len(big["sha256"]) == 64
