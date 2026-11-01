"""SQLAlchemy models.

Hierarchy is a strict tree: every node has exactly one parent and one ``path``.
Variables are temporal rows (``created_rev`` / ``deleted_rev``) so the whole
inventory can be read as of any revision.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PATH_SEP = "."


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


class Revision(Base):
    __tablename__ = "revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255), default="system")
    source: Mapped[str] = mapped_column(String(32), default="cli")  # ui|api|module|cli|import
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    message: Mapped[str | None] = mapped_column(Text)


class Node(Base):
    __tablename__ = "node"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_node_parent_name"),
        UniqueConstraint("path", name="uq_node_path"),
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

    parent: Mapped["Node | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Node"]] = relationship(back_populates="parent", cascade="all")
    variables: Mapped[list["NodeVariable"]] = relationship(back_populates="node", cascade="all")

    def ancestors_paths(self) -> list[str]:
        """Paths of self and every ancestor, root first."""
        parts = self.path.split(PATH_SEP)
        return [PATH_SEP.join(parts[: i + 1]) for i in range(len(parts))]


class VariableDefinition(Base):
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
    """Explicit targeting-only group. Tags never carry variables (enforced by having
    no variable table and by the exporter)."""

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class TagMember(Base):
    __tablename__ = "tag_member"

    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id", ondelete="CASCADE"), primary_key=True)
