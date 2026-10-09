"""The approval broker: turns a CONFIRM verdict into a card the owner can tap.

A CONFIRM never runs the tool. ``propose`` freezes the arguments and the
provenance into the store, and the owner's tap is the only thing that can
approve them. The callback data carries only a short id and nonce, so it fits
Telegram's limit and reveals nothing useful if it is logged.

Two lifetimes: an interactive card (the owner is in the chat) expires after 5
minutes; a card raised by an unattended run expires after 30 minutes, which
gives the owner time to see it when they next look at their phone.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.types import CallContext, Verdict
from ..store import Approval, Store
from ..store.approval_rows import ID_LEN, NONCE_LEN

if TYPE_CHECKING:  # pragma: no cover
    from ..tools.registry import ToolSpec

INTERACTIVE_TTL_S = 5 * 60
UNATTENDED_TTL_S = 30 * 60

_YES = "y"
_NO = "n"


class ApprovalBroker:
    def __init__(self, store: Store) -> None:
        self._store = store

    def propose(self, chat_id: int, spec: "ToolSpec", args: dict[str, Any], verdict: Verdict,
                ctx: CallContext) -> Approval:
        """Freeze one call for the owner to approve. Nothing runs until ``consume``."""
        interactive = ctx.actor == "owner" and ctx.chat_id is not None
        return self._store.approval_create(
            chat_id=chat_id,
            tool=spec.name,
            args=dict(args),
            summary=verdict.summary or spec.name,
            provenance=int(ctx.provenance),
            autonomy=ctx.autonomy.value,
            generation=ctx.generation,
            two_channel=verdict.two_channel,
            ttl_s=INTERACTIVE_TTL_S if interactive else UNATTENDED_TTL_S,
        )

    def buttons(self, approval: Approval) -> list[list[dict[str, str]]]:
        """An inline keyboard with Yes and No. Callback data is 24 bytes: ap:<id>:<nonce>:<y|n>."""
        return [[
            {"text": "Ha", "callback_data": _callback(approval, _YES)},
            {"text": "Yo'q", "callback_data": _callback(approval, _NO)},
        ]]

    @staticmethod
    def parse_callback(data: str) -> tuple[str, str, bool] | None:
        """Split callback data into (approval id, nonce, approve). None when it is not ours."""
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "ap" or parts[3] not in (_YES, _NO):
            return None
        approval_id, nonce = parts[1], parts[2]
        if len(approval_id) != ID_LEN or len(nonce) != NONCE_LEN:
            return None
        return approval_id, nonce, parts[3] == _YES

    def consume(self, approval_id: str, nonce: str, actor_id: int, owner_id: int | None) -> Approval | None:
        """The owner's "yes". Returns the approval exactly once; every other attempt gets None."""
        return self._store.approval_consume(approval_id, nonce, actor_id, owner_id)

    def decline(self, approval_id: str, nonce: str, actor_id: int, owner_id: int | None) -> bool:
        return self._store.approval_decline(approval_id, nonce, actor_id, owner_id)

    def local_approve(self, approval_id: str) -> bool:
        """The desktop half of a two-channel approval. Called only from the local UI."""
        return self._store.approval_local_approve(approval_id)


def _callback(approval: Approval, answer: str) -> str:
    return f"ap:{approval.id}:{approval.nonce}:{answer}"
