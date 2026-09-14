# Spec — M-Meta-impl: media in/out + ventana 24h + HSM + statuses

> **Estado:** ✅ **Completado** — decisiones del análisis de findings ya incorporadas acá (ver BITACORA).
> Owner: Chris. Ref: `SPECS_MVP §M-Meta en detalle`.

## 1. Intent

Implementar las piezas Meta que destraban el e2e completo: reply-humano por WhatsApp,
envío de QRs (pago + entrada), y espejo de media en el CRM.

## 2. Decisiones tomadas (de findings)

| Gap | Decisión | Archivos |
|---|---|---|
| 1 — Media entrante | Download en webhook; ruta relativa en `AiChatHistory.message` | `webhook_service.py`, `whatsapp_service.py` |
| 2 — Media saliente | Envío by-link (no upload); `send_image`/`send_document`/`send_template` | `whatsapp_service.py` |
| 3 — Ventana 24h | Query a `ai_chat_histories`; `is_within_window()`; `OutsideWindowError` → 422 | `whatsapp_service.py`, `ai_chat_history_repository.py`, `shared/exceptions.py` |
| 4 — Statuses | **Fase 2** — no implementado | — |
| 5 — HSM templates | `send_template` implementado; crear `entry_qr_ready` en Business Manager | `whatsapp_service.py` |

## 3. Qué se implementó (M-Meta-impl)

### `whatsapp_schemas.py`
- `WAImage` — campos `id`, `mime_type`, `sha256`, `caption`
- `WADocument` — campos `id`, `mime_type`, `filename`, `caption`
- `WAIncomingMessage` — agrega `image: WAImage | None` y `document: WADocument | None`

### `whatsapp_service.py`
- `is_within_window(last_user_message_at)` — función standalone para detección de ventana
- `WhatsAppSender.send_image(to, link, caption)` — envío de imagen por link público
- `WhatsAppSender.send_document(to, link, filename, caption)` — envío de documento por link
- `WhatsAppSender.send_template(to, name, lang, components)` — envío de plantilla HSM
- `WhatsAppSender.download_media(media_id)` → `(bytes, mime_type)` — descarga media de Meta
- `WhatsAppSender._post(payload)` — helper interno compartido; detecta error 131047 → `OutsideWindowError`

### `webhook_service.py`
- `_get_or_create_conversation` — extraído como método privado (evita duplicación)
- `_handle_media(instance, wa_id, msg)` — descarga el media, lo persiste en disco, guarda ruta en `AiChatHistory.message`
- `_save_media(org_id, wamid, mime_type, content)` → ruta relativa — escribe en `MEDIA_ROOT/{org_id}/{wamid}.ext`
- `_MIME_EXT` — tabla de extensiones por mime type
- `_handle_change` — ruteado: `text` → `_handle_text`; `image`/`document` → `_handle_media`

### `ai_chat_history_repository.py`
- `get_last_user_message_at(thread_id)` → `datetime | None` — query de ventana 24h

### `shared/exceptions.py`
- `OutsideWindowError(ValidationException)` — → 422 automático vía handler existente

### `config.py`
- `media_root: str = "uploads"` — directorio de almacenamiento de media
- `media_base_url: str = "http://localhost:8000"` — base para URLs públicas de media

### `main.py`
- `os.makedirs(settings.media_root)` al crear la app
- `StaticFiles` montado en `/media` → sirve archivos de `media_root`

### `pyproject.toml`
- `aiofiles>=24.1.0` — requerido por `StaticFiles`

## 4. Formato de `AiChatHistory.message` para media entrante

```json
{
  "role": "user",
  "content": "[image: sin descripción]",
  "wamid": "wamid.xxx",
  "channel": "whatsapp",
  "media_type": "image",
  "media_path": "{org_id}/{wamid}.jpg",
  "media_caption": ""
}
```

URL pública del archivo: `{media_base_url}/media/{media_path}`

## 5. Contratos para integradores

### Ventana 24h (N — CRM `POST /crm/cards/{id}/send`)

```python
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.services.whatsapp_service import is_within_window
from server.shared.exceptions import OutsideWindowError

last_at = await history_repo.get_last_user_message_at(thread_id=str(conversation.id))
if not is_within_window(last_at):
    raise OutsideWindowError("Cannot send message: 24h customer service window has expired")
```

Respuesta al front: `422 {"detail": "Cannot send message: 24h customer service window has expired"}`

### Envío de QR (agente + generate-entry)

```python
sender = WhatsAppSender()
# QR generado y servido en /media/{path}
qr_url = f"{settings.media_base_url}/media/{qr_relative_path}"
await sender.send_image(to=lead_phone, link=qr_url, caption="Tu código QR")
```

### Template HSM `entry_qr_ready` (pendiente crear en Business Manager)

```python
await sender.send_template(
    to=lead_phone,
    name="entry_qr_ready",
    lang="es_AR",
    components=[
        {"type": "header", "parameters": [{"type": "image", "image": {"link": qr_url}}]},
        {"type": "body", "parameters": [{"type": "text", "text": lead_name}]},
    ],
)
```

## 6. Pendiente (no bloquea e2e)

- **Crear template `entry_qr_ready` en Meta Business Manager** — hacerlo antes de integrar `generate-entry`.
- **Statuses (delivery/read)** — Fase 2, `webhookservice` ignora el array `statuses`.
- **Campo `is_within_window` en respuesta del card** — lo agrega N en `GET /crm/cards/{id}`.
