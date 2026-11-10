"""Durable plugin state: watch list and per-version element indexes."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

PLUGIN_ID = "speckle"
# Bump when indexing or viewer serialization changes, so stale per-version caches are ignored.
CACHE_VERSION = 4
_lock = threading.Lock()


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_ID)


def previews_dir() -> Path:
    return data_dir() / "previews"


def _watches_path() -> Path:
    return data_dir() / "watches.json"


def load_watches() -> dict[str, dict]:
    path = _watches_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_watches(mutate: Callable[[dict[str, dict]], Any]) -> Any:
    with _lock:
        watches = load_watches()
        result = mutate(watches)
        path = _watches_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(watches, indent=2), encoding="utf-8")
        tmp.replace(path)
        return result


def _index_path(project_id: str, version_id: str) -> Path:
    return data_dir() / f"indexes-v{CACHE_VERSION}" / f"{project_id}-{version_id}.json"


def cached_index(project_id: str, version_id: str) -> dict | None:
    path = _index_path(project_id, version_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_index(project_id: str, version_id: str, index: dict) -> None:
    path = _index_path(project_id, version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index), encoding="utf-8")


def viewer_objects_path(project_id: str, version_id: str) -> Path:
    return data_dir() / f"viewer-v{CACHE_VERSION}" / f"{project_id}-{version_id}.json"


def save_viewer_objects(project_id: str, version_id: str, objects_json: str) -> None:
    path = viewer_objects_path(project_id, version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(objects_json, encoding="utf-8")
    tmp.replace(path)


def _selection_path() -> Path:
    return data_dir() / "selection.json"


def save_selection(selection: dict) -> None:
    """What the user has selected in the Desktop viewer ({url, project_id, model_id, version_id, ids, at})."""
    path = _selection_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(selection), encoding="utf-8")
    tmp.replace(path)


def load_selection() -> dict | None:
    path = _selection_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _checks_path() -> Path:
    return data_dir() / "checks.json"


def load_checks(model_key: str) -> list[dict]:
    path = _checks_path()
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get(model_key) or [])
    except (OSError, ValueError, AttributeError):
        return []


def update_checks(model_key: str, mutate: Callable[[list[dict]], Any]) -> Any:
    """Checks are saved per model (project_id/model_id) and apply to all its versions."""
    with _lock:
        path = _checks_path()
        try:
            everything = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            everything = {}
        checks = list(everything.get(model_key) or [])
        result = mutate(checks)
        if checks:
            everything[model_key] = checks
        else:
            everything.pop(model_key, None)
        _write_json(path, everything)
        return result


def _review_path() -> Path:
    return data_dir() / "review.json"


def save_review(review: dict) -> None:
    """The version diff the viewer is showing ({project_id, model_id, from_version_id, to_version_id})."""
    _write_json(_review_path(), review)


def load_review() -> dict | None:
    path = _review_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
