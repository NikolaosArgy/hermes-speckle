"""Tool schemas and handlers. Handlers return JSON strings and never raise."""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Callable

from . import checks, model_index, service, speckle_api, store, watcher
from .speckle_api import ModelRef, SpeckleConfigError

logger = logging.getLogger(__name__)

TOOLSET = "speckle"
_MODEL_URL = {
    "type": "string",
    "description": "Speckle model URL (https://<server>/projects/<project>/models/<model>, "
    "optionally ending in @<version>) or 'project_id/model_id'.",
}


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }


def _handler(fn: Callable[[dict, dict], Any]) -> Callable[..., str]:
    @functools.wraps(fn)
    def wrapped(args: dict, **kwargs: Any) -> str:
        try:
            return json.dumps({"success": True, **fn(args or {}, kwargs)}, default=str)
        except (SpeckleConfigError, ValueError, LookupError) as exc:
            return json.dumps({"success": False, "error": str(exc)})
        except Exception as exc:
            logger.warning("speckle tool %s failed", fn.__name__, exc_info=True)
            return json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"})

    return wrapped


def _ref(args: dict) -> ModelRef:
    return speckle_api.parse_model_ref(args.get("model_url", ""))


@_handler
def list_projects(args: dict, _ctx: dict) -> dict:
    from specklepy.api.inputs import UserProjectsFilter

    search = args.get("search")
    projects = speckle_api.client().active_user.get_projects(
        limit=min(int(args.get("limit", 20)), 50),
        filter=UserProjectsFilter(search=search) if search else None,
    )
    return {"projects": [
        {"id": p.id, "name": p.name, "role": p.role, "updated_at": p.updated_at,
         "url": f"{speckle_api.server_url()}/projects/{p.id}"}
        for p in projects.items
    ]}


@_handler
def list_models(args: dict, _ctx: dict) -> dict:
    project_id = args["project_id"]
    with speckle_api.access(f"Project {project_id}"):
        models = speckle_api.client().model.get_models(project_id, models_limit=min(int(args.get("limit", 25)), 100))
    return {"models": [
        {"id": m.id, "name": m.name, "updated_at": m.updated_at,
         "url": f"{speckle_api.server_url()}/projects/{project_id}/models/{m.id}"}
        for m in models.items
    ]}


