from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class WAText(_Permissive):
    body: str


class WAImage(_Permissive):
    id: str
    mime_type: str = ""
    sha256: str = ""
    caption: str = ""


class WADocument(_Permissive):
    id: str
    mime_type: str = ""
    filename: str = ""
    caption: str = ""


class WAIncomingMessage(_Permissive):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str
    text: WAText | None = None
    image: WAImage | None = None
    document: WADocument | None = None


class WAContact(_Permissive):
    profile: dict[str, str] = {}
    wa_id: str


class WAMetadata(_Permissive):
    display_phone_number: str
    phone_number_id: str


class WAValue(_Permissive):
    messaging_product: str
    metadata: WAMetadata
    contacts: list[WAContact] = []
    messages: list[WAIncomingMessage] = []
    statuses: list[dict[str, object]] = []


class WAChange(_Permissive):
    value: WAValue
    field: str


class WAEntry(_Permissive):
    id: str
    changes: list[WAChange]


class WAWebhookPayload(_Permissive):
    object: str
    entry: list[WAEntry]
