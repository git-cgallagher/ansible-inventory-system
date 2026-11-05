"""Jinja render: facts, per-key errors, mock hosts, magic vars."""

from __future__ import annotations

from tests.conftest import current_rev


def test_render_evaluates_templates_with_facts(editor, viewer, seeded):
    rev = current_rev(editor)
    web = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all.commercial.prod.dc1.web")
    editor.put(f"/api/v1/nodes/{web}/vars/listen", json={"value": "{{ ansible_default_ipv4.address }}:{{ http_port }}"}, if_match=rev)
    out = viewer.post("/api/v1/render", json={"host": "web01", "facts": {"default_ipv4": {"address": "10.1.1.11"}, "ansible_os_family": "Debian"}}).json()
    assert out["rendered"]["listen"] == "10.1.1.11:8080"
    assert out["resolved"]["listen"] == "{{ ansible_default_ipv4.address }}:{{ http_port }}"  # resolved stays raw
    assert out["errors"] == {}
    # every non-template key renders to itself
    assert out["rendered"]["packages"] == out["resolved"]["packages"]
    assert out["rendered"]["sysctl"] == out["resolved"]["sysctl"]


def test_render_reports_errors_per_key(editor, viewer, seeded):
    rev = current_rev(editor)
    web = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all.commercial.prod.dc1.web")
    rev = editor.put(f"/api/v1/nodes/{web}/vars/needs_fact", json={"value": "{{ ansible_hostname }}-x"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{web}/vars/broken", json={"value": "{{ http_port "}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{web}/vars/fine", json={"value": "port-{{ http_port }}"}, if_match=rev).json()["revision"]
    out = viewer.post("/api/v1/render", json={"host": "web01"}).json()
    assert out["rendered"]["fine"] == "port-8080"
    assert "needs_fact" not in out["rendered"] and out["errors"]["needs_fact"]["type"] == "AnsibleUndefinedVariable"
    assert "ansible_hostname" in out["errors"]["needs_fact"]["message"]
    assert out["errors"]["broken"]["type"] == "AnsibleTemplateSyntaxError"
    # supplying the fact fixes that key only
    out = viewer.post("/api/v1/render", json={"host": "web01", "facts": {"hostname": "web01"}}).json()
    assert out["rendered"]["needs_fact"] == "web01-x" and list(out["errors"]) == ["broken"]


def test_mock_host_render_magic_vars(viewer, seeded):
    out = viewer.post(
        "/api/v1/render",
        json={"path": "all.commercial.lab.dc1.web", "overrides": {"me": "{{ inventory_hostname }} in {{ group_names | join('/') }}"}, "mock_hostname": "lab-web99"},
    ).json()
    assert out["rendered"]["me"] == "lab-web99 in commercial/commercial_lab/commercial_lab_dc1/commercial_lab_dc1_web"
    assert out["rendered"]["http_port"] == 8081
    no_template = viewer.post("/api/v1/render", json={"path": "all.commercial.lab.dc1.web", "template": False}).json()
    assert "rendered" not in no_template and "errors" not in no_template


# A viewer token must never reach the API server through the render: lookups, the builtin
# filters and tests that read its environment or files, collection plugins, includes and
# templated facts are all refused, whether the template is stored or sent as an override.
# (Keys must not be lookup, query or q: a variable shadows the global of that name.)
SERVER_REACHING = {
    "piped": "{{ lookup('pipe', 'echo reached-$((40 + 2))') }}",
    "queried": "{{ query('ansible.builtin.env', 'HOME') }}",
    "aliased": "{% set f = lookup %}{{ f('env', 'HOME') }}",
    "expandvars": "{{ '$HOME' | expandvars }}",
    "expandvars_fq": "{{ '$HOME' | ansible.builtin.expandvars }}",
    "mapped": "{{ ['$HOME'] | map('expandvars') | list }}",
    "fileglob": "{{ '/etc/*' | fileglob }}",
    "is_file": "{{ '/etc/passwd' is file }}",
    "selected": "{{ ['/etc/passwd'] | select('ansible.builtin.exists') | list }}",
    "collection": "{{ 'x' | community.general.dict_kv('a') }}",
    "include": "{% include 'ais/api.py' %}",
}


def _assert_all_refused(out):
    assert not set(SERVER_REACHING) & set(out["rendered"]), out["rendered"]
    for key in SERVER_REACHING:
        if key != "include":
            assert "does not run in the server render" in out["errors"][key]["message"], (key, out["errors"][key])
    assert "reached-42" not in str(out)  # the shell's output, never in the template text
    assert out["rendered"]["benign"] == "8081-lab-web99"


def test_render_never_reaches_the_server_via_overrides(viewer, seeded):
    overrides = dict(SERVER_REACHING, benign="{{ http_port }}-{{ inventory_hostname }}")
    out = viewer.post("/api/v1/render", json={"path": "all.commercial.lab.dc1.web", "mock_hostname": "lab-web99", "overrides": overrides}).json()
    _assert_all_refused(out)


def test_render_never_reaches_the_server_via_stored_values(editor, viewer, seeded):
    rev = current_rev(editor)
    web = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all.commercial.lab.dc1.web")
    for key, value in dict(SERVER_REACHING, benign="{{ http_port }}-{{ inventory_hostname }}").items():
        rev = editor.put(f"/api/v1/nodes/{web}/vars/{key}", json={"value": value}, if_match=rev).json()["revision"]
    out = viewer.post("/api/v1/render", json={"path": "all.commercial.lab.dc1.web", "mock_hostname": "lab-web99"}).json()
    _assert_all_refused(out)


def test_render_never_templates_facts(viewer, seeded):
    out = viewer.post(
        "/api/v1/render",
        json={"host": "web01", "facts": {"hostname": "{{ lookup('pipe', 'echo reached-$((40 + 2))') }}"}, "overrides": {"me": "{{ ansible_hostname }}"}},
    ).json()
    assert out["rendered"]["me"] == "{{ lookup('pipe', 'echo reached-$((40 + 2))') }}"  # a fact is data, as in Ansible
