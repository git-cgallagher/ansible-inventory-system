"""The fold must make 'emit cumulative fold at every contributing node, then let
Ansible replace' equal 'fold the whole path' for every strategy."""

from __future__ import annotations

import random

from ansible.utils.vars import combine_vars
from hypothesis import given, settings
from hypothesis import strategies as st

from ais.models import MergeStrategy
from ais.resolver import fold


def test_merge_deep():
    acc = fold(MergeStrategy.merge, None, {"a": {"x": 1, "y": 2}, "b": 1})
    out = fold(MergeStrategy.merge, acc, {"a": {"y": 3, "z": 4}, "c": 5})
    assert out == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}


def test_append_and_unique():
    assert fold(MergeStrategy.append, [1, 2], [2, 3]) == [1, 2, 2, 3]
    assert fold(MergeStrategy.unique_append, [1, 2], [2, 3]) == [1, 2, 3]


def test_type_mismatch_falls_back_to_replace():
    assert fold(MergeStrategy.merge, {"a": 1}, "scalar") == "scalar"
    assert fold(MergeStrategy.append, [1], {"a": 1}) == {"a": 1}


def emit_then_replace(strategy: MergeStrategy, contributions: list):
    """Simulate the exporter + Ansible: each contributing level emits the cumulative
    fold; Ansible applies them root-first with replace semantics."""
    acc = None
    emitted = []
    for value in contributions:
        if value is None:  # level does not contribute
            continue
        acc = fold(strategy, acc, value)
        emitted.append({"k": acc})
    result: dict = {}
    for e in emitted:
        result = combine_vars(result, e)
    return result.get("k")


def reference_fold(strategy: MergeStrategy, contributions: list):
    acc = None
    for value in contributions:
        if value is not None:
            acc = fold(strategy, acc, value)
    return acc


scalars = st.one_of(st.integers(), st.text(max_size=5), st.booleans())
dicts = st.recursive(
    st.dictionaries(st.text(min_size=1, max_size=3), scalars, max_size=4),
    lambda inner: st.dictionaries(st.text(min_size=1, max_size=3), inner | scalars, max_size=4),
    max_leaves=10,
)
lists = st.lists(scalars, max_size=5)


@settings(max_examples=200)
@given(st.lists(st.one_of(st.none(), dicts), min_size=1, max_size=7))
def test_merge_emit_equals_reference(path):
    assert emit_then_replace(MergeStrategy.merge, path) == reference_fold(MergeStrategy.merge, path)


@settings(max_examples=200)
@given(st.lists(st.one_of(st.none(), lists), min_size=1, max_size=7), st.sampled_from([MergeStrategy.append, MergeStrategy.unique_append]))
def test_list_emit_equals_reference(path, strategy):
    assert emit_then_replace(strategy, path) == reference_fold(strategy, path)


def test_two_depths_contribute_deeper_wins_with_full_fold():
    # all=[vim], business=[awscli], group=[nginx], host=[tcpdump]
    path = [["vim"], ["awscli"], None, None, ["nginx"], ["tcpdump"]]
    assert emit_then_replace(MergeStrategy.unique_append, path) == ["vim", "awscli", "nginx", "tcpdump"]
