import importlib.util
import sys
import types
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_pkg = types.ModuleType("speckle_pure")
_pkg.__path__ = [str(_root)]
sys.modules["speckle_pure"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"speckle_pure.{name}", _root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


model_index = _load("model_index")
checks = _load("checks")

FIRE = "Parameters.Instance Parameters.Fire.Fire Rating"


def wall(key, fire="", fp=None, **props):
    return {"key": key, "label": "Walls", "name": "Basic Wall", "fp": fp or f"fp-{key}",
            "props": {"category": "Walls", FIRE: fire, **props}}


def index(*records):
    return {r["key"]: r for r in records}


def test_check_needs_a_name_and_a_filter():
    with pytest.raises(ValueError):
        checks.make_check("", {"category": "Walls"})
    with pytest.raises(ValueError):
        checks.make_check("Anything", {})
    with pytest.raises(ValueError):
        checks.make_check("Bad op", {"property": "Fire Rating", "operator": "gt"})


def test_run_reports_failures_new_and_fixed_against_previous_version():
    rule = checks.make_check("Walls need a fire rating", {"category": "Walls", "property": "Fire Rating", "operator": "empty"})
    before = index(wall("w1"), wall("w2"), wall("w3", "EI60"), wall("gone"))
    after = index(wall("w1"), wall("w2", "EI30"), wall("w3", ""), wall("w4"))

    [result] = checks.run(after, [rule], previous=before)

    assert result["failing"] == 3
    assert result["new"] == ["w3", "w4"]
    assert result["fixed"] == ["w2"]  # "gone" left the model: not a fix
    assert result["previously_failing"] == 3


def test_not_contains_operator_for_naming_rules():
    rule = checks.make_check("Beams are W-shapes", {"category": "Walls", "property": "type", "operator": "not_contains", "value": "W"})
    idx = index(wall("a", type="W12x26"), wall("b", type="HSS6x6"))

    assert checks.failures(idx, rule) == ["b"]


def test_viewer_groups_color_each_element_once_and_skip_passing_checks():
    results = [
        {"name": "A", "failing": 2, "ids": ["x", "y"]},
        {"name": "B", "failing": 0, "ids": []},
        {"name": "C", "failing": 2, "ids": ["y", "z"]},
    ]

    groups = checks.viewer_groups(results)

    assert [(g["value"], g["ids"], g["count"]) for g in groups] == [("A", ["x", "y"], 2), ("C", ["z"], 2)]


def test_element_change_lists_changed_properties_or_notes_geometry():
    old, new = wall("w", "EI60", fp="a"), wall("w", "EI30", fp="b")

    assert model_index.element_change(None, new) == {"status": "added"}
    assert model_index.element_change(old, new)["properties"] == {FIRE: {"from": "EI60", "to": "EI30"}}
    assert "geometry" in model_index.element_change(wall("w", fp="a"), wall("w", fp="b"))["note"]


def test_change_groups_carry_removed_count_without_ids():
    groups = model_index.change_groups(index(wall("a"), wall("b", fp="1")), index(wall("b", fp="2"), wall("c")))

    assert [(g["value"].split()[0], g["ids"], g["count"]) for g in groups] == [
        ("added", ["c"], 1), ("changed", ["b"], 1), ("removed", [], 1),
    ]


def test_run_warns_when_no_element_has_the_property():
    rule = checks.make_check("Walls need an acoustic rating", {"category": "Walls", "property": "STC", "operator": "empty"})

    [result] = checks.run(index(wall("w1"), wall("w2")), [rule])

    assert result["failing"] == 2
    assert "STC" in result["warning"]
