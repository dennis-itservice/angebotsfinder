from typing import Any

from pydantic import BaseModel


class PushSubscribeRequest(BaseModel):
    subscription: dict[str, Any]
    label: str = ""


class PushEndpointRequest(BaseModel):
    endpoint: str | None = None
