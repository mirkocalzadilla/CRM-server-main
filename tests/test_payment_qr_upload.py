"""Subida del QR de pago de la organización: uno solo, reemplazable y borrable.

El QR propio era una URL que el operador pegaba a mano apuntando a un hosting ajeno.
Ahora el archivo vive adentro, y lo que hay que garantizar es que no se acumulen
imágenes huérfanas: cada tenant tiene un archivo, reemplazarlo borra el anterior y
sacarlo devuelve el QR global de la plataforma.

`media_root` se apunta a un tmp_path porque estos tests escriben en disco de verdad;
es la única forma de verificar el "uno solo" contra el directorio.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select

from server.config import get_settings
from server.modules.crm.domain.payment_models import PaymentSettings
from server.modules.crm.services import payment_qr_store

from .test_catalog import (
    API,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    SessionFactory,
    _auth,
    _login,
    _operator_token,
    _seed_staff_member,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPG_BYTES = b"\xff\xd8\xff" + b"0" * 32
PDF_BYTES = b"%PDF-1.7 fake"

QR_ENDPOINT = f"{API}/crm/payment-settings/qr"


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        payment_qr_store,
        "get_settings",
        lambda: SimpleNamespace(media_root=str(tmp_path), media_base_url="https://media.test"),
    )
    return tmp_path


def _stored_files(media_root: Path) -> list[Path]:
    return sorted(path for path in (media_root / "payments").rglob("*") if path.is_file())


async def _upload(
    client: AsyncClient, token: str, data: bytes, mime: str = "image/png"
) -> Response:
    return await client.post(
        QR_ENDPOINT,
        files={"file": ("qr.png", data, mime)},
        headers=_auth(token),
    )


async def test_upload_sets_the_org_qr_and_writes_one_file(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    token = await _operator_token(client, session_factory)
    response = await _upload(client, token, PNG_BYTES)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_qr_url_custom"] is True
    assert body["is_qr_uploaded"] is True
    assert body["payment_qr_url"].startswith("https://media.test/media/payments/")
    assert len(_stored_files(media_root)) == 1

    async with session_factory() as session:
        row = (await session.execute(select(PaymentSettings))).scalar_one()
        assert row.payment_qr_storage_ref is not None
        assert row.payment_qr_url == body["payment_qr_url"]


async def test_second_upload_replaces_the_previous_file(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    """El punto del CR: una sola imagen por organización, sin huérfanos en disco."""
    token = await _operator_token(client, session_factory)
    first = (await _upload(client, token, PNG_BYTES)).json()
    second = (await _upload(client, token, JPG_BYTES, "image/jpeg")).json()

    assert second["payment_qr_url"] != first["payment_qr_url"]  # URL nueva ⇒ sin cache vieja
    assert len(_stored_files(media_root)) == 1


async def test_delete_returns_to_global_qr_and_removes_the_file(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    token = await _operator_token(client, session_factory)
    await _upload(client, token, PNG_BYTES)

    response = await client.delete(QR_ENDPOINT, headers=_auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payment_qr_url"] == get_settings().payment_qr_url
    assert body["is_qr_url_custom"] is False
    assert body["is_qr_uploaded"] is False
    assert _stored_files(media_root) == []


async def test_delete_without_previous_upload_is_a_no_op(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    """Borrar cuando ya se está en el global no puede fallar ni crear una fila."""
    token = await _operator_token(client, session_factory)
    response = await client.delete(QR_ENDPOINT, headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["payment_qr_url"] == get_settings().payment_qr_url


async def test_manual_url_update_drops_the_uploaded_file(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    """Si alguien pisa la URL por el PUT, el archivo nuestro deja de estar referenciado
    y no debe quedar ocupando disco."""
    token = await _operator_token(client, session_factory)
    await _upload(client, token, PNG_BYTES)

    response = await client.put(
        f"{API}/crm/payment-settings",
        json={"payment_qr_url": "https://cdn.example.com/qr.png"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_qr_uploaded"] is False
    assert _stored_files(media_root) == []


async def test_resending_the_same_url_keeps_the_uploaded_file(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    """Un cliente que devuelve por PUT la config que acaba de leer no está cambiando
    el QR: borrar el archivo ahí dejaría la fila apuntando a una imagen que no existe."""
    token = await _operator_token(client, session_factory)
    uploaded = (await _upload(client, token, PNG_BYTES)).json()

    response = await client.put(
        f"{API}/crm/payment-settings",
        json={"payment_qr_url": uploaded["payment_qr_url"], "expected_beneficiary": "Mirko"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payment_qr_url"] == uploaded["payment_qr_url"]
    assert body["is_qr_uploaded"] is True
    assert len(_stored_files(media_root)) == 1


async def test_upload_rejects_a_non_image(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    token = await _operator_token(client, session_factory)
    response = await _upload(client, token, PDF_BYTES, "application/pdf")

    assert response.status_code == 422, response.text
    assert _stored_files(media_root) == []


async def test_upload_rejects_a_fake_mime_type(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    """El content-type lo declara el cliente; lo que decide son los magic bytes."""
    token = await _operator_token(client, session_factory)
    response = await _upload(client, token, PDF_BYTES, "image/png")

    assert response.status_code == 422, response.text
    assert _stored_files(media_root) == []


async def test_staff_cannot_upload_or_delete_the_qr(
    client: AsyncClient, session_factory: SessionFactory, media_root: Path
) -> None:
    await _operator_token(client, session_factory)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    assert (await _upload(client, staff_token, PNG_BYTES)).status_code == 403
    assert (await client.delete(QR_ENDPOINT, headers=_auth(staff_token))).status_code == 403
