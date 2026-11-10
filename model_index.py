"""Element index operations: summary, version diff and query.

Pure functions over plain record dicts ({key, label, name, fp, props}), so they are testable
without a server. Records come from bundle_model.element_index and are keyed by applicationId,
which is stable across versions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


def summarize(index: dict[str, dict]) -> dict:
    categories = Counter(r["label"] for r in index.values())
    levels = Counter(r["props"].get("level") or "(no level)" for r in index.values())
    keys: Counter = Counter()
    for r in index.values():
        keys.update(r["props"].keys())
    return {
        "element_count": len(index),
        "by_category": dict(categories.most_common()),
        "by_level": dict(levels.most_common()),
        "common_properties": [k for k, _ in keys.most_common(30)],
    }


def _changed_props(old: dict, new: dict) -> list[str]:
    names = set(old) | set(new)
    return sorted(n for n in names if old.get(n) != new.get(n))


def change_sets(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list[str]]:
    """Element keys added, removed and changed (fingerprint differs) between two indexes."""
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(k for k in old.keys() & new.keys() if old[k]["fp"] != new[k]["fp"]),
    }


def element_change(old: dict | None, new: dict) -> dict:
    """How one element differs from its previous version."""
    if old is None:
        return {"status": "added"}
    if old["fp"] == new["fp"]:
        return {"status": "unchanged"}
    names = _changed_props(old["props"], new["props"])
    change: dict[str, Any] = {"status": "changed",
                              "properties": {p: {"from": old["props"].get(p), "to": new["props"].get(p)} for p in names}}
    if not names:
        change["note"] = "geometry or placement changed; no property changed"
    return change


ADDED_COLOR, CHANGED_COLOR = "#59a14f", "#f28e2b"


def change_groups(old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    """Viewer color groups for a version diff; removed elements are not in the new version, so
    that group only carries a count for the legend."""
    sets = change_sets(old, new)
    return [
        {"value": "added", "color": ADDED_COLOR, "ids": sets["added"], "count": len(sets["added"])},
        {"value": "changed", "color": CHANGED_COLOR, "ids": sets["changed"], "count": len(sets["changed"])},
        {"value": "removed (not in this version)", "color": OTHER_COLOR, "ids": [], "count": len(sets["removed"])},
    ]


def diff(old: dict[str, dict], new: dict[str, dict], examples: int = 5) -> dict:
    sets = change_sets(old, new)
    added = [new[k] for k in sets["added"]]
    removed = [old[k] for k in sets["removed"]]
    changed = [(old[k], new[k]) for k in sets["changed"]]

    by_label: dict[str, dict] = defaultdict(lambda: {"added": 0, "removed": 0, "changed": 0})
    for r in added:
        by_label[r["label"]]["added"] += 1
    for r in removed:
        by_label[r["label"]]["removed"] += 1
    param_changes: Counter = Counter()
    for o, n in changed:
        by_label[n["label"]]["changed"] += 1
        param_changes.update(_changed_props(o["props"], n["props"]))

    def sample(records: Iterable[dict]) -> list[dict]:
        return [{"id": r["key"], "category": r["label"], "name": r["name"]} for r in list(records)[:examples]]

    return {
        "totals": {"added": len(added), "removed": len(removed), "changed": len(changed),
                   "unchanged": len(new) - len(added) - len(changed)},
        "by_category": dict(sorted(by_label.items(), key=lambda kv: -sum(kv[1].values()))),
        "top_changed_properties": [p for p, _ in param_changes.most_common(10)],
        "examples": {
            "added": sample(added),
            "removed": sample(removed),
            "changed": [
                {"id": n["key"], "category": n["label"], "name": n["name"],
                 "properties": {p: {"from": o["props"].get(p), "to": n["props"].get(p)}
                                for p in _changed_props(o["props"], n["props"])[:6]}}
                for o, n in changed[:examples]
            ],
        },
    }


def headline(d: dict) -> str:
    t = d["totals"]
    parts = []
    for label, c in list(d["by_category"].items())[:4]:
        bits = [f"+{c['added']}" if c["added"] else "", f"−{c['removed']}" if c["removed"] else "",
                f"~{c['changed']}" if c["changed"] else ""]
        parts.append(f"{label} {' '.join(b for b in bits if b)}")
    head = f"{t['added']} added, {t['removed']} removed, {t['changed']} changed"
    return head + (f" ({'; '.join(parts)})" if parts else "")


def resolve_property(props: dict, name: str) -> str | None:
    """Full dotted path for a property name: exact path, else the path whose last segments match
    ("Base Constraint", "Constraints.Base Constraint"), else a unique substring match."""
    wanted = name.strip().lower()
    lowered = {k.lower(): k for k in props}
    if wanted in lowered:
        return lowered[wanted]
    suffix = [k for lk, k in lowered.items() if lk.endswith("." + wanted)]
    if suffix:
        return min(suffix, key=len)
    partial = [k for lk, k in lowered.items() if wanted in lk]
    return partial[0] if len(partial) == 1 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


OPERATORS = ("equals", "not_equals", "contains", "not_contains", "gt", "lt", "exists", "missing", "empty", "not_empty")


def _compare(present: bool, actual: Any, op: str, value: Any) -> bool:
    if op == "exists":
        return present
    if op == "missing":
        return not present
    if op == "empty":
        return not present or actual in (None, "")
    if op == "not_empty":
        return present and actual not in (None, "")
    if not present:
        return False
    if op in ("gt", "lt"):
        a, b = _number(actual), _number(value)
        return a is not None and b is not None and (a > b if op == "gt" else a < b)
    text, target = str(actual).lower(), str(value).lower()
    if op == "contains":
        return target in text
    if op == "not_contains":
        return target not in text
    if op == "not_equals":
        return text != target
    return text == target


def _select_fields(props: dict, fields: list[str] | None, always: list[str]) -> dict:
    if not fields:
        chosen = dict(props)
    else:
        needles = [f.strip().lower() for f in fields if f and f.strip()]
        chosen = {k: v for k, v in props.items() if any(n in k.lower() for n in needles)}
    for key in always:
        if key in props:
            chosen[key] = props[key]
    return chosen


def match(index: dict[str, dict], *, category: str | None = None, name_contains: str | None = None,
          prop: str | None = None, value: Any = None, op: str | None = None) -> list[tuple[dict, str | None]]:
    """Every record passing the filter, with the resolved path of `prop` (None without one)."""
    op = (op or ("equals" if value is not None else "exists")).lower()
    if op not in OPERATORS:
        raise ValueError(f"Unknown operator {op!r}; use one of {', '.join(OPERATORS)}")
    hits: list[tuple[dict, str | None]] = []
    for r in index.values():
        if category and category.lower() not in r["label"].lower():
            continue
        if name_contains and name_contains.lower() not in str(r["name"]).lower():
            continue
        key = resolve_property(r["props"], prop) if prop else None
        if prop and not _compare(key is not None, r["props"].get(key) if key else None, op, value):
            continue
        hits.append((r, key))
    return hits


# Distinct hues for categorical values; a light-to-dark ramp for numeric ranges.
CATEGORICAL = ("#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
               "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#17becf")
SEQUENTIAL = ("#fde725", "#90d743", "#35b779", "#21918c", "#31688e", "#443983")
OTHER_COLOR = "#9a9a9a"


def color_groups(records: Iterable[dict], prop: str) -> dict:
    """Group records by the value of `prop` for color coding: one color per value when there are
    few, value ranges when there are many numbers, else the most common values plus "other"."""
    values: dict[str, Any] = {}
    paths: set[str] = set()
    for r in records:
        key = resolve_property(r["props"], prop)
        if key:
            paths.add(key)
        values[r["key"]] = r["props"][key] if key and r["props"][key] not in (None, "") else None

    numbers = {k: _number(v) for k, v in values.items() if v is not None}
    distinct = {str(v) for v in values.values() if v is not None}
    groups: list[dict] = []
    if len(distinct) > len(CATEGORICAL) and numbers and all(n is not None for n in numbers.values()):
        ordered = sorted(set(numbers.values()))
        bins = len(SEQUENTIAL)
        edges = [ordered[round(i * (len(ordered) - 1) / bins)] for i in range(bins + 1)]
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            last = i == bins - 1
            ids = [k for k, n in numbers.items() if n is not None and lo <= n and (n <= hi if last else n < hi)]
            if ids:
                groups.append({"value": f"{lo:g} – {hi:g}", "color": SEQUENTIAL[i], "ids": ids})
    else:
        by_value: dict[str, list[str]] = defaultdict(list)
        for k, v in values.items():
            if v is not None:
                by_value[str(v)].append(k)
        ranked = sorted(by_value.items(), key=lambda kv: -len(kv[1]))
        for i, (v, ids) in enumerate(ranked[: len(CATEGORICAL)]):
            groups.append({"value": v, "color": CATEGORICAL[i], "ids": ids})
        rest = [k for _, ids in ranked[len(CATEGORICAL):] for k in ids]
        if rest:
            groups.append({"value": f"other ({len(ranked) - len(CATEGORICAL)} values)", "color": OTHER_COLOR, "ids": rest})
    unset = [k for k, v in values.items() if v is None]
    if unset:
        groups.append({"value": "(not set)", "color": OTHER_COLOR, "ids": unset})
    return {"property": sorted(paths) or [prop], "groups": groups}


def query(index: dict[str, dict], *, category: str | None = None, name_contains: str | None = None,
          prop: str | None = None, value: Any = None, op: str | None = None,
          fields: list[str] | None = None, group_by: str | None = None, limit: int = 50) -> dict:
    """Filter elements; return their properties (all, or only `fields`) and optional value counts.

    The filtered property is always returned, so a match is never shown without the value that
    made it match. `group_by` counts over every match, not only the returned page."""
    hits = match(index, category=category, name_contains=name_contains, prop=prop, value=value, op=op)
    result: dict[str, Any] = {"match_count": len(hits)}
    if group_by:
        counts: Counter = Counter()
        resolved_paths: set[str] = set()
        for r, _ in hits:
            key = resolve_property(r["props"], group_by)
            if key:
                resolved_paths.add(key)
            counts[str(r["props"][key]) if key else "(not set)"] += 1
        result["group_by"] = {"property": sorted(resolved_paths) or [group_by],
                              "counts": dict(counts.most_common())}
    result["elements"] = [
        {"id": r["key"], "category": r["label"], "name": r["name"],
         "properties": _select_fields(r["props"], fields, [key] if key else [])}
        for r, key in hits[:limit]
    ]
    if len(hits) > limit and not group_by:
        result["note"] = f"Showing {limit} of {len(hits)} matches; use group_by to count all of them."
    return result
