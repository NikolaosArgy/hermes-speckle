"""Use cases shared by the tools, the watcher and the Desktop backend."""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import bundle_model, checks, model_index, speckle_api, store
from .speckle_api import ModelRef

logger = logging.getLogger(__name__)


def version_info(version: Any) -> dict:
    author = getattr(version, "author_user", None)
    return {
        "id": version.id,
        "message": version.message,
        "author": getattr(author, "name", None),
        "source_application": version.source_application,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "format": "bundle" if speckle_api.is_bundle(version) else "objects",
    }


def resolve_version(ref: ModelRef, version_id: Optional[str] = None) -> Any:
    if version_id or ref.version_id:
        return speckle_api.get_version(ref, version_id or ref.version_id)
    versions = speckle_api.latest_versions(ref, limit=1)
    if not versions:
        raise LookupError(f"Model {ref.key} has no versions yet.")
    return versions[0]


def _receive_and_cache(ref: ModelRef, version: Any) -> None:
    """One receive3 feeds both caches: the element index (tools) and the viewer objects (Desktop)."""
    model = bundle_model.receive(ref, version)
    try:
        store.save_index(ref.project_id, version.id, bundle_model.element_index(model))
        tree = bundle_model.viewer_tree(model, name=ref.key)
        store.save_viewer_objects(ref.project_id, version.id, speckle_api.serialize_for_viewer(tree))
    finally:
        model.close()


def element_index(ref: ModelRef, version: Any) -> dict[str, dict]:
    cached = store.cached_index(ref.project_id, version.id)
    if cached is None:
        _receive_and_cache(ref, version)
        cached = store.cached_index(ref.project_id, version.id)
    return cached


def viewer_objects_path(ref: ModelRef, version: Any):
    path = store.viewer_objects_path(ref.project_id, version.id)
    if not path.exists():
        _receive_and_cache(ref, version)
    return path


def previous_version(ref: ModelRef, version: Any) -> Optional[Any]:
    versions = speckle_api.latest_versions(ref, limit=25)
    ids = [v.id for v in versions]
    if version.id in ids:
        pos = ids.index(version.id)
        return versions[pos + 1] if pos + 1 < len(versions) else None
    return versions[0] if versions else None


def diff_versions(ref: ModelRef, from_version: Any, to_version: Any, show: bool = False) -> dict:
    old = element_index(ref, from_version)
    new = element_index(ref, to_version)
    result = model_index.diff(old, new)
    result["headline"] = model_index.headline(result)
    result["from_version"] = version_info(from_version)
    result["to_version"] = version_info(to_version)
    if show:
        show_changes(ref, from_version.id, to_version.id, old, new)
        result["viewer"] = "The Desktop viewer shows the new version: added elements green, changed orange, the rest faded."
    return result


def show_changes(ref: ModelRef, from_version_id: str, to_version_id: str,
                 old: dict[str, dict], new: dict[str, dict]) -> None:
    """Paint a version diff in the Desktop viewer and remember it, so a click on a changed element
    can be explained against the version before."""
    store.save_review({"project_id": ref.project_id, "model_id": ref.model_id,
                       "from_version_id": from_version_id, "to_version_id": to_version_id})
    send_viewer_command(ref, to_version_id, {
        "action": "color", "title": f"changes since {from_version_id}", "isolate": True,
        "groups": model_index.change_groups(old, new),
    })


def run_checks(ref: ModelRef, version: Any, show: bool = False) -> dict:
    """The model's saved checks on this version, compared with the version before it."""
    saved = store.load_checks(f"{ref.project_id}/{ref.model_id}")
    if not saved:
        return {"checks": [], "note": "This model has no saved checks yet; add one with speckle_checks."}
    index = element_index(ref, version)
    before = previous_version(ref, version)
    previous = element_index(ref, before) if before is not None else None
    results = checks.run(index, saved, previous)
    if show:
        groups = checks.viewer_groups(results)
        failing = sum(r["failing"] for r in results)
        send_viewer_command(ref, version.id, {
            "action": "color", "isolate": bool(groups), "groups": groups,
            "title": f"checks: {failing} failure(s)" if groups else "checks: all passing",
        })
    return {"version_id": version.id, "compared_with": before.id if before is not None else None,
            "checks": results}


def selection_details(selection: dict, records: list[dict]) -> dict[str, dict]:
    """Per selected element: how it changed in the diff the viewer shows, and the checks it fails."""
    details: dict[str, dict] = {r["key"]: {} for r in records}
    review = store.load_review() or {}
    if review.get("to_version_id") == selection.get("version_id"):
        old = store.cached_index(review["project_id"], review["from_version_id"])
        if old is not None:
            for r in records:
                details[r["key"]]["change"] = model_index.element_change(old.get(r["key"]), r)
                details[r["key"]]["change"]["since_version"] = review["from_version_id"]
    saved = store.load_checks(f"{selection['project_id']}/{selection.get('model_id', '')}")
    for key, names in checks.failed_by(records, saved).items():
        details[key]["fails_checks"] = names
    return details


def viewer_url(ref: ModelRef, version_id: Optional[str] = None) -> str:
    return ModelRef(ref.server, ref.project_id, ref.model_id, version_id).url


def focus_desktop_viewer(ref: ModelRef, version_id: Optional[str] = None, reason: str = "show") -> None:
    """Point the Desktop viewer pane at this model; a no-op without a Desktop window."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(
            store.PLUGIN_ID,
            "model.focus",
            {"url": viewer_url(ref, version_id), "reason": reason},
        )
    except Exception:
        logger.debug("speckle: desktop focus broadcast failed", exc_info=True)


def send_viewer_command(ref: ModelRef, version_id: str, command: dict) -> str:
    """Drive the Desktop viewer (color, isolate, select, reset); a no-op without a Desktop window.
    Returns the viewer url the command targets, which the pane also focuses."""
    url = viewer_url(ref, version_id)
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(store.PLUGIN_ID, "viewer.command", {"url": url, "command": command})
    except Exception:
        logger.debug("speckle: viewer command broadcast failed", exc_info=True)
    return url


def current_selection(max_age_seconds: float | None = None) -> tuple[dict, list[dict]] | None:
    """The Desktop viewer selection and its index records, from the cache only (no network)."""
    import time

    selection = store.load_selection()
    if not selection or not selection.get("ids"):
        return None
    if max_age_seconds is not None and time.time() - selection.get("at", 0) > max_age_seconds:
        return None
    index = store.cached_index(selection["project_id"], selection["version_id"]) or {}
    records = [index[i] for i in selection["ids"] if i in index]
    return (selection, records) if records else None


def preview_path(ref: ModelRef, version: Any) -> Optional[str]:
    path = speckle_api.download_preview(ref, version, store.previews_dir())
    return str(path) if path else None
