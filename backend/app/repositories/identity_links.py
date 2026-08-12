"""Data access for `identity_links` (UC2's repositories layer).

The table is APPEND-ONLY (docs/plan.md D5): a link is never deleted or
re-pointed -- moving a mention between persons is `deactivate` (flip the
active flag, history row survives) plus `create` (a fresh active row on the
new person). The partial unique index `uq_identity_links_active_mention`
makes a double-active mention impossible at the DB level, so a racing
second writer fails loudly instead of corrupting the clustering."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IdentityLink


class IdentityLinkRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        person_id: uuid.UUID,
        mention_id: uuid.UUID,
        method: str,
        score: float | None = None,
        rule_id: str | None = None,
        rationale: str | None = None,
        agent_run_id: uuid.UUID | None = None,
        active: bool = True,
    ) -> IdentityLink:
        link = IdentityLink(
            person_id=person_id,
            mention_id=mention_id,
            score=score,
            method=method,
            rule_id=rule_id,
            rationale=rationale,
            agent_run_id=agent_run_id,
            active=active,
        )
        self.db.add(link)
        self.db.flush()  # populate link.id without committing yet
        return link

    def active_link_for_mention(self, mention_id: uuid.UUID) -> IdentityLink | None:
        return self.db.scalar(
            select(IdentityLink).where(
                IdentityLink.mention_id == mention_id, IdentityLink.active.is_(True)
            )
        )

    def active_links_for_person(self, person_id: uuid.UUID) -> list[IdentityLink]:
        return list(
            self.db.scalars(
                select(IdentityLink)
                .where(IdentityLink.person_id == person_id, IdentityLink.active.is_(True))
                .order_by(IdentityLink.created_at.asc(), IdentityLink.id.asc())
            )
        )

    def all_links_for_person(self, person_id: uuid.UUID) -> list[IdentityLink]:
        """Active AND historical rows -- the person detail ER panel shows
        the full replayable merge/split history (docs/plan.md D5)."""
        return list(
            self.db.scalars(
                select(IdentityLink)
                .where(IdentityLink.person_id == person_id)
                .order_by(IdentityLink.created_at.asc(), IdentityLink.id.asc())
            )
        )

    def deactivate(self, link: IdentityLink) -> None:
        """The only mutation this table permits: active -> False. The row
        itself stays forever (append-only history)."""
        link.active = False
        self.db.flush()
