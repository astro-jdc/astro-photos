"""Unidad de trabajo por petición.

Una petición HTTP = una transacción. Se confirma **antes** de emitir la respuesta,
de modo que cuando el cliente recibe un 200 el dato ya es visible para cualquier
otra conexión. Ese es el invariante que aquí se defiende: *lo que la respuesta
promete, ya está.*

Por qué un middleware y no el teardown de la dependencia
--------------------------------------------------------

El teardown de una dependencia con ``yield`` corre en un punto que depende de la
versión de FastAPI y que, en la nuestra, es **posterior** al envío de la respuesta.
Confirmar ahí produce exactamente el fallo que QA midió: un 200 con la metadata
nueva y una lectura inmediata que devuelve la fila anterior. El middleware, en
cambio, envuelve la ejecución del handler y devuelve el objeto respuesta hacia
arriba **antes** de que nadie lo serialice al cliente, así que el punto de commit es
explícito y verificable.

Efectos externos: ``after_commit``
----------------------------------

Encolar en SQS dentro de la transacción es un error clásico: si el commit falla
después, queda un mensaje apuntando a una fila que no existe, y el worker revienta.
Al revés es recuperable —una fila ``queued`` sin mensaje la vuelve a encolar un
barrido— así que los efectos externos se registran con :meth:`UnitOfWork.after_commit`
y corren **después** de que la transacción sea durable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["UnitOfWork", "UnitOfWorkMiddleware", "unit_of_work"]

log = structlog.get_logger(__name__)

#: Clave bajo la que el middleware deja la unidad de trabajo en el scope ASGI.
SCOPE_KEY = "astro_uow"

AfterCommitHook = Callable[[], Awaitable[Any]]


class UnitOfWork:
    """La transacción de una petición, más sus efectos posteriores al commit."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None
        self._hooks: list[AfterCommitHook] = []
        self._closed = False

    @property
    def active(self) -> bool:
        """``True`` si la petición llegó a abrir sesión. Las lecturas puras que no
        tocan la base no gastan ni una conexión del pool."""
        return self._session is not None

    async def session(self) -> AsyncSession:
        """La sesión de esta petición, creada al primer uso."""
        if self._closed:
            raise RuntimeError(
                "La unidad de trabajo de esta petición ya está cerrada. El código que "
                "corre después de la respuesta (SSE, BackgroundTasks) debe abrir la "
                "suya con `app.db.session.session_scope()`."
            )
        if self._session is None:
            self._session = self._factory()
        return self._session

    def after_commit(self, hook: AfterCommitHook) -> None:
        """Registra un efecto externo que solo debe ocurrir si la escritura persiste.

        Se usa para encolar en SQS: primero el dato es durable, después el mensaje.
        Un mensaje sin fila rompe al worker; una fila sin mensaje se reencola.
        """
        self._hooks.append(hook)

    async def commit(self) -> None:
        """Confirma y dispara los efectos externos, en ese orden."""
        if self._session is not None:
            await self._session.commit()
        for hook in self._hooks:
            # Un efecto externo fallido no puede deshacer una transacción ya
            # confirmada, así que se registra y se sigue: el estado en base es
            # correcto y recuperable.
            try:
                await hook()
            except Exception:
                log.exception("after_commit_hook_failed")
        self._hooks.clear()

    async def rollback(self) -> None:
        """Revierte y **descarta** los efectos externos: nada que no persista se anuncia."""
        self._hooks.clear()
        if self._session is not None:
            await self._session.rollback()

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None


def unit_of_work(request: Request) -> UnitOfWork:
    """La unidad de trabajo de la petición en curso."""
    uow = request.scope.get(SCOPE_KEY)
    if uow is None:
        raise RuntimeError(
            "No hay unidad de trabajo en esta petición: falta UnitOfWorkMiddleware en la app."
        )
    assert isinstance(uow, UnitOfWork)
    return uow


class UnitOfWorkMiddleware:
    """Abre, confirma y cierra la unidad de trabajo de cada petición.

    Middleware ASGI puro y no ``BaseHTTPMiddleware``: aquí importa exactamente
    *cuándo* se confirma respecto al envío de la respuesta, y con ASGI crudo ese
    punto se ve —es justo antes de dejar pasar el ``http.response.start``— en vez de
    quedar escondido tras una capa de streaming.

    Criterio de commit: **el código de estado**. Un 4xx que se lanza después de
    haber escrito (por ejemplo un 409 de licencia congelada tras haber tocado la
    fila) revierte, porque la respuesta le está diciendo al cliente que no ha pasado
    nada.
    """

    def __init__(self, app: ASGIApp, factory: Callable[[], async_sessionmaker[AsyncSession]]):
        self.app = app
        self._factory = factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        uow = UnitOfWork(self._factory())
        scope[SCOPE_KEY] = uow
        status_code = 500
        finished = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code, finished
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                # Último instante en que se puede decidir: el cuerpo aún no ha salido
                # y el cliente todavía no sabe nada. Después de esto, lo prometido
                # tiene que estar en base.
                if uow.active:
                    if status_code < 400:
                        await uow.commit()
                    else:
                        await uow.rollback()
                elif status_code < 400:
                    # Sin sesión pero con efectos registrados (raro, pero posible).
                    await uow.commit()
                await uow.close()
                finished = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            await uow.rollback()
            await uow.close()
            raise
        finally:
            if not finished:
                # La respuesta nunca arrancó (cliente desconectado a media petición).
                await uow.rollback()
                await uow.close()


def install_unit_of_work(app: Any) -> None:
    """Registra el middleware con la fábrica de sesiones perezosa."""
    from app.db.session import session_factory

    app.add_middleware(UnitOfWorkMiddleware, factory=session_factory)
