"""Unidad de trabajo por petición.

El invariante: **cuando el cliente ve la respuesta, lo que promete ya está en base**.
Y su recíproco, igual de importante: lo que la respuesta niega (un 4xx) no queda
escrito.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.core.uow import SCOPE_KEY, UnitOfWork, UnitOfWorkMiddleware


class FakeSession:
    """Registra el orden de las operaciones de transacción."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")

    async def close(self) -> None:
        self.log.append("close")


def fake_factory(log: list[str]) -> Any:
    def factory() -> FakeSession:
        log.append("open")
        return FakeSession(log)

    return factory


# --------------------------------------------------------------------------- #
# La unidad de trabajo en aislamiento
# --------------------------------------------------------------------------- #
async def test_a_request_that_never_touches_the_database_opens_no_session() -> None:
    """Una lectura pura no debe gastar una conexión del pool."""
    log: list[str] = []
    uow = UnitOfWork(fake_factory(log))
    assert uow.active is False
    await uow.commit()
    await uow.close()
    assert log == []


async def test_the_session_is_created_once_and_shared() -> None:
    """Todo lo de una petición entra en la **misma** transacción."""
    log: list[str] = []
    uow = UnitOfWork(fake_factory(log))
    first = await uow.session()
    second = await uow.session()
    assert first is second
    assert log.count("open") == 1


async def test_commit_runs_hooks_after_the_transaction() -> None:
    """El efecto externo va después de que el dato sea durable, nunca antes."""
    log: list[str] = []
    uow = UnitOfWork(fake_factory(log))
    await uow.session()

    async def hook() -> None:
        log.append("enqueue")

    uow.after_commit(hook)
    await uow.commit()
    assert log == ["open", "commit", "enqueue"]


async def test_rollback_discards_the_hooks() -> None:
    """Nada que no persista se anuncia: sin commit no hay mensaje en la cola."""
    log: list[str] = []
    uow = UnitOfWork(fake_factory(log))
    await uow.session()

    async def hook() -> None:  # pragma: no cover - no debe ejecutarse
        log.append("enqueue")

    uow.after_commit(hook)
    await uow.rollback()
    await uow.commit()
    assert "enqueue" not in log


async def test_a_failing_hook_does_not_undo_a_committed_transaction() -> None:
    """El dato ya es durable; que SQS falle se registra y se sigue."""
    log: list[str] = []
    uow = UnitOfWork(fake_factory(log))
    await uow.session()

    async def boom() -> None:
        raise RuntimeError("SQS caído")

    async def after() -> None:
        log.append("segundo")

    uow.after_commit(boom)
    uow.after_commit(after)
    await uow.commit()
    assert "commit" in log
    assert "segundo" in log


async def test_using_a_closed_unit_of_work_fails_loudly() -> None:
    """El código posterior a la respuesta debe abrir su propia sesión."""
    uow = UnitOfWork(fake_factory([]))
    await uow.close()
    with pytest.raises(RuntimeError, match="session_scope"):
        await uow.session()


# --------------------------------------------------------------------------- #
# El middleware, contra una app real
# --------------------------------------------------------------------------- #
def build(log: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(UnitOfWorkMiddleware, factory=lambda: fake_factory(log))

    @app.get("/ok")
    async def ok(request: Request) -> dict[str, str]:
        await request.scope[SCOPE_KEY].session()
        log.append("handler")
        return {"status": "ok"}

    @app.get("/client-error")
    async def client_error(request: Request) -> dict[str, str]:
        # Escribe y **después** falla: es el caso que importa.
        await request.scope[SCOPE_KEY].session()
        log.append("write")
        raise HTTPException(status_code=409, detail="conflicto")

    @app.get("/boom")
    async def boom(request: Request) -> dict[str, str]:
        await request.scope[SCOPE_KEY].session()
        log.append("write")
        raise RuntimeError("fallo inesperado")

    @app.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        await request.scope[SCOPE_KEY].session()

        async def body() -> Any:
            # Para cuando esto corre, la unidad de trabajo ya se cerró.
            log.append("streaming")
            yield b"data\n"

        return StreamingResponse(body())

    return app


def test_a_successful_request_commits_before_the_response() -> None:
    log: list[str] = []
    with TestClient(build(log)) as client:
        assert client.get("/ok").status_code == 200
    assert log == ["open", "handler", "commit", "close"]


def test_a_4xx_raised_after_writing_rolls_back() -> None:
    """La respuesta le dice al cliente que no ha pasado nada; que sea verdad."""
    log: list[str] = []
    with TestClient(build(log)) as client:
        assert client.get("/client-error").status_code == 409
    assert log == ["open", "write", "rollback", "close"]
    assert "commit" not in log


def test_an_unhandled_exception_rolls_back_and_closes() -> None:
    log: list[str] = []
    with TestClient(build(log), raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500
    assert "rollback" in log
    assert "close" in log
    assert "commit" not in log


def test_a_streaming_response_closes_the_unit_of_work_before_streaming() -> None:
    """El cuerpo de un SSE se produce después; por eso no puede usar esa sesión."""
    log: list[str] = []
    with TestClient(build(log)) as client:
        assert client.get("/stream").status_code == 200
    assert log.index("close") < log.index("streaming"), (
        "la unidad de trabajo debe cerrarse antes de que el generador emita nada; "
        f"orden observado: {log}"
    )


def test_the_session_is_always_closed_even_on_error() -> None:
    """Ninguna conexión se queda colgada del pool, pase lo que pase."""
    log: list[str] = []
    app = build(log)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/ok")
        client.get("/client-error")
        client.get("/boom")
    assert log.count("open") == log.count("close") == 3
