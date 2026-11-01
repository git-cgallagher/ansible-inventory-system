"""Jinja evaluation of resolved variables with ansible-core's ``Templar``.

The resolved vars (what the inventory emits) plus mock facts are templated with the
same engine a playbook uses. ansible-core 2.19+ only templates strings tagged as
trusted, so every string from the database is marked ``trust_as_template`` first:
inventory values are trusted input, exactly as they are when the plugin feeds them
to Ansible.

Precedence follows Ansible: facts override inventory vars, and the magic vars
(``inventory_hostname``, ``group_names`` …) always win. Errors are reported per key
in ``errors`` and never fail the whole render.
"""

from __future__ import annotations

import json
from typing import Any

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from ansible.utils.vars import combine_vars

MOCK_HOSTNAME = "mock-host"


def _trust(value: Any) -> Any:
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {k: _trust(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trust(v) for v in value]
    return value


def _plain(value: Any) -> Any:
    """Strip ansible's tagged types so the result is plain JSON."""
    return json.loads(json.dumps(value, default=str))


def _error(exc: BaseException) -> dict[str, str]:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return {"type": type(exc).__name__, "message": message}


def magic_vars(hostname: str, chain: list[str]) -> dict[str, Any]:
    groups = [g for g in chain if g != hostname]
    return {
        "inventory_hostname": hostname,
        "inventory_hostname_short": hostname.split(".")[0],
        "group_names": [g for g in groups if g != "all"],
    }


def render_templates(
    resolved: dict[str, Any],
    *,
    facts: dict[str, Any] | None = None,
    hostname: str = MOCK_HOSTNAME,
    chain: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Return ``(rendered, errors)``: every resolved key templated, or an error."""
    facts = facts or {}
    fact_vars: dict[str, Any] = {"ansible_facts": facts}
    for key, value in facts.items():
        fact_vars[key if key.startswith("ansible_") else f"ansible_{key}"] = value

    trusted = _trust(resolved)
    variables = combine_vars(trusted, _trust(fact_vars))
    variables = combine_vars(variables, magic_vars(hostname, chain or []))

    templar = Templar(loader=DataLoader(), variables=variables)
    rendered: dict[str, Any] = {}
    errors: dict[str, dict[str, str]] = {}
    for key, value in trusted.items():
        try:
            rendered[key] = _plain(templar.template(value))
        except AnsibleError as exc:
            errors[key] = _error(exc)
        except Exception as exc:  # noqa: BLE001 - a bad template must never fail the render
            errors[key] = _error(exc)
    return rendered, errors
