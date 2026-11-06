"""``json_schema`` on variable definitions, enforced at write time.

A definition's schema validates every value *stored* under that key: each node's own
contribution, not the folded result a host ends up with (for ``append`` a list that
validates at every level also validates folded; for ``merge`` a partial dict at a deeper
level is the normal case, so a schema should not mark its keys ``required``). A
``!vault`` value is checked in the JSON form it is stored in.

Enforced on every path that writes a value or a schema: ``PUT …/vars/{key}``, the
``vars`` of ``POST /nodes``, the importer (CLI and ``POST /import``) and
``PUT /variables/{name}``, which also re-checks every live value of that key so a new
schema can never leave the tree in a state it rejects. A profile's values (issue #22) are
contributions like a node's and are checked the same way, named ``profile:<name>``: the
check does not depend on where a value applies, so attaching a profile can never make one
of its values fail (there is nothing to re-check on attach). Draft 2020-12 unless the schema
names another ``$schema``; ``format`` is not asserted.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, exceptions, validators
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.models import Node, NodeVariable, ProfileVariable, VariableDefinition

MAX_REPORTED = 5


class SchemaViolation(ValueError):
    pass


def _validator(schema: dict[str, Any]):
    cls = validators.validator_for(schema, default=Draft202012Validator)
    return cls(schema)


def check_schema(schema: dict[str, Any]) -> None:
    """Raise ``SchemaViolation`` when ``schema`` is not a valid JSON Schema."""
    cls = validators.validator_for(schema, default=Draft202012Validator)
    try:
        cls.check_schema(schema)
    except exceptions.SchemaError as exc:
        raise SchemaViolation(f"invalid json_schema: {exc.message}") from exc


def problem(schema: dict[str, Any] | None, value: Any) -> str | None:
    """The first validation error of ``value`` against ``schema``, or None."""
    if not schema:
        return None
    error = exceptions.best_match(_validator(schema).iter_errors(value))
    if error is None:
        return None
    where = "".join(f"[{p!r}]" for p in error.absolute_path)
    return f"{where}: {error.message}" if where else error.message


def check_value(session: Session, key: str, value: Any, path: str) -> None:
    """Refuse one value about to be stored at ``path``."""
    definition = session.get(VariableDefinition, key)
    msg = problem(definition.json_schema if definition else None, value)
    if msg is not None:
        raise SchemaViolation(f"{key} at {path} does not match its json_schema: {msg}")


def check_live(session: Session, *, keys: set[str] | None = None, created_rev: int | None = None) -> None:
    """Re-check live values against their definitions' schemas: every live value of
    ``keys``, plus every value written under ``created_rev``. Reports up to
    ``MAX_REPORTED`` offenders in one ``SchemaViolation``."""
    schemas = {d.name: d.json_schema for d in session.scalars(select(VariableDefinition)) if d.json_schema}
    wanted = {k for k in schemas if (keys is not None and k in keys)}
    if not schemas or (not wanted and created_rev is None):
        return
    session.flush()
    stmt = (
        select(NodeVariable.key, NodeVariable.value, NodeVariable.created_rev, Node.path)
        .join(Node, Node.id == NodeVariable.node_id)
        .where(NodeVariable.deleted_rev.is_(None), NodeVariable.key.in_(list(schemas)))
        .order_by(Node.path, NodeVariable.key)
    )
    bad: list[str] = []
    for key, value, rev, path in session.execute(stmt):
        if key not in wanted and rev != created_rev:
            continue
        msg = problem(schemas[key], value)
        if msg is not None:
            bad.append(f"{key} at {path}: {msg}")
            if len(bad) >= MAX_REPORTED:
                break
    if len(bad) < MAX_REPORTED:
        pstmt = (
            select(ProfileVariable.key, ProfileVariable.value, ProfileVariable.created_rev, ProfileVariable.profile)
            .where(ProfileVariable.deleted_rev.is_(None), ProfileVariable.key.in_(list(schemas)))
            .order_by(ProfileVariable.profile, ProfileVariable.key)
        )
        for key, value, rev, name in session.execute(pstmt):
            if key not in wanted and rev != created_rev:
                continue
            msg = problem(schemas[key], value)
            if msg is not None:
                bad.append(f"{key} at profile:{name}: {msg}")
                if len(bad) >= MAX_REPORTED:
                    break
    if bad:
        raise SchemaViolation("values do not match their json_schema: " + "; ".join(bad))
