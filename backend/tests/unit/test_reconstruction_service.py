"""Orquestación de reconstrucciones, con repositorios falsos (sin base de datos).

Se testea lo que ``docs/api.md`` y ``docs/licensing.md`` prometen:

* el preview no encola nada y sí explica qué bloquea el job,
* la creación aborta con 422 si alguna entrada no permite derivadas,
* la procedencia se escribe **antes** de encolar,
* la ``Idempotency-Key`` no crea un segundo job,
* los límites de ritmo devuelven 429.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import LicenseBlockedError, NotFoundError, RateLimitError
from app.domain.disclosure import ResultArtifacts
from app.domain.licensing import LicenseCode
from app.domain.selection import RejectionReason
from app.models.enums import JobStatus, PhotoStatus
from app.models.photo import Photo
from app.models.reconstruction import Reconstruction, ReconstructionInput
from app.schemas.reconstruction import ReconstructionCreateIn
from app.schemas.search import PhotoSearchQuery
from app.services.reconstruction import ReconstructionService

OBJECT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def make_photo(
    index: int,
    license_code: LicenseCode = LicenseCode.CC_BY,
    *,
    stacks: bool = True,
    quality: float = 0.8,
    dither: tuple[float, float] = (0.0, 0.0),
    scale: float = 2.0,
) -> Photo:
    photo = Photo(
        id=uuid.UUID(f"00000000-0000-0000-0000-{index:012d}"),
        owner_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        status=PhotoStatus.READY,
        s3_bucket="b",
        s3_key_original=f"k{index}",
        checksum_sha256=bytes([index]) * 32,
        license=license_code,
        allow_derivatives_in_stacks=stacks,
        object_id=OBJECT_ID,
        quality_score=quality,
        dither_phase_x=dither[0],
        dither_phase_y=dither[1],
        pixel_scale_arcsec=scale,
        aperture_mm=100.0,
        attribution_name=f"Autor {index}",
    )
    photo.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    photo.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return photo


class FakePhotoRepo:
    def __init__(self, photos: list[Photo]) -> None:
        self.photos = {p.id: p for p in photos}

    async def get_many(self, photo_ids: Any) -> list[Photo]:
        return [self.photos[pid] for pid in photo_ids if pid in self.photos]

    async def candidates_for_object(self, object_id: uuid.UUID, **kw: Any) -> list[Photo]:
        return [p for p in self.photos.values() if p.object_id == object_id]


class FakeReconstructionRepo:
    def __init__(self) -> None:
        self.jobs: list[Reconstruction] = []
        self.inputs: list[ReconstructionInput] = []
        self.active = 0
        self.today = 0
        self.by_key: dict[str, Reconstruction] = {}

    async def get_by_idempotency_key(self, user_id: uuid.UUID, key: str) -> Reconstruction | None:
        return self.by_key.get(key)

    async def count_active_for_user(self, user_id: uuid.UUID) -> int:
        return self.active

    async def count_last_24h(self, user_id: uuid.UUID) -> int:
        return self.today

    async def add(self, job: Reconstruction) -> Reconstruction:
        job.id = job.id or uuid.uuid4()
        job.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        job.updated_at = job.created_at
        self.jobs.append(job)
        if job.idempotency_key:
            self.by_key[job.idempotency_key] = job
        return job

    async def add_inputs(self, rows: list[ReconstructionInput]) -> None:
        self.inputs.extend(rows)


class FakeObjectRepo:
    async def resolve(self, token: str) -> Any:
        return None


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(
        self, queue: str, body: dict[str, Any], *, idempotency_key: str | None = None
    ) -> str:
        self.sent.append((queue, body, idempotency_key))
        return "message-id"


@pytest.fixture
def settings() -> Settings:
    return Settings(max_queued_jobs_per_user=5, max_jobs_per_day=20)


def build(photos: list[Photo], settings: Settings) -> tuple[ReconstructionService, Any, Any]:
    recon_repo = FakeReconstructionRepo()
    queue = FakeQueue()
    service = ReconstructionService(
        photos=FakePhotoRepo(photos),  # type: ignore[arg-type]
        reconstructions=recon_repo,  # type: ignore[arg-type]
        objects=FakeObjectRepo(),  # type: ignore[arg-type]
        audit=FakeAudit(),  # type: ignore[arg-type]
        queue=queue,
        settings=settings,
        pipeline_version="test-sha",
    )
    return service, recon_repo, queue


def payload(photos: list[Photo], **kw: Any) -> ReconstructionCreateIn:
    return ReconstructionCreateIn(photo_ids=[p.id for p in photos], **kw)


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
async def test_preview_does_not_enqueue_anything(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, i / 5)) for i in range(1, 5)]
    service, repo, queue = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert queue.sent == []
    assert repo.jobs == []
    assert plan.can_run is True
    assert plan.input_count == 4


async def test_preview_reports_the_resolved_license(fake_user: Any, settings: Settings) -> None:
    photos = [
        make_photo(1, LicenseCode.CC_BY, dither=(0.0, 0.0)),
        make_photo(2, LicenseCode.CC_BY_NC_SA, dither=(0.5, 0.5)),
    ]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.resulting_license is LicenseCode.CC_BY_NC_SA
    assert plan.requires_attribution is True


async def test_preview_lists_blocked_photos_instead_of_hiding_them(
    fake_user: Any, settings: Settings
) -> None:
    """Regla 1: el usuario tiene que ver *qué* le bloquea el job."""
    photos = [
        make_photo(1, LicenseCode.CC_BY, dither=(0.0, 0.0)),
        make_photo(2, LicenseCode.CC_BY_ND, dither=(0.5, 0.5)),
        make_photo(3, LicenseCode.CC_BY, stacks=False, dither=(0.2, 0.7)),
    ]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.can_run is False
    assert plan.resulting_license is None
    assert {str(b.photo_id)[-1] for b in plan.blocked} == {"2", "3"}


async def test_preview_warns_about_low_dither_diversity(fake_user: Any, settings: Settings) -> None:
    """Regla dura 1 de ``CLAUDE.md``: no prometer lo que la física no da."""
    photos = [make_photo(i, dither=(0.3, 0.3)) for i in range(1, 6)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.phase_diversity == pytest.approx(0.0)
    assert any("no recuperará muestreo" in w for w in plan.warnings)


async def test_preview_always_states_the_diffraction_ceiling(
    fake_user: Any, settings: Settings
) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.1)) for i in range(1, 5)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.best_diffraction_limit_arcsec == pytest.approx(1.384, abs=0.01)
    assert any("Techo físico" in w for w in plan.warnings)


async def test_preview_estimates_the_snr_gain_as_sqrt_n(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 10, i / 10)) for i in range(1, 11)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.estimated_snr_gain_db == pytest.approx(10.0, abs=0.01)


async def test_preview_estimates_a_cost(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 5)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos, pipeline="drizzle-v1"))
    assert plan.estimated_compute_seconds == pytest.approx(16.0)
    assert plan.estimated_cost_usd is not None and plan.estimated_cost_usd > 0
    assert plan.estimated_queue_seconds is not None
    assert plan.cost_basis is not None and "AWS Batch" in plan.cost_basis


# --------------------------------------------------------------------------- #
# Creación
# --------------------------------------------------------------------------- #
async def test_create_enqueues_and_records_provenance(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, i / 5)) for i in range(1, 5)]
    service, repo, queue = build(photos, settings)
    job, created = await service.create(
        user=fake_user, payload=payload(photos), idempotency_key=None
    )
    assert created is True
    assert job.license is LicenseCode.CC_BY
    assert job.pipeline_version == "test-sha"
    # Procedencia: una fila por foto, con la licencia congelada.
    assert len(repo.inputs) == 4
    assert all(row.snapshot_license is LicenseCode.CC_BY for row in repo.inputs)
    assert all(row.snapshot_attribution_name for row in repo.inputs)
    assert sum(row.weight for row in repo.inputs) == pytest.approx(1.0)
    assert len(queue.sent) == 1


async def test_create_writes_provenance_before_enqueueing(
    fake_user: Any, settings: Settings
) -> None:
    """Si el encolado falla, la trazabilidad ya está escrita."""
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    service, repo, queue = build(photos, settings)

    order: list[str] = []
    original_inputs = repo.add_inputs
    original_send = queue.send

    async def spy_inputs(rows: Any) -> None:
        order.append("inputs")
        await original_inputs(rows)

    async def spy_send(*args: Any, **kwargs: Any) -> str:
        order.append("queue")
        return await original_send(*args, **kwargs)

    repo.add_inputs = spy_inputs  # type: ignore[method-assign]
    queue.send = spy_send  # type: ignore[method-assign]
    await service.create(user=fake_user, payload=payload(photos), idempotency_key=None)
    assert order == ["inputs", "queue"]


async def test_create_rejects_blocked_licenses_with_422_detail(
    fake_user: Any, settings: Settings
) -> None:
    photos = [
        make_photo(1, LicenseCode.CC_BY, dither=(0.0, 0.0)),
        make_photo(2, LicenseCode.ARR, dither=(0.5, 0.5)),
    ]
    service, repo, queue = build(photos, settings)
    with pytest.raises(LicenseBlockedError) as exc:
        await service.create(user=fake_user, payload=payload(photos), idempotency_key=None)
    assert exc.value.status_code == 422
    assert exc.value.errors[0]["code"] == "no_derivatives"
    # Y no se ha creado ni encolado nada.
    assert repo.jobs == []
    assert queue.sent == []


async def test_idempotency_key_returns_the_same_job(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    service, _repo, queue = build(photos, settings)
    first, created_first = await service.create(
        user=fake_user, payload=payload(photos), idempotency_key="abc-123"
    )
    second, created_second = await service.create(
        user=fake_user, payload=payload(photos), idempotency_key="abc-123"
    )
    assert created_first is True and created_second is False
    assert first.id == second.id
    assert len(queue.sent) == 1


async def test_too_many_queued_jobs_is_rate_limited(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    service, repo, _ = build(photos, settings)
    repo.active = settings.max_queued_jobs_per_user
    with pytest.raises(RateLimitError) as exc:
        await service.create(user=fake_user, payload=payload(photos), idempotency_key=None)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"


async def test_daily_job_limit_is_rate_limited(fake_user: Any, settings: Settings) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    service, repo, _ = build(photos, settings)
    repo.today = settings.max_jobs_per_day
    with pytest.raises(RateLimitError):
        await service.create(user=fake_user, payload=payload(photos), idempotency_key=None)


# --------------------------------------------------------------------------- #
# Validación del cuerpo
# --------------------------------------------------------------------------- #
def test_a_single_frame_is_not_a_reconstruction() -> None:
    with pytest.raises(ValueError, match="al menos 2 frames"):
        ReconstructionCreateIn(photo_ids=[uuid.uuid4()])


def test_duplicate_photo_ids_are_rejected() -> None:
    same = uuid.uuid4()
    with pytest.raises(ValueError, match="duplicados"):
        ReconstructionCreateIn(photo_ids=[same, same])


def test_either_photo_ids_or_selector_but_not_both() -> None:
    with pytest.raises(ValueError, match="exactamente uno"):
        ReconstructionCreateIn()


def test_unknown_pipeline_is_rejected() -> None:
    with pytest.raises(ValueError, match="Pipeline desconocido"):
        ReconstructionCreateIn(photo_ids=[uuid.uuid4(), uuid.uuid4()], pipeline="magia-v9")


# --------------------------------------------------------------------------- #
# best_single_frame — la comparación honesta
# --------------------------------------------------------------------------- #
class _InputRow:
    def __init__(self, photo_id: uuid.UUID, *, rejected: bool = False) -> None:
        self.photo_id = photo_id
        self.was_rejected = rejected


class _InputsRepo:
    def __init__(self, rows: list[_InputRow]) -> None:
        self.rows = rows

    async def inputs_for(self, reconstruction_id: uuid.UUID) -> list[_InputRow]:
        return self.rows


async def _sign(key: str | None) -> str | None:
    return f"https://signed/{key}" if key else None


async def test_best_single_frame_picks_the_highest_quality_frame_used() -> None:
    from app.api.v1.reconstructions import _best_single_frame

    photos = [make_photo(1, quality=0.4), make_photo(2, quality=0.91), make_photo(3, quality=0.7)]
    for p in photos:
        p.s3_key_preview = f"preview-{p.id}"
        p.fwhm_arcsec = 2.0
        p.snr_estimate = 30.0
    repo = _InputsRepo([_InputRow(p.id) for p in photos])
    best = await _best_single_frame(
        repo,  # type: ignore[arg-type]
        FakePhotoRepo(photos),  # type: ignore[arg-type]
        uuid.uuid4(),
        _sign,
    )
    assert best is not None
    assert best.photo_id == photos[1].id
    assert best.quality_score == pytest.approx(0.91)
    assert best.preview_url is not None and best.preview_url.startswith("https://signed/")


async def test_best_single_frame_ignores_rejected_inputs() -> None:
    """Se compara contra un frame que *entró*, no contra uno que se descartó."""
    from app.api.v1.reconstructions import _best_single_frame

    used = make_photo(1, quality=0.5)
    rejected = make_photo(2, quality=0.99)
    repo = _InputsRepo([_InputRow(used.id), _InputRow(rejected.id, rejected=True)])
    best = await _best_single_frame(
        repo,  # type: ignore[arg-type]
        FakePhotoRepo([used, rejected]),  # type: ignore[arg-type]
        uuid.uuid4(),
        _sign,
    )
    assert best is not None
    assert best.photo_id == used.id


async def test_best_single_frame_breaks_ties_deterministically() -> None:
    from app.api.v1.reconstructions import _best_single_frame

    photos = [make_photo(i, quality=0.8) for i in (3, 1, 2)]
    repo = _InputsRepo([_InputRow(p.id) for p in photos])
    first = await _best_single_frame(
        repo,
        FakePhotoRepo(photos),
        uuid.uuid4(),
        _sign,  # type: ignore[arg-type]
    )
    second = await _best_single_frame(
        repo,
        FakePhotoRepo(list(reversed(photos))),
        uuid.uuid4(),
        _sign,  # type: ignore[arg-type]
    )
    assert first is not None and second is not None
    assert first.photo_id == second.photo_id == min(p.id for p in photos)


async def test_best_single_frame_is_none_without_inputs() -> None:
    from app.api.v1.reconstructions import _best_single_frame

    best = await _best_single_frame(
        _InputsRepo([]),
        FakePhotoRepo([]),
        uuid.uuid4(),
        _sign,  # type: ignore[arg-type]
    )
    assert best is None


async def test_best_single_frame_handles_photos_without_a_score() -> None:
    """Una foto sin `quality_score` no puede ganar a una que sí lo tiene."""
    from app.api.v1.reconstructions import _best_single_frame

    scored = make_photo(1, quality=0.3)
    unscored = make_photo(2, quality=0.3)
    unscored.quality_score = None
    repo = _InputsRepo([_InputRow(scored.id), _InputRow(unscored.id)])
    best = await _best_single_frame(
        repo,  # type: ignore[arg-type]
        FakePhotoRepo([scored, unscored]),  # type: ignore[arg-type]
        uuid.uuid4(),
        _sign,
    )
    assert best is not None
    assert best.photo_id == scored.id


# --------------------------------------------------------------------------- #
# Etiquetado de IA y separación bloqueo/rechazo en el preview
# --------------------------------------------------------------------------- #
async def test_preview_flags_a_classical_pipeline_as_not_learned(
    fake_user: Any, settings: Settings
) -> None:
    """`classical-stack-v1` combina píxeles medidos: no hay nada que inferir."""
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 5)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(
        user=fake_user, payload=payload(photos, pipeline="classical-stack-v1")
    )
    assert plan.uses_learned_model is False


async def test_preview_flags_a_learned_pipeline(fake_user: Any, settings: Settings) -> None:
    """Es lo que dispara el aviso de IA del frontend (regla dura 2 de CLAUDE.md)."""
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 5)]
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos, pipeline="burst-sr-v1"))
    assert plan.uses_learned_model is True


async def test_preview_reports_optics_of_the_selected_frames(
    fake_user: Any, settings: Settings
) -> None:
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 5)]
    for p in photos:
        p.fwhm_arcsec = 2.5
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert all(f.fwhm_arcsec == pytest.approx(2.5) for f in plan.selected)
    assert all(f.pixel_scale_arcsec == pytest.approx(2.0) for f in plan.selected)


async def test_preview_separates_rejected_from_blocked(fake_user: Any, settings: Settings) -> None:
    """Un frame sin resolver se **rechaza**; no bloquea el job, que sigue vivo.

    Es la distinción que el contrato tenía mezclada: `blocked[]` aborta con 422
    porque no hay forma legal de continuar; `rejected[]` solo deja el frame fuera.
    """
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 5)]
    unsolved = make_photo(9)
    unsolved.dither_phase_x = None
    unsolved.dither_phase_y = None
    unsolved.pixel_scale_arcsec = None
    everything = [*photos, unsolved]
    service, _, _ = build(everything, settings)
    # Vía `selector`: con lista explícita el target es la lista entera y un frame sin
    # resolver se usa igualmente (aporta SNR) en vez de descartarse.
    plan = await service.preview(
        user=fake_user,
        payload=ReconstructionCreateIn(
            object_id=OBJECT_ID,
            selector=PhotoSearchQuery(),
            target_count=len(photos),
        ),
    )
    assert plan.blocked == []
    assert plan.can_run is True
    assert RejectionReason.UNSOLVED in {r.reason for r in plan.rejected}
    assert unsolved.id not in {f.photo_id for f in plan.selected}


async def test_an_explicit_list_still_uses_an_unsolved_frame_for_snr(
    fake_user: Any, settings: Settings
) -> None:
    """Si el usuario la pide por id, un frame sin resolver entra: aporta señal.

    Solo se descarta cuando hay frames resueltos de sobra para llenar el cupo.
    """
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    unsolved = make_photo(9)
    unsolved.dither_phase_x = None
    unsolved.dither_phase_y = None
    unsolved.pixel_scale_arcsec = None
    everything = [*photos, unsolved]
    service, _, _ = build(everything, settings)
    plan = await service.preview(user=fake_user, payload=payload(everything))
    assert unsolved.id in {f.photo_id for f in plan.selected}


async def test_a_blocked_photo_still_aborts_the_whole_job(
    fake_user: Any, settings: Settings
) -> None:
    """El contraste con el test anterior: esto sí es un bloqueo, y aborta."""
    photos = [make_photo(i, dither=(i / 5, 0.0)) for i in range(1, 4)]
    photos.append(make_photo(9, LicenseCode.CC_BY_ND, dither=(0.9, 0.9)))
    service, _, _ = build(photos, settings)
    plan = await service.preview(user=fake_user, payload=payload(photos))
    assert plan.can_run is False
    assert len(plan.blocked) == 1


# --------------------------------------------------------------------------- #
# publish_result — la regla dura 2 la impone la máquina
# --------------------------------------------------------------------------- #
def _job(pipeline: str, model_id: uuid.UUID | None = None) -> Reconstruction:
    job = Reconstruction(
        id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        pipeline=pipeline,
        pipeline_version="sha",
        model_id=model_id,
        status=JobStatus.RUNNING,
        input_count=4,
        license=LicenseCode.CC_BY,
    )
    job.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    job.updated_at = job.created_at
    return job


class _JobRepo:
    def __init__(self, job: Reconstruction) -> None:
        self.job = job

    async def get(self, reconstruction_id: uuid.UUID) -> Reconstruction | None:
        return self.job if reconstruction_id == self.job.id else None


def _publisher(job: Reconstruction, settings: Settings) -> ReconstructionService:
    return ReconstructionService(
        photos=FakePhotoRepo([]),  # type: ignore[arg-type]
        reconstructions=_JobRepo(job),  # type: ignore[arg-type]
        objects=FakeObjectRepo(),  # type: ignore[arg-type]
        audit=FakeAudit(),  # type: ignore[arg-type]
        queue=FakeQueue(),
        settings=settings,
    )


async def test_a_classical_result_publishes_without_an_uncertainty_map(
    settings: Settings,
) -> None:
    """Un apilado clásico combina píxeles medidos: no hay nada que desetiquetar."""
    job = _job("classical-stack-v1")
    published = await _publisher(job, settings).publish_result(
        reconstruction_id=job.id,
        artifacts=ResultArtifacts(
            pipeline="classical-stack-v1",
            s3_key_result="r.fits",
            s3_key_attribution="ATTRIBUTION.md",
        ),
    )
    assert published.status is JobStatus.SUCCEEDED
    assert published.progress == pytest.approx(1.0)


async def test_a_learned_result_without_uncertainty_map_is_refused(
    settings: Settings,
) -> None:
    """Regla dura 2: nada generado sin etiquetar. Lo impone la máquina, no la costumbre."""
    job = _job("burst-sr-v1")
    published = await _publisher(job, settings).publish_result(
        reconstruction_id=job.id,
        artifacts=ResultArtifacts(
            pipeline="burst-sr-v1",
            s3_key_result="r.fits",
            s3_key_attribution="ATTRIBUTION.md",
        ),
    )
    assert published.status is JobStatus.FAILED
    assert published.error_message is not None
    assert "incertidumbre" in published.error_message
    # Y no se ha publicado nada: el resultado no queda accesible.
    assert published.s3_key_result is None


async def test_a_learned_result_with_uncertainty_map_publishes(
    settings: Settings,
) -> None:
    job = _job("burst-sr-v1")
    published = await _publisher(job, settings).publish_result(
        reconstruction_id=job.id,
        artifacts=ResultArtifacts(
            pipeline="burst-sr-v1",
            s3_key_result="r.fits",
            s3_key_uncertainty="unc.fits",
            s3_key_weight_map="w.fits",
            s3_key_attribution="ATTRIBUTION.md",
        ),
        metrics={"snr_gain_db": 9.0},
    )
    assert published.status is JobStatus.SUCCEEDED
    assert published.s3_key_uncertainty == "unc.fits"


async def test_a_classical_pipeline_with_an_explicit_model_also_needs_the_map(
    settings: Settings,
) -> None:
    """Un `model_id` en un pipeline clásico es una incoherencia: se trata como aprendido."""
    model_id = uuid.uuid4()
    job = _job("drizzle-v1", model_id)
    published = await _publisher(job, settings).publish_result(
        reconstruction_id=job.id,
        artifacts=ResultArtifacts(
            pipeline="drizzle-v1",
            model_id=str(model_id),
            s3_key_result="r.fits",
            s3_key_attribution="ATTRIBUTION.md",
        ),
    )
    assert published.status is JobStatus.FAILED


async def test_a_result_without_attribution_is_refused(settings: Settings) -> None:
    """Regla 5 de docs/licensing.md: atribución siempre, venga de donde venga."""
    job = _job("classical-stack-v1")
    published = await _publisher(job, settings).publish_result(
        reconstruction_id=job.id,
        artifacts=ResultArtifacts(pipeline="classical-stack-v1", s3_key_result="r.fits"),
    )
    assert published.status is JobStatus.FAILED
    assert "ATTRIBUTION" in (published.error_message or "")


async def test_publishing_an_unknown_job_is_a_404(settings: Settings) -> None:
    job = _job("classical-stack-v1")
    with pytest.raises(NotFoundError):
        await _publisher(job, settings).publish_result(
            reconstruction_id=uuid.uuid4(),
            artifacts=ResultArtifacts(
                pipeline="classical-stack-v1",
                s3_key_result="r.fits",
                s3_key_attribution="a.md",
            ),
        )
