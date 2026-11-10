import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "speckle_model_index", Path(__file__).resolve().parents[1] / "model_index.py"
)
model_index = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = model_index
_spec.loader.exec_module(model_index)

CONSTRAINTS = "Parameters.Instance Parameters.Constraints"


def record(key, category, name="", fp=None, props=None):
    """An index record as bundle_model.element_index produces it."""
    return {"key": key, "label": category, "name": name, "fp": fp or f"fp-{key}",
            "props": {"category": category, **(props or {})}}


def index(*records):
    return {r["key"]: r for r in records}


def wall(key, level, height, fire="", fp=None):
    return record(key, "Walls", "Basic Wall", fp, {
        "level": level,
        f"{CONSTRAINTS}.Base Constraint": level,
        f"{CONSTRAINTS}.Unconnected Height": height,
        "Parameters.Instance Parameters.Fire.Fire Rating": fire,
        **{f"Parameters.Type Parameters.Other.Param {i}": i for i in range(30)},
    })


def walls():
    return index(
        wall("w1", "Level 1", 3.0, "EI60"), wall("w2", "Level 1", 4.5), wall("w3", "Level 2", 3.0),
        record("d1", "Doors", "Door", props={"level": "Level 1"}),
    )


def test_summary_counts_categories_and_levels():
    summary = model_index.summarize(walls())

    assert summary["element_count"] == 4
    assert summary["by_category"] == {"Walls": 3, "Doors": 1}
    assert summary["by_level"] == {"Level 1": 3, "Level 2": 1}


def test_diff_reports_added_removed_and_changed_properties_per_category():
    old = index(wall("w1", "Level 1", 3.0, fp="a"), record("c1", "Columns"), record("c2", "Columns"))
    new = index(wall("w1", "Level 1", 3.5, fp="b"), record("c1", "Columns"), record("b1", "Beams"))

    result = model_index.diff(old, new)

    assert result["totals"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    assert result["by_category"]["Columns"]["removed"] == 1
    assert result["by_category"]["Beams"]["added"] == 1
    assert result["top_changed_properties"] == [f"{CONSTRAINTS}.Unconnected Height"]
    assert result["examples"]["changed"][0]["properties"] == {
        f"{CONSTRAINTS}.Unconnected Height": {"from": 3.0, "to": 3.5}
    }
    assert "1 added, 1 removed, 1 changed" in model_index.headline(result)


def test_short_property_name_resolves_and_filtered_value_is_always_returned():
    result = model_index.query(walls(), prop="Base Constraint", value="level 2", fields=["Unconnected"])

    [element] = result["elements"]
    assert element["id"] == "w3"
    assert element["properties"] == {
        f"{CONSTRAINTS}.Unconnected Height": 3.0,
        f"{CONSTRAINTS}.Base Constraint": "Level 2",
    }


def test_group_by_counts_every_match_even_with_limit_zero():
    result = model_index.query(walls(), category="Walls", group_by="Base Constraint", limit=0)

    assert result["match_count"] == 3
    assert result["group_by"]["counts"] == {"Level 1": 2, "Level 2": 1}
    assert result["elements"] == []


def test_group_by_level_works_for_every_category():
    result = model_index.query(walls(), group_by="level", limit=0)

    assert result["group_by"]["counts"] == {"Level 1": 3, "Level 2": 1}


def test_operators_numeric_and_empty():
    taller = model_index.query(walls(), prop="Unconnected Height", op="gt", value="3", fields=["x"])
    unrated = model_index.query(walls(), category="Walls", prop="Fire Rating", op="empty", limit=0)

    assert [e["id"] for e in taller["elements"]] == ["w2"]
    assert unrated["match_count"] == 2


def test_color_groups_one_color_per_level_and_unset_last():
    result = model_index.color_groups(walls().values(), "Base Constraint")

    assert [(g["value"], sorted(g["ids"])) for g in result["groups"]] == [
        ("Level 1", ["w1", "w2"]), ("Level 2", ["w3"]), ("(not set)", ["d1"]),
    ]
    assert len({g["color"] for g in result["groups"][:2]}) == 2


def test_color_groups_bins_many_numbers_into_ranges():
    records = [record(f"b{i}", "Beams", props={"Length": i}) for i in range(30)]

    result = model_index.color_groups(records, "Length")

    assert len(result["groups"]) == len(model_index.SEQUENTIAL)
    assert sorted(k for g in result["groups"] for k in g["ids"]) == sorted(f"b{i}" for i in range(30))
