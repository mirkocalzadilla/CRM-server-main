"""API CRM (slice 2 + 2b). Tablero + detalle + mover card + takeover + SSE realtime.

Todo autenticado y tenant-scoped por `ctx.tenant.id`. Las ops del CRM
(inbox/tablero/takeover/`/send`) las pueden hacer los 3 roles; config/users/agente
exigen platform_operator (ver core/api/deps.py). `/events` (SSE) autentica por
`?token=` porque `EventSource` no permite headers custom.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from server.modules.agent.services.asset_service import MATERIAL_MAX_BYTES
from server.modules.core.api.deps import CurrentUser, DbSession, get_current_context
from server.modules.crm.api.contacts_router import router as contacts_router
from server.modules.crm.api.event_router import router as event_router
from server.modules.crm.api.payment_settings_router import router as payment_settings_router
from server.modules.crm.api.receipt_router import router as receipt_router
from server.modules.crm.api.reconciliation_router import router as reconciliation_router
from server.modules.crm.api.redemption_router import router as redemption_router
from server.modules.crm.api.schemas import (
    AiActiveRequest,
    BoardOut,
    CardCreate,
    CardDetailOut,
    CardOut,
    CardServiceOut,
    CardServicesIn,
    CardUpdate,
    MoveRequest,
    QrEntryOut,
    SendRequest,
    SendResponse,
    ThreadMessage,
)
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.delivery_trigger import deliver_if_payment_validated
from server.modules.crm.services.entry_service import EntryService
from server.modules.crm.services.media_store import store_outbound_media
from server.modules.crm.services.opportunity_service import OpportunityService
from server.modules.crm.services.reply_service import ReplyService
from server.shared.exceptions import (
    ExternalServiceError,
    NotFoundException,
    ValidationException,
)
from server.shared.pubsub import crm_channel, publisher, subscribe

router = APIRouter(prefix="/crm", tags=["crm"])
router.include_router(contacts_router)  # /crm/contacts (ABM de contactos, #101)
router.include_router(payment_settings_router)  # /crm/payment-settings (config de pagos por org)
router.include_router(receipt_router)  # /crm/cards/{id}/receipt (validación del comprobante)
router.include_router(reconciliation_router)  # /crm/payments (conciliación de pagos)
router.include_router(event_router)  # /crm/agenda (ABM de eventos; /crm/events es el SSE)
router.include_router(redemption_router)  # /crm/entries (control de acceso en la puerta)

HEARTBEAT_SECONDS = 15.0


def _service(session: DbSession) -> BoardService:
    return BoardService(session=session, publisher=publisher)


async def _next_event(events: AsyncGenerator[str, None]) -> str:
    return await anext(events)


async def _event_stream(tenant_id: uuid.UUID) -> AsyncIterator[str]:
    """Frames SSE del canal del tenant; `: ping` como heartbeat cuando no hay tráfico."""
    events = subscribe(crm_channel(tenant_id))
    pending: asyncio.Task[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(_next_event(events))
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield ": ping\n\n"
                continue
            finished, pending = pending, None
            try:
                payload = finished.result()
            except StopAsyncIteration:
                return
            yield f"data: {payload}\n\n"
    finally:
        if pending is not None:
            # La cancelación entra al generador `subscribe`, que libera Redis en su finally.
            pending.cancel()
        else:
            await events.aclose()


@router.get("/events")
async def stream_events(session: DbSession, token: str | None = None) -> StreamingResponse:
    """Realtime del tablero por SSE (slice 2b), autenticado por `?token=<jwt>`.

    El JWT viaja por query param (mitigación: vida corta) y se valida igual que el
    Bearer. El stream emite solo el canal del tenant del token: sin fuga cross-tenant.
    """
    ctx = await get_current_context(session, token)
    await session.close()  # auth resuelta: no retener la conexión DB durante el stream
    return StreamingResponse(
        _event_stream(ctx.tenant.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/boards", response_model=BoardOut)
async def get_boards(ctx: CurrentUser, session: DbSession) -> BoardOut:
    return await _service(session).get_board(ctx.tenant.id)


@router.get("/cards/{card_id}", response_model=CardDetailOut)
async def get_card(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> CardDetailOut:
    detail = await _service(session).get_card_detail(card_id, ctx.tenant.id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")
    return detail


@router.post("/cards/{card_id}/move", response_model=CardOut)
async def move_card(
    card_id: uuid.UUID, payload: MoveRequest, ctx: CurrentUser, session: DbSession
) -> CardOut:
    try:
        card = await _service(session).move_card(
            card_id, payload.stage_id, ctx.user.id, ctx.tenant.id, payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")
    # Validar el pago es el disparador de la entrega: el operador arrastra la card a
    # "Pago validado" y el sistema entrega lo que corresponda (entrada QR o links) sin
    # que tenga que apretar nada más. El move ya está hecho y no se revierte si la
    # entrega no se puede completar: la card queda con su aviso.
    #
    # La respuesta es el `CardOut` del move humano. Si la entrega avanzó la card, el
    # estado final llega al front por el evento `card_moved` del realtime, igual que
    # cualquier otro move — no se re-proyecta acá.
    await deliver_if_payment_validated(
        session, card.stage_id, card_id, ctx.tenant.id, approved_by=ctx.user.id
    )
    return card


@router.post(
    "/cards/{card_id}/attended",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def mark_card_attended(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> None:
    """Marca la oportunidad como atendida: sale de la cola de "Requiere atención".

    Para la conversación que ya terminó y ninguna regla puede saberlo — el lead dijo
    "gracias" y no hay nada que responder. No cierra la oportunidad ni limpia sus avisos,
    y si el lead vuelve a escribir la señal se reenciende sola.

    Operación diaria del tablero: la pueden hacer los 3 roles, como mover una card.
    """
    if not await _service(session).mark_attended(card_id, ctx.tenant.id, ctx.user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")


@router.post("/cards", response_model=CardOut, status_code=status.HTTP_201_CREATED)
async def create_card(payload: CardCreate, ctx: CurrentUser, session: DbSession) -> CardOut:
    """Alta manual de oportunidad (#97): crea conversación (chat vacío) + card en el
    primer stage. Idempotente por teléfono."""
    svc = OpportunityService(session=session, publisher=publisher)
    try:
        return await svc.create(ctx.tenant.id, payload, ctx.user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/cards/{card_id}", response_model=CardOut)
async def update_card(
    card_id: uuid.UUID, payload: CardUpdate, ctx: CurrentUser, session: DbSession
) -> CardOut:
    """Edita nombre y/o notas de la oportunidad."""
    svc = OpportunityService(session=session, publisher=publisher)
    card = await svc.update(card_id, ctx.tenant.id, payload)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")
    return card


@router.put("/cards/{card_id}/services", response_model=list[CardServiceOut])
async def set_card_services(
    card_id: uuid.UUID, payload: CardServicesIn, ctx: CurrentUser, session: DbSession
) -> list[CardServiceOut]:
    """Asigna manualmente el set de servicios del catálogo a la oportunidad (#132)."""
    try:
        services = await _service(session).set_card_services(
            card_id, ctx.tenant.id, payload.service_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if services is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")
    return services


@router.delete(
    "/cards/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_card(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> None:
    """Baja de oportunidad: borrado duro (card + historial)."""
    svc = OpportunityService(session=session, publisher=publisher)
    deleted = await svc.delete(card_id, ctx.tenant.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card no encontrada")


@router.post("/cards/{card_id}/generate-entry", response_model=QrEntryOut)
async def generate_entry(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> QrEntryOut:
    svc = EntryService(session=session, publisher=publisher)
    try:
        entry = await svc.generate_entry(card_id, ctx.user.id, ctx.tenant.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    except ExternalServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    return QrEntryOut(card_id=entry.card_id, token=entry.token, qr_ref=entry.qr_ref)


@router.post("/cards/{card_id}/send", response_model=SendResponse)
async def send_human_reply(
    card_id: uuid.UUID, payload: SendRequest, ctx: CurrentUser, session: DbSession
) -> SendResponse:
    svc = ReplyService(session=session)
    try:
        message = await svc.send_human_reply(card_id, payload.text, ctx.user.id, ctx.tenant.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    except ExternalServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    return SendResponse(sender=message.sender, text=message.text, at=message.at)


def _send_response(message: ThreadMessage) -> SendResponse:
    return SendResponse(
        sender=message.sender,
        text=message.text,
        at=message.at,
        type=message.type,
        media_url=message.media_url,
    )


@router.post("/cards/{card_id}/send-media", response_model=SendResponse)
async def send_human_media(
    card_id: uuid.UUID,
    ctx: CurrentUser,
    session: DbSession,
    file: Annotated[UploadFile, File()],
    caption: Annotated[str, Form(max_length=1024)] = "",
) -> SendResponse:
    """Adjunto del takeover (#251): valida/persiste el archivo, lo envía por WhatsApp
    (imagen o documento según MIME) y lo espeja como mensaje humano."""
    if file.size is not None and file.size > MATERIAL_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo supera el límite de 5 MB"
        )
    data = await file.read()
    try:
        media = store_outbound_media(
            organization_id=ctx.tenant.id,
            content_type=file.content_type,
            filename=file.filename,
            data=data,
        )
        message = await ReplyService(session=session).send_human_media(
            card_id, media, caption.strip(), ctx.user.id, ctx.tenant.id
        )
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    except ExternalServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    return _send_response(message)


@router.post("/cards/{card_id}/send-qr", response_model=SendResponse)
async def send_payment_qr(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> SendResponse:
    """Envía manualmente el QR de pago configurado (misma imagen que usa el agente)."""
    try:
        message = await ReplyService(session=session).send_payment_qr(
            card_id, ctx.user.id, ctx.tenant.id
        )
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    except ExternalServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    return _send_response(message)


@router.put("/conversations/{conversation_id}/ai-active")
async def set_ai_active(
    conversation_id: uuid.UUID, payload: AiActiveRequest, ctx: CurrentUser, session: DbSession
) -> dict[str, bool]:
    ok = await _service(session).set_ai_active(conversation_id, payload.is_ai_active, ctx.tenant.id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="conversación no encontrada"
        )
    return {"is_ai_active": payload.is_ai_active}
