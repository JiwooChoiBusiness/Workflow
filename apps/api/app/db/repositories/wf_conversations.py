"""챗플로우 대화·메시지(2단계 설계 4.1의 wf_conversation·wf_message) 리포지토리."""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.db.models import WfConversation, WfMessage
from app.db.models.enums import MessageRole, RunStatus
from app.db.repositories.base import WorkspaceScopedRepository


class WfConversationRepository(WorkspaceScopedRepository[WfConversation]):
    # 지운(deleted_at) 대화는 런·메시지 로그를 위해 남는다. 조회는 기본으로 살아 있는 행만 본다.
    model = WfConversation

    async def get_active(self, conversation_id: uuid.UUID) -> WfConversation | None:
        return await self._scalar(
            self._select().where(
                WfConversation.id == conversation_id, WfConversation.deleted_at.is_(None)
            )
        )

    async def list_filtered(
        self,
        *,
        workflow_id: uuid.UUID | None = None,
        source: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        non_empty: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WfConversation]:
        statement = self._select().where(WfConversation.deleted_at.is_(None))
        if non_empty:
            statement = statement.where(WfConversation.message_count > 0)
        if workflow_id is not None:
            statement = statement.where(WfConversation.workflow_id == workflow_id)
        if source is not None:
            statement = statement.where(WfConversation.source == source)
        if api_key_id is not None:
            statement = statement.where(WfConversation.api_key_id == api_key_id)
        if external_user is not None:
            statement = statement.where(WfConversation.external_user == external_user)
        if since is not None:
            statement = statement.where(WfConversation.created_at >= since)
        if until is not None:
            statement = statement.where(WfConversation.created_at < until)
        return await self._scalars(
            statement.order_by(
                WfConversation.last_message_at.desc().nulls_last(),
                WfConversation.created_at.desc(),
                WfConversation.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

    async def deleted_ids(self, conversation_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        """주어진 id 중 지운 대화. 행이 없는 id는 빠진다."""
        if not conversation_ids:
            return set()
        rows = await self._scalars(
            self._select(WfConversation.id).where(
                WfConversation.id.in_(list(conversation_ids)),
                WfConversation.deleted_at.is_not(None),
            )
        )
        return set(rows)

    async def live_ids(self, workflow_ids: Collection[uuid.UUID]) -> list[uuid.UUID]:
        """워크플로우들의 지우지 않은 대화 id."""
        if not workflow_ids:
            return []
        return await self._scalars(
            self._select(WfConversation.id).where(
                WfConversation.workflow_id.in_(list(workflow_ids)),
                WfConversation.deleted_at.is_(None),
            )
        )

    def add(self, conversation: WfConversation) -> WfConversation:
        return self._add(conversation)

    async def flush(self) -> None:
        await self._flush()


class WfMessageRepository(WorkspaceScopedRepository[WfMessage]):
    model = WfMessage

    async def next_seq(self, conversation_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(WfMessage.seq)).where(
                WfMessage.conversation_id == conversation_id
            )
        )
        return (current or 0) + 1

    async def list_page(
        self, conversation_id: uuid.UUID, *, before_seq: int | None, limit: int
    ) -> list[WfMessage]:
        """before_seq보다 앞의 메시지 limit개를 seq 오름차순으로."""
        statement = self._select().where(WfMessage.conversation_id == conversation_id)
        if before_seq is not None:
            statement = statement.where(WfMessage.seq < before_seq)
        rows = await self._scalars(statement.order_by(WfMessage.seq.desc()).limit(limit))
        return list(reversed(rows))

    async def user_for_run(self, wf_run_id: uuid.UUID) -> WfMessage | None:
        return await self._scalar(
            self._select().where(
                WfMessage.wf_run_id == wf_run_id, WfMessage.role == MessageRole.USER
            )
        )

    async def last_user(self, conversation_id: uuid.UUID) -> WfMessage | None:
        return await self._scalar(
            self._select()
            .where(WfMessage.conversation_id == conversation_id, WfMessage.role == MessageRole.USER)
            .order_by(WfMessage.seq.desc())
            .limit(1)
        )

    async def latest_user_inputs(
        self, conversation_ids: Collection[uuid.UUID]
    ) -> list[dict[str, Any]]:
        """대화마다 마지막 user 메시지의 inputs. 다음 메시지가 inputs를 비우면 이 값을 다시 쓴다."""
        if not conversation_ids:
            return []
        earlier = aliased(WfMessage)
        newest = (
            select(func.max(earlier.seq))
            .where(
                earlier.workspace_id == self.workspace_id,
                earlier.conversation_id == WfMessage.conversation_id,
                earlier.role == MessageRole.USER,
            )
            .scalar_subquery()
        )
        rows = await self._scalars(
            self._select(WfMessage.inputs).where(
                WfMessage.conversation_id.in_(list(conversation_ids)),
                WfMessage.role == MessageRole.USER,
                WfMessage.seq == newest,
            )
        )
        return [row for row in rows if row]

    async def user_count(self, conversation_id: uuid.UUID, *, up_to_seq: int) -> int:
        """up_to_seq까지의 user 행 수. 그 메시지가 대화의 몇 번째 턴인지(sys.turn)다."""
        count = await self._scalar(
            self._select(func.count(WfMessage.id)).where(
                WfMessage.conversation_id == conversation_id,
                WfMessage.role == MessageRole.USER,
                WfMessage.seq <= up_to_seq,
            )
        )
        return int(count or 0)

    async def history(
        self, conversation_id: uuid.UUID, *, before_seq: int, turns: int
    ) -> list[tuple[WfMessage, WfMessage]]:
        """before_seq 앞의 성공한 턴 turns개를 (user, assistant)로, 오래된 것부터.

        답 없이 끝난 턴(실패·취소)은 모델에 질문만 보이게 되므로 뺀다.
        """
        if turns <= 0:
            return []
        answers = await self._scalars(
            self._select()
            .where(
                WfMessage.conversation_id == conversation_id,
                WfMessage.role == MessageRole.ASSISTANT,
                WfMessage.status == RunStatus.SUCCEEDED,
                WfMessage.seq < before_seq,
                WfMessage.wf_run_id.is_not(None),
            )
            .order_by(WfMessage.seq.desc())
            .limit(turns)
        )
        run_ids = [row.wf_run_id for row in answers]
        if not run_ids:
            return []
        users = await self._scalars(
            self._select().where(
                WfMessage.conversation_id == conversation_id,
                WfMessage.role == MessageRole.USER,
                WfMessage.wf_run_id.in_(run_ids),
            )
        )
        by_run = {row.wf_run_id: row for row in users}
        pairs = [(by_run[row.wf_run_id], row) for row in answers if row.wf_run_id in by_run]
        return list(reversed(pairs))

    def add(self, message: WfMessage) -> WfMessage:
        return self._add(message)

    async def flush(self) -> None:
        await self._flush()
