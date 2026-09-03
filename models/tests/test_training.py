"""Dataset construction and the consent gate.

The pair builder runs without torch — it is pure NumPy — so the consent rule
and the snapshot record are testable on any machine. The training loop itself
needs the ``[torch]`` extra and is skipped without it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from astrostack.sr._torch import available as torch_available
from astrostack.stack import optimal_coadd
from training.dataset import BurstSample, PairBuilder, build_snapshot, make_pairs_from_coadd

torch_only = pytest.mark.skipif(not torch_available(), reason="needs the [torch] extra")


def _opt_out(frames, ids):
    out = []
    for f in frames:
        g = f.copy_with(f.data)
        if f.frame_id in ids:
            g.meta = f.meta.model_copy(update={"allow_ai_training": False})
        out.append(g)
    return out


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------
def test_snapshot_excludes_opted_out_photos(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    doctored = _opt_out(frames, {"synthetic-000", "synthetic-003"})

    snapshot = build_snapshot(doctored, {"object": "M42"}, created_from="test")
    assert "synthetic-000" not in snapshot.photo_ids
    assert "synthetic-003" not in snapshot.photo_ids
    assert snapshot.excluded_opt_out == ["synthetic-000", "synthetic-003"]
    assert snapshot.photo_count == len(frames) - 2
    assert snapshot.filter_query == {"object": "M42"}


def test_snapshot_checksum_is_stable_and_sensitive(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    a = build_snapshot(frames, {"q": 1})
    b = build_snapshot(list(reversed(frames)), {"q": 1})
    c = build_snapshot(frames[:-1], {"q": 1})
    d = build_snapshot(frames, {"q": 2})

    assert a.checksum == b.checksum, "order must not change the snapshot identity"
    assert a.checksum != c.checksum
    assert a.checksum != d.checksum
    assert len(a.checksum) == 64


def test_snapshot_writes_the_dataset_snapshots_row(homogeneous_corpus, tmp_path):
    _, frames, _ = homogeneous_corpus
    snapshot = build_snapshot(frames, {"object": "M31"})
    snapshot.write(tmp_path / "snap.json")

    payload = json.loads((tmp_path / "snap.json").read_text())
    assert set(payload) == {
        "photo_ids", "filter_query", "checksum", "photo_count", "excluded_opt_out", "created_from",
    }  # fmt: skip
    assert payload["photo_count"] == len(payload["photo_ids"])


def test_pair_builder_refuses_when_consent_leaves_too_few_frames(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    doctored = _opt_out(frames, {f.frame_id for f in frames[:6]})
    coadd = optimal_coadd(frames)

    with pytest.raises(ValueError, match="consent is a hard gate"):
        PairBuilder(doctored, coadd, burst_size=5)


def test_pair_builder_drops_opted_out_frames_from_the_bursts(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    excluded = {"synthetic-000"}
    doctored = _opt_out(frames, excluded)
    coadd = optimal_coadd(frames)

    builder = PairBuilder(doctored, coadd, burst_size=3, seed=7)
    samples = list(builder.samples(12))
    assert samples
    for sample in samples:
        assert excluded.isdisjoint(sample.frame_ids)

    snapshot = builder.snapshot()
    assert excluded.isdisjoint(snapshot.photo_ids)
    assert snapshot.excluded_opt_out == sorted(excluded)


# --------------------------------------------------------------------------
# Pair construction
# --------------------------------------------------------------------------
def test_bursts_carry_the_physical_conditioning(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    coadd = optimal_coadd(frames)
    samples = list(make_pairs_from_coadd(frames, coadd, burst_size=4, n_samples=5, seed=3))

    assert len(samples) == 5
    for sample in samples:
        assert isinstance(sample, BurstSample)
        assert sample.lr.shape[0] == 4
        assert sample.valid.shape == sample.lr.shape
        assert sample.conditioning.shape == (4, 7, *sample.lr.shape[1:])
        assert sample.psfs.shape[0] == 4
        assert np.allclose(sample.psfs.sum(axis=(1, 2)), 1.0, atol=1e-4)
        assert sample.sigmas.shape == (4,)
        assert sample.hr.shape == coadd.image.shape
        assert sample.frame_ids == sorted(sample.frame_ids)
        assert sample.as_metadata()["n_frames"] == 4


def test_burst_selection_is_deterministic(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    coadd = optimal_coadd(frames)

    def ids(seed):
        return [s.frame_ids for s in make_pairs_from_coadd(frames, coadd, burst_size=3, n_samples=6, seed=seed)]

    assert ids(11) == ids(11)
    assert ids(11) != ids(12)


def test_upscaled_target_conserves_flux(homogeneous_corpus):
    """A x2 pseudo-HR target must hold the same total flux as the coadd."""
    _, frames, _ = homogeneous_corpus
    coadd = optimal_coadd(frames)
    sample = next(iter(make_pairs_from_coadd(frames, coadd, burst_size=3, n_samples=1, scale=2)))

    assert sample.hr.shape == (coadd.image.shape[0] * 2, coadd.image.shape[1] * 2)
    assert float(sample.hr.sum()) == pytest.approx(float(coadd.image.sum()), rel=1e-4)


def test_burst_size_larger_than_the_corpus_is_refused(homogeneous_corpus):
    _, frames, _ = homogeneous_corpus
    coadd = optimal_coadd(frames)
    with pytest.raises(ValueError, match="at least burst_size"):
        list(make_pairs_from_coadd(frames, coadd, burst_size=99, n_samples=1))


# --------------------------------------------------------------------------
# Training loop (torch)
# --------------------------------------------------------------------------
@torch_only
def test_seeding_records_whether_determinism_was_achieved():
    from training.train import set_all_seeds

    record = set_all_seeds(1234)
    assert record["seed"] == 1234
    assert "deterministic_algorithms" in record
    assert isinstance(record["note"], str)


@torch_only
def test_short_training_run_writes_the_whole_log(homogeneous_corpus, tmp_path):
    """``training_runs.log_s3_key`` must point at a complete, parseable log."""
    from training.train import Hyperparameters, train

    _, frames, _ = homogeneous_corpus
    coadd = optimal_coadd(frames)
    builder = PairBuilder(frames, coadd, burst_size=3, seed=5, scale=2)
    samples = list(builder.samples(4))

    hp = Hyperparameters(
        epochs=1, steps_per_epoch=2, batch_size=2, burst_size=3, scale=2,
        channels=8, n_blocks=2,
    )  # fmt: skip
    run = train(samples, builder.snapshot(), tmp_path, hyperparams=hp, device="cpu")

    assert run.status == "succeeded"
    lines = [json.loads(line) for line in (tmp_path / "train.jsonl").read_text().splitlines()]
    events = [row["event"] for row in lines]
    assert events[0] == "run_start"
    assert events[-1] == "run_end"
    assert events.count("step") == 2, "every step must be logged, not just the last"

    step = next(row for row in lines if row["event"] == "step")
    for key in ("loss", "nll", "l1", "flux", "shape", "fidelity", "grad_norm", "lr"):
        assert key in step

    assert (tmp_path / "training_run.json").is_file()
    assert (tmp_path / "dataset_snapshot.json").is_file()
    assert (tmp_path / "checkpoint.pt").is_file()

    record = json.loads((tmp_path / "training_run.json").read_text())
    assert record["git_sha"]
    assert record["hyperparams"]["seed"] == hp.seed
    assert record["dataset_snapshot"]["checksum"]
    assert record["hardware"]["hardware_tag"] == "cpu"