@_handler
def list_versions(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    versions = speckle_api.latest_versions(ref, limit=min(int(args.get("limit", 10)), 50))
    return {"model": ref.key, "versions": [service.version_info(v) for v in versions]}


@_handler
def model_summary(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    version = service.resolve_version(ref)
    index = service.element_index(ref, version)
    service.focus_desktop_viewer(ref, version.id)
    return {"model": ref.key, "version": service.version_info(version),
            "viewer_url": service.viewer_url(ref, version.id), **model_index.summarize(index)}


_QUERY_ARGS = ("category", "name_contains", "property", "operator", "value", "fields", "group_by", "limit")
_MAX_QUERIES = 50


def _run_query(index: dict, args: dict) -> dict:
    fields = args.get("fields")
    if isinstance(fields, str):
        fields = [fields]
    return model_index.query(
        index,
        category=args.get("category"),
        name_contains=args.get("name_contains"),
        prop=args.get("property"),
        value=args.get("value"),
        op=args.get("operator"),
        fields=fields,
        group_by=args.get("group_by"),
        limit=max(0, min(int(args.get("limit", 20)), 200)),
    )


@_handler
def query_objects(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    version = service.resolve_version(ref)
    index = service.element_index(ref, version)
    batch = args.get("queries")
    if not batch:
        return {"model": ref.key, "version_id": version.id, **_run_query(index, args)}
    # Hermes's tool_call bridge refuses to batch plugin tools, so a per-category breakdown used to
    # cost one model round-trip per query; one call with queries runs them all on one loaded index.
    if not isinstance(batch, list) or not all(isinstance(q, dict) for q in batch):
        raise ValueError("queries must be a list of objects, each with the same filters as a single query")
    if len(batch) > _MAX_QUERIES:
        raise ValueError(f"queries takes at most {_MAX_QUERIES} entries; you sent {len(batch)}")
    shared = {k: args[k] for k in _QUERY_ARGS if k in args}
    results = []
    for q in batch:
        try:
            results.append({"query": q, **_run_query(index, {**shared, **q})})
        except (ValueError, LookupError) as exc:
            results.append({"query": q, "error": str(exc)})
    return {"model": ref.key, "version_id": version.id, "results": results}


@_handler
def diff_versions(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    to_version = service.resolve_version(ref, args.get("to_version"))
    if args.get("from_version"):
        from_version = speckle_api.get_version(ref, args["from_version"])
    else:
        from_version = service.previous_version(ref, to_version)
        if from_version is None:
            raise LookupError("This model has only one version; nothing to compare against.")
    show = args.get("show_in_viewer", True) is not False
    return {"model": ref.key, **service.diff_versions(ref, from_version, to_version, show=show)}


@_handler
def show_model(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    version = service.resolve_version(ref)
    url = service.viewer_url(ref, version.id)
    service.focus_desktop_viewer(ref, version.id)
    preview = service.preview_path(ref, version)
    hint = (
        "The Hermes Desktop Speckle pane now shows this version in 3D. To also show it inline, "
        f'put ::speckle-model{{url="{url}"}} on its own line.'
    )
    if preview:
        hint += f" On messaging surfaces, end your reply with MEDIA:{preview}"
    return {
        "model": ref.key,
        "version": service.version_info(version),
        "viewer_url": url,
        "preview_image": preview,
        "display_hint": hint,
    }


@_handler
def create_issue(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("An issue needs a title.")
    issue = speckle_api.create_issue(ref, title, args.get("description") or "", args.get("version_id") or ref.version_id)
    return {"issue": issue, "model_url": ref.url,
            "issues_url": f"{speckle_api.server_url()}/projects/{ref.project_id}/issues"}


@_handler
def watch_model(args: dict, ctx: dict) -> dict:
    from gateway.session_context import get_session_env

    ref = _ref(args)
    session_key = get_session_env("HERMES_SESSION_KEY", "")
    watch = watcher.add_watch(ref, session_key)
    return {
        "watching": watch,
        "note": "I'll post here when a new version is published. If notices never arrive, the "
        "user must set plugins.entries.speckle.allow_gateway_injection: true in config.yaml.",
    }


@_handler
def unwatch_model(args: dict, _ctx: dict) -> dict:
    ref = _ref(args)
    return {"removed": watcher.remove_watch(ref), "model": ref.key}


@_handler
def list_watches(_args: dict, _ctx: dict) -> dict:
    return {"watches": watcher.watch_status()}


VIEWER_ACTIONS = ("color_by", "isolate", "select", "reset", "get_selection")
_MAX_SELECTION_RETURNED = 25


def _element(record: dict, fields: list[str] | None) -> dict:
    props = record["props"]
    if fields:
        needles = [f.lower() for f in fields]
        props = {k: v for k, v in props.items() if any(n in k.lower() for n in needles)}
    return {"id": record["key"], "category": record["label"], "name": record["name"], "properties": props}


def _get_selection(args: dict) -> dict:
    found = service.current_selection()
    if found is None:
        return {"selected": [], "note": "Nothing is selected in the Hermes Desktop viewer."}
    selection, records = found
    fields = args.get("fields")
    if isinstance(fields, str):
        fields = [fields]
    shown = records[:_MAX_SELECTION_RETURNED]
    details = service.selection_details(selection, shown)
    result = {"model_url": selection["url"], "selected_count": len(records),
              "selected": [{**_element(r, fields), **details.get(r["key"], {})} for r in shown]}
    if len(records) > _MAX_SELECTION_RETURNED:
        result["note"] = f"Showing {_MAX_SELECTION_RETURNED} of {len(records)} selected elements."
    return result


@_handler
def viewer(args: dict, _ctx: dict) -> dict:
    action = args.get("action")
    if action not in VIEWER_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(VIEWER_ACTIONS)}")
    if action == "get_selection":
        return _get_selection(args)

    ref = _ref(args)
    version = service.resolve_version(ref)
    if action == "reset":
        url = service.send_viewer_command(ref, version.id, {"action": "reset"})
        return {"model": ref.key, "viewer_url": url, "done": "Colors, isolation and selection cleared."}

    index = service.element_index(ref, version)
    filters = {"category": args.get("category"), "name_contains": args.get("name_contains"),
               "prop": args.get("property"), "value": args.get("value"), "op": args.get("operator")}
    filtered = any(v for k, v in filters.items() if k != "op")
    records = [r for r, _ in model_index.match(index, **filters)] if filtered else list(index.values())
    if not records:
        raise LookupError("No elements match that filter, so the viewer was left unchanged.")

    if action == "color_by":
        prop = (args.get("color_by") or "").strip()
        if not prop:
            raise ValueError("color_by needs the property to color by, e.g. 'category', 'level' or 'Fire Rating'.")
        colored = model_index.color_groups(records, prop)
        groups = colored["groups"]
        command = {"action": "color", "title": prop, "isolate": filtered, "groups": groups}
        url = service.send_viewer_command(ref, version.id, command)
        return {"model": ref.key, "viewer_url": url, "element_count": len(records),
                "property": colored["property"], "others_ghosted": filtered,
                "legend": [{"value": g["value"], "color": g["color"], "count": len(g["ids"])} for g in groups]}

    if not filtered:
        raise ValueError(f"{action} needs a filter (category, name_contains or property/value).")
    ids = [r["key"] for r in records]
    command = {"action": action, "ids": ids, "zoom": args.get("zoom", True) is not False}
    url = service.send_viewer_command(ref, version.id, command)
    return {"model": ref.key, "viewer_url": url, "element_count": len(ids),
            "by_category": model_index.summarize({r["key"]: r for r in records})["by_category"]}


CHECK_ACTIONS = ("add", "list", "remove", "run")


def _check_view(check: dict) -> dict:
    return {**check, "rule": checks.describe(check)}


def _summarize_run(run: dict, max_ids: int = 50) -> dict:
    """Tool output without the full failing-id lists, which only the viewer needs."""
    out = dict(run)
    out["checks"] = []
    for r in run["checks"]:
        item = {k: v for k, v in r.items() if k != "ids"}
        for key in ("new", "fixed"):
            if key in item:
                item[f"{key}_count"] = len(item[key])
                item[key] = item[key][:max_ids]
        out["checks"].append(item)
    return out


@_handler
def checks_tool(args: dict, _ctx: dict) -> dict:
    action = args.get("action")
    if action not in CHECK_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(CHECK_ACTIONS)}")
    ref = _ref(args)
    model_key = f"{ref.project_id}/{ref.model_id}"
    show = args.get("show_in_viewer", True) is not False

    if action == "list":
        return {"model": ref.key, "checks": [_check_view(c) for c in store.load_checks(model_key)]}

    if action == "remove":
        check_id = args.get("check_id") or ""

        def drop(saved: list[dict]) -> list[dict]:
            gone = [c for c in saved if c["id"] == check_id]
            saved[:] = [c for c in saved if c["id"] != check_id]
            return gone

        gone = store.update_checks(model_key, drop)
        if not gone:
            raise LookupError(f"No check {check_id!r} on {ref.key}; list them with action list.")
        return {"model": ref.key, "removed": gone[0]["name"]}

    if action == "add":
        check = checks.make_check(args.get("name") or "", {
            "category": args.get("category"), "name_contains": args.get("name_contains"),
            "property": args.get("property"), "operator": args.get("operator"), "value": args.get("value"),
        })

        def upsert(saved: list[dict]) -> None:
            saved[:] = [c for c in saved if c["id"] != check["id"]] + [check]

        store.update_checks(model_key, upsert)

    version = service.resolve_version(ref, args.get("version_id"))
    run = _summarize_run(service.run_checks(ref, version, show=show))
    result = {"model": ref.key, **run}
    if action == "add":
        result["added"] = _check_view(check)
        result["note"] = "Saved. It runs on every new version of this model the watcher sees."
    return result


TOOLS: list[tuple[dict, Callable[..., str]]] = [
    (_schema("speckle_list_projects", "List the user's Speckle projects (AEC/BIM model repositories).",
             {"search": {"type": "string", "description": "Filter by name."},
              "limit": {"type": "integer", "default": 20}}), list_projects),
    (_schema("speckle_list_models", "List the models in a Speckle project.",
             {"project_id": {"type": "string"}, "limit": {"type": "integer", "default": 25}},
             ["project_id"]), list_models),
    (_schema("speckle_list_versions", "List recent versions (publishes) of a Speckle model, newest first, "
             "with author, source application and message.",
             {"model_url": _MODEL_URL, "limit": {"type": "integer", "default": 10}}, ["model_url"]), list_versions),
    (_schema("speckle_model_summary", "Summarize what a Speckle model version contains: element counts per "
             "category and the most common properties. Also shows it in the Hermes Desktop viewer.",
             {"model_url": _MODEL_URL}, ["model_url"]), model_summary),
    (_schema("speckle_query_objects", "Find and count elements in a Speckle model by category, name or "
             "property, and read their properties. Property names can be short ('Base Constraint') or full "
             "dotted paths ('Parameters.Instance Parameters.Constraints.Base Constraint'). For counts per "
             "value (walls per level, doors per fire rating) use group_by with limit 0 instead of one query "
             "per value. Pass fields to return only the properties you need; without fields every property "
             "of every returned element is included, which is large. To run several queries on one model "
             "(e.g. each category per level), put them all in queries in ONE call; never make one call per "
             "query.",
             {"model_url": _MODEL_URL,
              "category": {"type": "string", "description": "Substring match on category, e.g. 'Walls'."},
              "name_contains": {"type": "string"},
              "property": {"type": "string", "description": "Property to filter on (short name or full path)."},
              "operator": {"type": "string", "enum": list(model_index.OPERATORS),
                           "description": "How to compare 'property' with 'value'. Default: equals when a "
                           "value is given, else exists. gt/lt compare numbers; empty also matches missing."},
              "value": {"type": "string", "description": "Value to compare against (case-insensitive)."},
              "fields": {"type": "array", "items": {"type": "string"},
                         "description": "Only return properties whose path contains one of these, e.g. "
                         "['Constraints'] or ['Base Constraint', 'Unconnected Height']. The filtered "
                         "property is always returned."},
              "group_by": {"type": "string", "description": "Property to count matches by, over all "
                           "matches (e.g. 'Base Constraint' for elements per level)."},
              "limit": {"type": "integer", "default": 20,
                        "description": "Max elements returned (0 = counts only)."},
              "queries": {"type": "array", "maxItems": _MAX_QUERIES,
                          "description": "Several queries on this model in one call, each an object with "
                          "the filters above (category, name_contains, property, operator, value, fields, "
                          "group_by, limit). Filters given at the top level apply to every entry unless the "
                          "entry sets its own, e.g. group_by 'level' and limit 0 at the top, then "
                          "[{'category': 'Walls'}, {'category': 'Curtain Wall Mullions'}]. Returns one "
                          "result per entry.",
                          "items": {"type": "object"}}}, ["model_url"]),
     query_objects),
    (_schema("speckle_diff_versions", "Compare two versions of a Speckle model element by element: what was "
             "added, removed or changed, per category, and which properties changed. Defaults to the latest "
             "version against the one before it.",
             {"model_url": _MODEL_URL, "from_version": {"type": "string"}, "to_version": {"type": "string"},
              "show_in_viewer": {"type": "boolean", "default": True, "description": "Color the change in "
                                 "the Hermes Desktop viewer: added green, changed orange, the rest faded. The "
                                 "user can then click an element and ask what changed on it."}},
             ["model_url"]), diff_versions),
    (_schema("speckle_checks", "Saved model checks: rules whose matching elements are failures, e.g. "
             "'Walls need a fire rating' = category Walls, property 'Fire Rating', operator empty. A check "
             "is saved per model and runs on every new version the watcher sees. Actions: add (save and run "
             "it), list, remove (by check_id), run (all checks on a version, default latest). Results give "
             "failures per check and what is new or fixed against the previous version, and paint the "
             "failures in the Desktop viewer. Write the rule as the condition that FAILS: 'beams must be "
             "W-shapes' = category Framing, property type, operator not_contains, value W.",
             {"action": {"type": "string", "enum": list(CHECK_ACTIONS)},
              "model_url": _MODEL_URL,
              "name": {"type": "string", "description": "For add: the rule in plain words."},
              "category": {"type": "string", "description": "Failure filter: substring match on category."},
              "name_contains": {"type": "string"},
              "property": {"type": "string", "description": "Failure filter: property (short name or full path)."},
              "operator": {"type": "string", "enum": list(model_index.OPERATORS)},
              "value": {"type": "string"},
              "check_id": {"type": "string", "description": "For remove."},
              "version_id": {"type": "string", "description": "For run/add: the version to check (default latest)."},
              "show_in_viewer": {"type": "boolean", "default": True}},
             ["action", "model_url"]), checks_tool),
    (_schema("speckle_show_model", "Show a Speckle model to the user: opens it in the Hermes Desktop 3D "
             "viewer pane and returns a preview image path for other chat surfaces.",
             {"model_url": _MODEL_URL}, ["model_url"]), show_model),
    (_schema("speckle_viewer", "Drive the 3D viewer in Hermes Desktop and read what the user selected in "
             "it. Actions: color_by (color elements by a property's value, with a legend; with a filter, "
             "only the matching elements are shown in color and the rest are ghosted), isolate (show only "
             "the matching elements and zoom to them), select (highlight and zoom to the matching elements; "
             "they become the user's selection), reset (clear colors, isolation and selection), "
             "get_selection (the elements the user clicked in the viewer, with their properties; no "
             "model_url needed). The filter arguments work like speckle_query_objects.",
             {"action": {"type": "string", "enum": list(VIEWER_ACTIONS)},
              "model_url": {**_MODEL_URL, "description": _MODEL_URL["description"]
                            + " Required for every action except get_selection."},
              "color_by": {"type": "string", "description": "For color_by: the property whose value sets "
                           "the color, e.g. 'category', 'level', 'type', 'Fire Rating'. Many numeric "
                           "values are grouped into ranges."},
              "category": {"type": "string", "description": "Filter: substring match on category."},
              "name_contains": {"type": "string", "description": "Filter: substring match on name."},
              "property": {"type": "string", "description": "Filter: property to compare (short name or "
                           "full path)."},
              "operator": {"type": "string", "enum": list(model_index.OPERATORS)},
              "value": {"type": "string"},
              "zoom": {"type": "boolean", "default": True, "description": "For select: zoom to the "
                       "selection."},
              "fields": {"type": "array", "items": {"type": "string"}, "description": "For "
                         "get_selection: only return properties whose path contains one of these."}},
             ["action"]), viewer),
    (_schema("speckle_create_issue", "Raise an issue on a Speckle model (visible to the whole project team "
             "in the Speckle web app). Confirm the title and content with the user first.",
             {"model_url": _MODEL_URL, "title": {"type": "string"},
              "description": {"type": "string"}, "version_id": {"type": "string"}},
             ["model_url", "title"]), create_issue),
    (_schema("speckle_watch_model", "Watch a Speckle model: whenever someone publishes a new version, Hermes "
             "posts a change summary with a preview into this chat.",
             {"model_url": _MODEL_URL}, ["model_url"]), watch_model),
    (_schema("speckle_unwatch_model", "Stop watching a Speckle model.",
             {"model_url": _MODEL_URL}, ["model_url"]), unwatch_model),
    (_schema("speckle_list_watches", "List the Speckle models being watched.", {}), list_watches),
]
