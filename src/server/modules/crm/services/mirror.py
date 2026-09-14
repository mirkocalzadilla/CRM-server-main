"""Merge del hilo espejo (read model, §"M-CRM"): une las 3 fuentes en orden temporal.

`ai_chat_histories` role `user` → lead; role `assistant` → agente; role `system` →
evento de error del agente (server#288); `app_chat_histories` → humano. Función pura
sobre las filas ya leídas (testeable sin DB).
"""

from __future__ import annotations

from server.modules.agent.domain.models import AiChatHistory, AppChatHistory
from server.modules.crm.api.schemas import ThreadMessage


def _ai_to_message(row: AiChatHistory, media_base_url: str) -> ThreadMessage:
    msg = row.message
    role = str(msg.get("role", ""))
    if role == "system":
        # El agente falló este turno (server#288): chip de error en el hilo, con la
        # categoría como código (el CRM la traduce; card_flags philosophy).
        return ThreadMessage(
            sender="system",
            text=str(msg.get("content", "")),
            at=row.created_at,
            type="error",
            category=str(msg.get("category", "")) or None,
        )
    sender = "lead" if role == "user" else "agent"
    text = str(msg.get("content", ""))
    media_type = str(msg.get("media_type", ""))
    if media_type in ("image", "document"):
        # Saliente (#175): URL absoluta del catálogo/QR, se usa tal cual. Entrante:
        # `media_path` relativo servido desde el mount `/media` con la base inyectada.
        media_url = msg.get("media_url")
        media_path = msg.get("media_path")
        if media_url:
            url = str(media_url)
        elif media_path:
            url = f"{media_base_url}/media/{media_path}"
        else:
            url = None
        if url:
            return ThreadMessage(
                sender=sender,
                text=text,
                at=row.created_at,
                type=media_type,
                media_url=url,
            )

    return ThreadMessage(sender=sender, text=text, at=row.created_at)


def _app_to_message(row: AppChatHistory) -> ThreadMessage:
    # Media saliente del takeover (#251): URL absoluta ya resuelta al enviar.
    if row.media_type in ("image", "document") and row.media_url:
        return ThreadMessage(
            sender="human",
            text=row.message,
            at=row.message_time,
            type=row.media_type,
            media_url=row.media_url,
        )
    return ThreadMessage(sender="human", text=row.message, at=row.message_time)


def build_thread(
    ai_rows: list[AiChatHistory], app_rows: list[AppChatHistory], media_base_url: str
) -> list[ThreadMessage]:
    """Une mensajes de IA y humanos, ordenados por timestamp ascendente.

    `media_base_url` se inyecta (no se lee de settings aquí) para mantener la función pura.
    """
    messages = [_ai_to_message(row, media_base_url) for row in ai_rows]
    messages += [_app_to_message(row) for row in app_rows]
    messages.sort(key=lambda m: m.at)
    return messages
