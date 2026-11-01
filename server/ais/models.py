"""SQLAlchemy models.

Hierarchy is a strict tree: every node has exactly one parent and one ``path``.
Nodes and variables are temporal rows (``created_rev`` / ``deleted_rev``) so the
whole inventory can be read as of any revision. Nothing is updated in place or
hard-deleted: a change closes the old row and opens a new one under a new
``Revision``.

The schema is owned by the Alembic migrations in ``ais/migrations``; these models
must stay in step with the latest migration (``ais init-db`` runs the migrations,
not ``create_all``).
"""

from __future__ import annotations

import datetime as dt
import enum
import re

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PATH_SEP = "."

# Node names must be valid ``ltree`` labels (PostgreSQL 16+: letters, digits,
# ``_`` and ``-``) so the generated ltree column on PostgreSQL always accepts them.
# ``.`` is the path separator and is therefore never part of a name.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class NodeKind(str, enum.Enum):
    all = "all"
    business = "business"
    tier = "tier"
    datacenter = "datacenter"
    group = "group"
    host = "host"


# Ordered level schema. ``group`` may repeat (free-form nesting below datacenter);
# ``host`` is always a leaf. A child's kind must be at least as deep as its
# parent's next level.
LEVEL_ORDER: list[NodeKind] = [
    NodeKind.all,
    NodeKind.business,
    NodeKind.tier,
    NodeKind.datacenter,
    NodeKind.group,
    NodeKind.host,
]

# Kinds that get an automatic var-less tag group (``<kind>_<name>``) for targeting.
TAGGED_KINDS = {NodeKind.business, NodeKind.tier, NodeKind.datacenter}


class MergeStrategy(str, enum.Enum):
    replace = "replace"
    merge = "merge"
    append = "append"
    unique_append = "unique_append"


class Role(str, enum.Enum):
    viewer = "viewer"
    editor = "editor"
    admin = "admin"


ROLE_RANK = {Role.viewer: 0, Role.editor: 1, Role.admin: 2}


class Revision(Base):
    __tablename__ = "revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255), default="system")
    source: Mapped[str] = mapped_column(String(32), default="cli")  # ui|api|module|cli|import
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    message: Mapped[str | None] = mapped_column(Text)


_LIVE = text("deleted_rev IS NULL")


class Node(Base):
    """Tree node. Live when ``deleted_rev`` is NULL; ``created_rev`` NULL means
    "has always existed" (rows from before node temporality)."""

    __tablename__ = "node"
    __table_args__ = (
        Index("uq_node_parent_name_live", "parent_id", "name", unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE),
        Index("uq_node_path_live", "path", unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE),
        Index("ix_node_path_prefix", "path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"))
    kind: Mapped[NodeKind] = mapped_column(Enum(NodeKind, native_enum=False, length=16))
    name: Mapped[str] = mapped_column(String(255))
    # Optional override for the emitted Ansible group name (non-host nodes).
    inventory_name: Mapped[str | None] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)  # all.commercial.prod.dc1.web
    depth: Mapped[int] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_rev: Mapped[int | None] = mapped_column(ForeignKey("revision.id", name="fk_node_created_rev"))
    deleted_rev: Mapped[int | None] = mapped_column(ForeignKey("revision.id", name="fk_node_deleted_rev"))

    parent: Mapped["Node | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Node"]] = relationship(back_populates="parent", cascade="all")
    variables: Mapped[list["NodeVariable"]] = relationship(back_populates="node", cascade="all")

    def ancestors_paths(self) -> list[str]:
        """Paths of self and every ancestor, root first."""
        parts = self.path.split(PATH_SEP)
        return [PATH_SEP.join(parts[: i + 1]) for i in range(len(parts))]

    @property
    def is_live(self) -> bool:
        return self.deleted_rev is None


class VariableDefinition(Base):
    """Per-variable declaration. Not temporal: changing a strategy applies to every
    revision's export (documented gotcha)."""

    __tablename__ = "variable_definition"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    merge_strategy: Mapped[MergeStrategy] = mapped_column(
        Enum(MergeStrategy, native_enum=False, length=16), default=MergeStrategy.replace
    )
    description: Mapped[str | None] = mapped_column(Text)
    json_schema: Mapped[dict | None] = mapped_column(JSON)


class NodeVariable(Base):
    """Temporal variable row. Live when ``deleted_rev`` is NULL."""

    __tablename__ = "node_variable"
    __table_args__ = (
        Index("ix_nodevar_node_key_live", "node_id", "key", "deleted_rev"),
        CheckConstraint("deleted_rev IS NULL OR deleted_rev > created_rev", name="ck_nodevar_revs"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[object] = mapped_column(JSON)
    created_rev: Mapped[int] = mapped_column(ForeignKey("revision.id"))
    deleted_rev: Mapped[int | None] = mapped_column(ForeignKey("revision.id"))

    node: Mapped[Node] = relationship(back_populates="variables")


class Tag(Base):
    """Explicit targeting-only group. Tags never carry variables: there is no
    variable table for them, no API route accepts one, and the exporter emits them
    var-less."""

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class TagMember(Base):
    __tablename__ = "tag_member"

    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"), primary_key=True)


# ------------------------------------------------------------------------ auth


class User(Base):
    """Local or OIDC user. ``password_hash`` is NULL for token-only (service) users
    and for OIDC users. Table is ``user_account`` because ``user`` is reserved in
    PostgreSQL."""

    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    auth_source: Mapped[str] = mapped_column(String(16), default="local")  # local|oidc
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16), default=Role.viewer)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiToken(Base):
    """Bearer token, hashed at rest (SHA-256 of the full token; tokens are random
    256-bit secrets so a fast hash is appropriate). ``role`` caps the user's role
    for this token when set. ``kind`` is ``personal`` (long-lived API token) or
    ``session`` (issued by local login, short-lived)."""

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), default="token")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # first chars, for display only
    kind: Mapped[str] = mapped_column(String(16), default="personal")
    role: Mapped[Role | None] = mapped_column(Enum(Role, native_enum=False, length=16))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="tokens")


# -------------------------------------------------------------------- audit


class AuditLog(Base):
    """One row per effective write or auth event, written in the same transaction
    as the change it records (a refused write leaves no row). ``before``/``after``
    are JSON documents of the target as it was and as it became; ``target_id`` is a
    string because targets range from node ids to e-mail addresses. ``revision`` links
    an inventory write to its ``Revision``; auth events have none."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_ts", "ts"),
        Index("ix_audit_actor", "actor"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))  # node.create, variable.set, login.failure, ...
    target_type: Mapped[str] = mapped_column(String(32))  # node, node_variable, variable_definition, tree, user, token
    target_id: Mapped[str] = mapped_column(String(512))
    before: Mapped[object | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    after: Mapped[object | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    source: Mapped[str] = mapped_column(String(32), default="api")  # api|module|cli|login|import
    request_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int | None] = mapped_column(ForeignKey("revision.id", name="fk_audit_revision"))


# -------------------------------------------------------------------- snapshots


class HostSnapshot(Base):
    """Materialized resolved vars of one host at one revision (written on
    render/export, deduplicated per host+revision; ``resolved_hash`` lets diffs
    short-circuit)."""

    __tablename__ = "host_snapshot"
    __table_args__ = (UniqueConstraint("host_node_id", "revision", name="uq_snapshot_host_rev"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    host_node_id: Mapped[int] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"))
    host_name: Mapped[str] = mapped_column(String(255), index=True)
    revision: Mapped[int] = mapped_column(ForeignKey("revision.id"))
    resolved: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    resolved_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
