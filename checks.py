"""Model checks: saved rules whose matching elements are failures ("walls with an empty fire
rating"), run on any version and compared with the version before it.

Pure functions over index records, like model_index; storage lives in store.py.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import model_index

FILTER_KEYS = ("category", "name_contains", "property", "operator", "value")
# Failing checks stand out against the change colors (green/orange) and the model's materials.
CHECK_COLORS = ("#e15759", "#b07aa1", "#edc948", "#ff9da7", "#9c755f", "#4e79a7")
SAMPLE = 5


def make_check(name: str, spec: dict) -> dict:
    """A validated check from a name and filter arguments (same names as speckle_query_objects)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A check needs a name, e.g. 'Walls need a fire rating'.")
    check: dict[str, Any] = {"name": name}
    for key in FILTER_KEYS:
        value = spec.get(key)
        if value not in (None, ""):
            check[key] = str(value)
    if not any(k in check for k in ("category", "name_contains", "property")):
        raise ValueError("A check needs a filter: category, name_contains and/or property.")
    op = check.get("operator") or ("equals" if "value" in check else "exists")
    if op not in model_index.OPERATORS:
        raise ValueError(f"Unknown operator {op!r}; use one of {', '.join(model_index.OPERATORS)}")
    if op in ("equals", "not_equals", "contains", "not_contains", "gt", "lt") and "value" not in check:
        raise ValueError(f"Operator {op} needs a value.")
    check["operator"] = op
    check["id"] = hashlib.sha1(repr(sorted(check.items())).encode()).hexdigest()[:8]
    return check


def failures(index: dict[str, dict], check: dict) -> list[str]:
    hits = model_index.match(index, category=check.get("category"), name_contains=check.get("name_contains"),
                             prop=check.get("property"), value=check.get("value"), op=check.get("operator"))
    return sorted(r["key"] for r, _ in hits)


def describe(check: dict) -> str:
    parts = []
    if check.get("category"):
        parts.append(f"category contains '{check['category']}'")
    if check.get("name_contains"):
        parts.append(f"name contains '{check['name_contains']}'")
    if check.get("property"):
        value = f" '{check['value']}'" if "value" in check else ""
        parts.append(f"{check['property']} {check['operator']}{value}")
    return "fails when " + " and ".join(parts)


def _has_property(index: dict[str, dict], check: dict) -> bool:
    return bool(model_index.match(index, category=check.get("category"), name_contains=check.get("name_contains"),
                                  prop=check["property"], op="exists"))


def run(index: dict[str, dict], checks: list[dict], previous: dict[str, dict] | None = None) -> list[dict]:
    """Each check's failures on `index`, and what is new or fixed against `previous`."""
    results = []
    for check in checks:
        ids = failures(index, check)
        result: dict[str, Any] = {
            "id": check["id"], "name": check["name"], "failing": len(ids), "ids": ids,
            "examples": [{"id": k, "category": index[k]["label"], "name": index[k]["name"]} for k in ids[:SAMPLE]],
        }
        if check.get("property") and ids and not _has_property(index, check):
            result["warning"] = (f"No element in scope has a property matching {check['property']!r}, so "
                                 "every one counts as failing. Check the property name with speckle_query_objects.")
        if previous is not None:
            before = set(failures(previous, check))
            now = set(ids)
            # An element that left the model is not a fix of the rule.
            result["new"] = sorted(now - before)
            result["fixed"] = sorted(k for k in before - now if k in index)
            result["previously_failing"] = len(before)
        results.append(result)
    return results


def viewer_groups(results: list[dict]) -> list[dict]:
    """One color per failing check; an element failing several checks takes the first one's color."""
    seen: set[str] = set()
    groups = []
    for i, r in enumerate(x for x in results if x["failing"]):
        ids = [k for k in r["ids"] if k not in seen]
        seen.update(ids)
        groups.append({"value": r["name"], "color": CHECK_COLORS[i % len(CHECK_COLORS)], "ids": ids, "count": r["failing"]})
    return groups


def failed_by(records: list[dict], checks: list[dict]) -> dict[str, list[str]]:
    """Per element key, the names of the checks it fails."""
    subset = {r["key"]: r for r in records}
    out: dict[str, list[str]] = {}
    for check in checks:
        for key in failures(subset, check):
            out.setdefault(key, []).append(check["name"])
    return out
