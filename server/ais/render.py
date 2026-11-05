"""Jinja evaluation of resolved variables with ansible-core's ``Templar``.

The resolved vars (what the inventory emits) plus mock facts are templated with the
same engine a playbook uses. ansible-core 2.19+ only templates strings tagged as
trusted, so every string from the database is marked ``trust_as_template`` first:
inventory values are trusted input, exactly as they are when the plugin feeds them
to Ansible.

Precedence follows Ansible: facts override inventory vars, and the magic vars
(``inventory_hostname``, ``group_names`` …) always win. Errors are reported per key
in ``errors`` and never fail the whole render.

**The render runs on the API server, for any viewer**, with values a viewer can supply
(``overrides``), so it must never reach the server itself. Ansible's own sandbox stops
attribute escapes (``''.__class__``); on top of it the render's environment:

- runs no lookups (``lookup``, ``query``, ``q``): ``pipe`` would be a shell on the server;
- refuses the builtin filters and tests that read the server's environment or files
  (``SERVER_FILTERS``, ``SERVER_TESTS``) and every collection filter or test (they are
  not on the server and are not audited), however they are spelled or called
  (``map('expandvars')`` too);
- has no template loader, so ``{% include %}``/``{% import %}`` find nothing;
- never templates the request's ``facts``: Ansible does not template facts either.

A refused call is that key's error (``RenderRefused``); a playbook on the controller
still evaluates the same value normally.
"""

from __future__ import annotations

import json
from typing import Any

from collections.abc import Callable, Iterator, MutableMapping

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from ansible.utils.vars import combine_vars
from jinja2 import DictLoader

MOCK_HOSTNAME = "mock-host"

LOOKUP_GLOBALS = ("lookup", "query", "q")
# Builtin plugins that read the server's environment, home, cwd or files.
SERVER_FILTERS = frozenset({"expanduser", "expandvars", "fileglob", "realpath", "relpath", "vault", "unvault"})
SERVER_TESTS = frozenset({
    "directory", "is_dir", "file", "is_file", "link", "is_link", "exists", "link_exists",
    "same_file", "is_same_file", "mount", "is_mount", "vaulted_file",
})
_BUILTIN_PREFIXES = ("ansible.builtin.", "ansible.legacy.")


class RenderRefused(AnsibleError):
    """A template asked for something the server render never runs."""


def _refuse(what: str) -> Callable[..., Any]:
    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise RenderRefused(f"{what} does not run in the server render (it would run on the API server); a playbook still runs it")

    return refused


class _GuardedPlugins(MutableMapping):
    """Jinja's ``filters``/``tests`` mapping with server-touching plugins refused.

    Every use goes through ``__getitem__`` (``x | f``, ``x is t``, ``map('f')``), so a
    refused name resolves to a function that raises when called."""

    def __init__(self, inner: MutableMapping, kind: str, denied: frozenset[str]):
        self._inner, self._kind, self._denied = inner, kind, denied

    def __getitem__(self, key: str) -> Callable[..., Any]:
        name = key
        for prefix in _BUILTIN_PREFIXES:
            if key.startswith(prefix):
                name = key[len(prefix):]
        if "." in name:
            return _refuse(f"the collection {self._kind} {key!r}")
        if name in self._denied:
            return _refuse(f"the {self._kind} {key!r}")
        return self._inner[key]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and ("." in key or key in self._denied or key in self._inner)

    def __setitem__(self, key: str, value: Callable[..., Any]) -> None:
        self._inner[key] = value

    def __delitem__(self, key: str) -> None:
        del self._inner[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._inner)

    def __len__(self) -> int:
        return len(self._inner)


def _guard(templar: Templar) -> None:
    # ``Templar.environment`` is deprecated; the engine's is the same object, created
    # for this Templar only, so changing it touches no other templating.
    env = templar._engine.environment
    for name in LOOKUP_GLOBALS:
        env.globals[name] = _refuse(f"{name}()")
    if not isinstance(env.filters, _GuardedPlugins):
        env.filters = _GuardedPlugins(env.filters, "filter", SERVER_FILTERS)
        env.tests = _GuardedPlugins(env.tests, "test", SERVER_TESTS)
    env.loader = DictLoader({})


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
    variables = combine_vars(trusted, fact_vars)
    variables = combine_vars(variables, magic_vars(hostname, chain or []))

    templar = Templar(loader=DataLoader(), variables=variables)
    _guard(templar)
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
