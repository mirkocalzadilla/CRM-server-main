from datetime import UTC, datetime, timedelta

import httpx

from server.config import get_settings
from server.modules.agent.domain.whatsapp_format import to_whatsapp_text
from server.shared.exceptions import OutsideWindowError
from server.shared.logger import get_logger

logger = get_logger(__name__)

_WINDOW_HOURS = 24
_META_ERROR_OUTSIDE_WINDOW = 131047


def is_within_window(last_user_message_at: datetime | None) -> bool:
    """True if a free-form WhatsApp message can be sent (within 24h window)."""
    if last_user_message_at is None:
        return False
    return (datetime.now(UTC) - last_user_message_at) < timedelta(hours=_WINDOW_HOURS)


class WhatsAppSender:
    """HTTP client for sending messages and downloading media via Meta Cloud API."""

    async def send_text(self, to: str, body: str) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": to_whatsapp_text(body)},
            }
        )
        logger.info("whatsapp.sent", to=to, type="text")

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        image: dict[str, object] = {"link": link}
        if caption:
            image["caption"] = caption
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "image",
                "image": image,
            }
        )
        logger.info("whatsapp.sent", to=to, type="image")

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        document: dict[str, object] = {"link": link, "filename": filename}
        if caption:
            document["caption"] = caption
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": document,
            }
        )
        logger.info("whatsapp.sent", to=to, type="document")

    async def send_template(
        self,
        to: str,
        name: str,
        lang: str,
        components: list[dict[str, object]],
    ) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": lang},
                    "components": components,
                },
            }
        )
        logger.info("whatsapp.sent", to=to, type="template", template=name)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download media binary from Meta. Returns (content, mime_type)."""
        settings = get_settings()
        headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
        base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            info_resp = await client.get(f"{base}/{media_id}", headers=headers)
            info_resp.raise_for_status()
            info = info_resp.json()
            media_url: str = info["url"]
            mime_type: str = info.get("mime_type", "application/octet-stream")
            content_resp = await client.get(media_url, headers=headers)
            content_resp.raise_for_status()
        logger.info("whatsapp.media_downloaded", media_id=media_id, mime_type=mime_type)
        return content_resp.content, mime_type

    async def _post(self, payload: dict[str, object]) -> None:
        settings = get_settings()
        headers = {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
        if response.status_code == 400:
            # Un 400 sin cuerpo JSON (proxy, error de infraestructura) no debe convertir
            # un fallo de envío en un 500 por JSONDecodeError: se trata como un 400
            # cualquiera y `raise_for_status` lo reporta como lo que es.
            try:
                payload_error = response.json().get("error", {})
            except ValueError:
                payload_error = {}
            if (
                isinstance(payload_error, dict)
                and payload_error.get("code") == _META_ERROR_OUTSIDE_WINDOW
            ):
                raise OutsideWindowError(
                    "Cannot send message: 24h customer service window has expired"
                )
        response.raise_for_status()
