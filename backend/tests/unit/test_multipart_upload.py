"""Cierre de subidas multipart.

Es el camino de las subidas > 100 MB. Sin `CompleteMultipartUpload` S3 guarda las
partes pero no materializa el objeto, y la regla de ciclo de vida acaba borrándolas:
por eso este flujo se prueba entero, incluidos todos los caminos de fallo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from app.domain.licensing import LicenseCode
from app.models.enums import PhotoStatus
from app.models.photo import Photo
from app.schemas.photo import MultipartCompleteIn
from app.services.upload import UploadService

UPLOAD_ID = "upload-abc-123"


def make_photo(
    *,
    owner_id: uuid.UUID,
    upload_id: str | None = UPLOAD_ID,
    status: PhotoStatus = PhotoStatus.UPLOADING,
    original_bytes: int | None = 300,
) -> Photo:
    photo = Photo(
        id=uuid.uuid4(),
        owner_id=owner_id,
        status=status,
        s3_bucket="uploads",
        s3_key_original="staging/u/p/big.tif",
        checksum_sha256=b"\x00" * 32,
        license=LicenseCode.CC_BY_NC,
        original_bytes=original_bytes,
        multipart_upload_id=upload_id,
    )
    photo.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    photo.updated_at = photo.created_at
    return photo


class FakePhotoRepo:
    def __init__(self, photos: list[Photo]) -> None:
        self.photos = {p.id: p for p in photos}

    async def get(self, photo_id: uuid.UUID, **kw: Any) -> Photo | None:
        return self.photos.get(photo_id)


class FakeStorage:
    def __init__(self, total_bytes: int = 300, error: Exception | None = None) -> None:
        self.total_bytes = total_bytes
        self.error = error
        self.completed: list[tuple[str, str, list[tuple[int, str]]]] = []
        self.aborted: list[tuple[str, str]] = []

    async def complete_multipart_upload(
        self, *, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> int:
        if self.error is not None:
            raise self.error
        self.completed.append((key, upload_id, parts))
        return self.total_bytes

    async def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


def build(photo: Photo, storage: FakeStorage | None = None) -> tuple[UploadService, Any]:
    store = storage or FakeStorage()
    audit = FakeAudit()
    service = UploadService(
        photos=FakePhotoRepo([photo]),  # type: ignore[arg-type]
        users=object(),  # type: ignore[arg-type]  # no se usa en este flujo
        audit=audit,  # type: ignore[arg-type]
        storage=store,  # type: ignore[arg-type]
        queue=object(),  # type: ignore[arg-type]
        settings=Settings(environment="test"),
    )
    return service, store


def payload(parts: int = 3, upload_id: str = UPLOAD_ID) -> MultipartCompleteIn:
    return MultipartCompleteIn(
        upload_id=upload_id,
        parts=[{"part_number": n, "etag": f"etag{n}"} for n in range(1, parts + 1)],  # type: ignore[list-item]
    )


def s3_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "CompleteMultipartUpload")


# --------------------------------------------------------------------------- #
# Validación del cuerpo
# --------------------------------------------------------------------------- #
def test_parts_must_start_at_one_and_have_no_gaps() -> None:
    with pytest.raises(ValueError, match="huecos"):
        MultipartCompleteIn(
            upload_id="u",
            parts=[{"part_number": 1, "etag": "a"}, {"part_number": 3, "etag": "b"}],  # type: ignore[list-item]
        )


def test_parts_cannot_start_at_two() -> None:
    with pytest.raises(ValueError, match="huecos"):
        MultipartCompleteIn(
            upload_id="u",
            parts=[{"part_number": 2, "etag": "a"}, {"part_number": 3, "etag": "b"}],  # type: ignore[list-item]
        )


def test_duplicate_part_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="repetidos"):
        MultipartCompleteIn(
            upload_id="u",
            parts=[{"part_number": 1, "etag": "a"}, {"part_number": 1, "etag": "b"}],  # type: ignore[list-item]
        )


def test_an_empty_part_list_is_rejected() -> None:
    with pytest.raises(ValueError):
        MultipartCompleteIn(upload_id="u", parts=[])


def test_etags_are_normalised_to_the_quoted_form_s3_expects() -> None:
    """S3 devuelve el ETag entrecomillado; se acepta con o sin comillas."""
    body = MultipartCompleteIn(
        upload_id="u",
        parts=[{"part_number": 1, "etag": "abc"}, {"part_number": 2, "etag": '"def"'}],  # type: ignore[list-item]
    )
    assert [p.etag for p in body.parts] == ['"abc"', '"def"']


def test_parts_out_of_order_in_the_body_are_accepted() -> None:
    """El orden lo impone el servicio al llamar a S3, no el cliente."""
    body = MultipartCompleteIn(
        upload_id="u",
        parts=[{"part_number": 2, "etag": "b"}, {"part_number": 1, "etag": "a"}],  # type: ignore[list-item]
    )
    assert len(body.parts) == 2


# --------------------------------------------------------------------------- #
# Camino feliz
# --------------------------------------------------------------------------- #
async def test_completion_closes_the_upload_and_clears_the_id(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, storage = build(photo)
    result = await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())
    assert result.photo_id == photo.id
    assert result.total_bytes == 300
    assert result.next_step.endswith("/complete")
    # El upload_id se limpia: una segunda llamada ya no puede colarse.
    assert photo.multipart_upload_id is None
    assert len(storage.completed) == 1


async def test_completion_passes_every_part_through(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, storage = build(photo)
    body = MultipartCompleteIn(
        upload_id=UPLOAD_ID,
        parts=[  # type: ignore[list-item]
            {"part_number": 3, "etag": "c"},
            {"part_number": 1, "etag": "a"},
            {"part_number": 2, "etag": "b"},
        ],
    )
    await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=body)
    _, upload_id, parts = storage.completed[0]
    assert upload_id == UPLOAD_ID
    assert sorted(n for n, _ in parts) == [1, 2, 3]


async def test_storage_sends_the_parts_sorted_to_s3() -> None:
    """S3 exige ``Parts`` ordenado por ``PartNumber``; no dependemos del cliente."""
    from app.services.storage import StorageService

    sent: dict[str, Any] = {}

    class FakeClient:
        def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
            sent.update(kwargs)
            return {}

        def head_object(self, **kwargs: Any) -> dict[str, int]:
            return {"ContentLength": 42}

    storage = StorageService(Settings(environment="test"))
    storage._client = FakeClient()
    total = await storage.complete_multipart_upload(
        key="k", upload_id="u", parts=[(3, '"c"'), (1, '"a"'), (2, '"b"')]
    )
    assert total == 42
    assert [p["PartNumber"] for p in sent["MultipartUpload"]["Parts"]] == [1, 2, 3]
    assert [p["ETag"] for p in sent["MultipartUpload"]["Parts"]] == ['"a"', '"b"', '"c"']


async def test_storage_abort_never_raises() -> None:
    """Abortar es limpieza: si falla, no debe tumbar la operación que la pidió."""
    from app.services.storage import StorageService

    class ExplodingClient:
        def abort_multipart_upload(self, **kwargs: Any) -> None:
            raise s3_error("NoSuchUpload")

    storage = StorageService(Settings(environment="test"))
    storage._client = ExplodingClient()
    await storage.abort_multipart_upload(key="k", upload_id="u")


async def test_completion_leaves_the_photo_ready_for_step_three(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo)
    result = await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())
    # Sigue en `uploading`: `complete` es quien pasa a `processing`.
    assert result.status is PhotoStatus.UPLOADING
    assert photo.status is PhotoStatus.UPLOADING


async def test_completion_is_audited(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo)
    await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())
    actions = [e["action"] for e in service.audit.entries]  # type: ignore[attr-defined]
    assert "photo.multipart_completed" in actions


# --------------------------------------------------------------------------- #
# Pertenencia y estado
# --------------------------------------------------------------------------- #
async def test_another_users_upload_is_a_404_not_a_403(fake_user: Any) -> None:
    """Confirmar que un id existe ya filtra información sobre subidas ajenas."""
    photo = make_photo(owner_id=uuid.uuid4())
    service, _ = build(photo)
    with pytest.raises(NotFoundError):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


async def test_an_unknown_photo_is_a_404(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo)
    with pytest.raises(NotFoundError):
        await service.complete_multipart(user=fake_user, photo_id=uuid.uuid4(), payload=payload())


async def test_a_wrong_upload_id_is_rejected(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, storage = build(photo)
    with pytest.raises(BadRequestError, match="no corresponde"):
        await service.complete_multipart(
            user=fake_user, photo_id=photo.id, payload=payload(upload_id="otro")
        )
    assert storage.completed == []


async def test_completing_twice_is_a_409(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo)
    await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())
    with pytest.raises(ConflictError):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


async def test_a_simple_upload_cannot_be_completed_as_multipart(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id, upload_id=None)
    service, _ = build(photo)
    with pytest.raises(ConflictError, match="no tiene ninguna subida multipart"):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


async def test_an_already_processed_photo_is_a_409(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id, upload_id=None, status=PhotoStatus.PROCESSING)
    service, _ = build(photo)
    with pytest.raises(ConflictError, match="ya se cerró"):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


# --------------------------------------------------------------------------- #
# Errores de S3
# --------------------------------------------------------------------------- #
async def test_an_expired_upload_becomes_a_409_with_a_way_forward(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo, FakeStorage(error=s3_error("NoSuchUpload")))
    with pytest.raises(ConflictError, match="POST /photos/uploads"):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())
    # Y se limpia el id muerto para no dejar la fila mintiendo.
    assert photo.multipart_upload_id is None


async def test_bad_etags_become_a_422_that_says_what_to_check(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, _ = build(photo, FakeStorage(error=s3_error("InvalidPart")))
    with pytest.raises(UnprocessableError, match="etag"):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


async def test_a_size_mismatch_is_a_409(fake_user: Any) -> None:
    """El objeto ensamblado no puede pesar algo distinto de lo anunciado."""
    photo = make_photo(owner_id=fake_user.id, original_bytes=300)
    service, _ = build(photo, FakeStorage(total_bytes=999))
    with pytest.raises(ConflictError, match="999"):
        await service.complete_multipart(user=fake_user, photo_id=photo.id, payload=payload())


# --------------------------------------------------------------------------- #
# Aborto
# --------------------------------------------------------------------------- #
async def test_aborting_frees_the_orphan_parts(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id)
    service, storage = build(photo)
    await service.abort_upload(user=fake_user, photo_id=photo.id)
    assert storage.aborted == [(photo.s3_key_original, UPLOAD_ID)]
    assert photo.multipart_upload_id is None
    assert photo.deleted_at is not None


async def test_aborting_a_simple_upload_does_not_call_s3(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id, upload_id=None)
    service, storage = build(photo)
    await service.abort_upload(user=fake_user, photo_id=photo.id)
    assert storage.aborted == []
    assert photo.deleted_at is not None


async def test_aborting_a_processed_photo_points_at_delete(fake_user: Any) -> None:
    photo = make_photo(owner_id=fake_user.id, upload_id=None, status=PhotoStatus.READY)
    service, _ = build(photo)
    with pytest.raises(ConflictError, match="DELETE"):
        await service.abort_upload(user=fake_user, photo_id=photo.id)


async def test_aborting_someone_elses_upload_is_a_404(fake_user: Any) -> None:
    photo = make_photo(owner_id=uuid.uuid4())
    service, _ = build(photo)
    with pytest.raises(NotFoundError):
        await service.abort_upload(user=fake_user, photo_id=photo.id)
