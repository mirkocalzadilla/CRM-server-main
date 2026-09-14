"""Check-in manual en la puerta, cuando la cámara no sirve (server#278, CR6).

El escáner puede fallar por razones triviales y muy probables: el navegador no da permiso
de cámara, hay poca luz, el QR está arrugado o la pantalla del lead está rota. Sin una vía
manual, cualquiera de esas cosas deja afuera a alguien que **pagó** — y además la
asistencia registrada queda por debajo de la real, que es el número que después se usa
para saber cuánta gente entró.

Lo que estos tests protegen:

1. Que la vía manual **admita** a quien tiene una entrada válida.
2. Que **rechace por lo mismo** que el escaneo. Es lo importante: el camino manual existe
   porque falló la cámara, no para saltear una entrada anulada o ya usada. Si los dos
   caminos no comparten los rechazos, el manual se convierte en el agujero por donde pasa
   todo lo que el escáner frena.
3. Que quede **marcado como manual**, porque después del evento la pregunta "a quién
   dejamos pasar sin ver el QR" necesita respuesta.
4. Que no cruce organizaciones.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select

from server.modules.crm.domain.models import QrEntry
from server.modules.crm.domain.redemption import RedeemStatus

from .test_catalog import API, SessionFactory, _auth, _operator_token, _register
from .test_entry_redemption import _redeem, _seed


async def _entry_id(session_factory: SessionFactory, token: str) -> uuid.UUID:
    """El id que la lista de asistencia le muestra a quien atiende."""
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        return entry.id


async def _check_in(
    client: AsyncClient,
    entry_id: uuid.UUID,
    auth: str,
    event_id: uuid.UUID | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {}
    if event_id is not None:
        body["event_id"] = str(event_id)
    response = await client.post(
        f"{API}/crm/entries/{entry_id}/check-in", json=body, headers=_auth(auth)
    )
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


async def test_manual_check_in_admits_and_shows_the_attendee(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    body = await _check_in(client, entry_id, token_auth, event_id)

    assert body["status"] == RedeemStatus.OK
    assert body["admitted"] is True
    # Los mismos datos que muestra el escaneo: quien atiende ve lo mismo por las dos vías.
    assert body["lead_name"] == "Ana Quispe"
    assert body["service_name"] == "Curso de edición"
    assert body["event_name"] == "Edición septiembre"


async def test_manual_check_in_is_recorded_as_manual(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """La marca durable: los logs de esta app no sobreviven a un deploy (#260)."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    await _check_in(client, entry_id, token_auth, event_id)

    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        assert entry.used_at is not None
        assert entry.used_manually is True
        assert entry.used_by is not None


async def test_scan_is_not_recorded_as_manual(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El contraste que le da sentido a la marca: escanear no la enciende."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)

    await _redeem(client, token, token_auth, event_id)

    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        assert entry.used_at is not None
        assert entry.used_manually is False


async def test_manual_check_in_refuses_a_revoked_entry(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El caso que importa: la vía manual no es una puerta de servicio."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory, revoked=True)
    entry_id = await _entry_id(session_factory, token)

    body = await _check_in(client, entry_id, token_auth, event_id)

    assert body["status"] == RedeemStatus.REVOKED
    assert body["admitted"] is False
    assert "anulada" in str(body["detail"])

    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        # Y no la consumió: una entrada anulada sigue anulada y sin usar.
        assert entry.used_at is None


async def test_manual_check_in_refuses_an_already_used_entry(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory, used=True)
    entry_id = await _entry_id(session_factory, token)

    body = await _check_in(client, entry_id, token_auth, event_id)

    assert body["status"] == RedeemStatus.ALREADY_USED
    assert body["admitted"] is False
    assert body["used_at"] is not None


async def test_manual_check_in_refuses_an_entry_from_another_event(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, _event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    body = await _check_in(client, entry_id, token_auth, uuid.uuid4())

    assert body["status"] == RedeemStatus.WRONG_EVENT
    assert body["admitted"] is False


async def test_manual_check_in_cannot_be_repeated(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Dos personas atendiendo la fila con dos teléfonos: la segunda no entra igual."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    first = await _check_in(client, entry_id, token_auth, event_id)
    second = await _check_in(client, entry_id, token_auth, event_id)

    assert first["admitted"] is True
    assert second["status"] == RedeemStatus.ALREADY_USED
    assert second["admitted"] is False


async def test_manual_check_in_after_a_scan_is_refused(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Las dos vías comparten el mismo estado: no se puede entrar dos veces mezclándolas."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    scan = await _redeem(client, token, token_auth, event_id)
    manual = await _check_in(client, entry_id, token_auth, event_id)

    assert scan["admitted"] is True
    assert manual["status"] == RedeemStatus.ALREADY_USED


async def test_manual_check_in_of_an_unknown_entry_is_not_found(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    await _seed(session_factory)

    body = await _check_in(client, uuid.uuid4(), token_auth)

    assert body["status"] == RedeemStatus.NOT_FOUND
    assert body["admitted"] is False


async def test_manual_check_in_does_not_cross_organizations(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Una entrada de otra organización se ve igual que una que no existe."""
    await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)
    foreign = await _register(client, "otra-admin@example.com", "otra-secret", "otra")

    body = await _check_in(client, entry_id, foreign, event_id)

    assert body["status"] == RedeemStatus.NOT_FOUND

    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        assert entry.used_at is None


async def test_attendance_list_flags_the_manual_ones(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Lo que hace útil a la marca: se ve en la lista, sin abrir la base."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    entry_id = await _entry_id(session_factory, token)

    await _check_in(client, entry_id, token_auth, event_id)
    response = await client.get(
        f"{API}/crm/entries/attendance/{event_id}", headers=_auth(token_auth)
    )

    assert response.status_code == 200, response.text
    attendees = response.json()["attendees"]
    assert len(attendees) == 1
    assert attendees[0]["used_manually"] is True
