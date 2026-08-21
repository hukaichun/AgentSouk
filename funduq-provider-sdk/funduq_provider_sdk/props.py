from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KyokForwardedProps(BaseModel):
    """funduq's `forwardedProps.kyok` entry: the grant a KYOK-bound run's agent presents when calling for completions.

    The independent twin of `funduq.kyok.KyokForwardedProps`, pinned by the
    same delivered-run frame. The token is opaque — carry it, sign over it
    (`kyok_call_payload`), never parse it.
    """

    model_config = ConfigDict(frozen=True)

    token: str
