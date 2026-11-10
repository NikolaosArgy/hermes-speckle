"""Background poller: notices new versions of watched models and reports them in chat.

Polling instead of Speckle webhooks: a webhook needs a public URL, which a local Hermes
install does not have. Each new version costs one GraphQL call per poll and zero LLM
tokens until something actually changed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import service, speckle_api, store
from .speckle_api import ModelRef

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {"thread": None, "stop": threading.Event(), "inject": None, "interval": 60}
_start_lock = threading.Lock()


def configure(inject: Callable[..., bool], interval_seconds: int) -> None:
    _state["inject"] = inject
    _state["interval"] = max(15, int(interval_seconds or 60))


def ensure_running() -> None:
    """Start the poller under the caller's context (it carries the profile's secret scope)."""
    with _start_lock:
        thread = _state["thread"]
        if thread is not None and thread.is_alive():
            return
        if not store.load_watches():
            return
        from agent.memory_provider import spawn_context_thread

        _state["stop"] = threading.Event()
        thread = spawn_context_thread(_loop, name="speckle-watcher", args=(_state["stop"],))
        _state["thread"] = thread
        thread.start()
        logger.info("speckle: watcher started")


def stop() -> None:
    _state["stop"].set()


def _loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        watches = store.load_watches()
        if not watches:
            break
        for key, watch in list(watches.items()):
            if stop_event.is_set():
                break
            try:
                check_watch(key, watch)
            except Exception:
                logger.warning("speckle: watch check failed for %s", key, exc_info=True)
        stop_event.wait(_state["interval"])
    logger.info("speckle: watcher stopped")


def add_watch(ref: ModelRef, session_key: str) -> dict:
    latest = service.resolve_version(ModelRef(ref.server, ref.project_id, ref.model_id))
    # Index the current version now, so the first change notification only has to
    # download the new version.
    service.element_index(ref, latest)
    watch = {
        "url": ModelRef(ref.server, ref.project_id, ref.model_id).url,
        "project_id": ref.project_id,
        "model_id": ref.model_id,
        "last_version_id": latest.id,
        "session_key": session_key,
        "since": datetime.now(timezone.utc).isoformat(),
    }
    store.update_watches(lambda w: w.__setitem__(ref.key, watch))
    ensure_running()
    return watch


def remove_watch(ref: ModelRef) -> bool:
    return bool(store.update_watches(lambda w: w.pop(ref.key, None)))


def check_watch(key: str, watch: dict) -> Optional[dict]:
    ref = speckle_api.parse_model_ref(watch["url"])
    versions = speckle_api.latest_versions(ref, limit=1)
    if not versions or versions[0].id == watch["last_version_id"]:
        return None
    new = versions[0]
    old = speckle_api.get_version(ref, watch["last_version_id"])
    # The viewer shows the change; check results go in the notice, so the two views don't fight.
    report = service.diff_versions(ref, old, new, show=True)
    try:
        report["checks"] = service.run_checks(ref, new)["checks"]
    except Exception:
        logger.warning("speckle: checks failed on %s@%s", ref.key, new.id, exc_info=True)
        report["checks"] = []
    preview = service.preview_path(ref, new)

    def advance(watches: dict) -> None:
        if key in watches:
            watches[key]["last_version_id"] = new.id

    store.update_watches(advance)
    _notify(watch, ref, new, report, preview)
    return report


def notification_text(ref: ModelRef, version: Any, report: dict, preview: Optional[str]) -> str:
    info = service.version_info(version)
    lines = [
        f"[Speckle watcher] A new version was published to the model I'm watching ({ref.key}).",
        f"Author: {info['author'] or 'unknown'} · App: {info['source_application'] or 'unknown'} "
        f"· Message: {info['message'] or '(none)'}",
        f"Change vs previous version: {report['headline']}.",
    ]
    if report["top_changed_properties"]:
        lines.append("Most-changed properties: " + ", ".join(report["top_changed_properties"][:6]) + ".")
    for kind in ("removed", "added"):
        ex = report["examples"][kind]
        if ex:
            lines.append(f"Examples {kind}: " + "; ".join(f"{e['category']} {e['name']} ({e['id']})" for e in ex))
    lines.extend(check_lines(report.get("checks") or []))
    lines.append(f"Viewer: {service.viewer_url(ref, version.id)} (Hermes Desktop shows it with added "
                 "elements green and changed ones orange)")
    if preview:
        lines.append(f"Preview image file: {preview}")
    lines.append(
        f"Call speckle_show_model with {service.viewer_url(ref, version.id)} so I can see it, then "
        "summarize this change for me in 2-4 sentences, flag anything that looks risky "
        "(removed structural elements, large changes, checks that got worse), and offer to raise a "
        "Speckle issue about it. I can click a colored element and ask what changed on it."
    )
    return "\n".join(lines)


def check_lines(results: list[dict]) -> list[str]:
    """Only what moved: a check that is still failing the same way is noise in a change notice."""
    if not results:
        return []
    moved = [r for r in results if r.get("new") or r.get("fixed")]
    failing = sum(1 for r in results if r["failing"])
    lines = [f"Model checks: {failing} of {len(results)} failing."]
    for r in moved:
        bits = [f"{len(r['new'])} new failure(s)" if r.get("new") else "",
                f"{len(r['fixed'])} fixed" if r.get("fixed") else ""]
        lines.append(f"- {r['name']}: {r['failing']} failing ({', '.join(b for b in bits if b)})")
    return lines


def _notify(watch: dict, ref: ModelRef, version: Any, report: dict, preview: Optional[str]) -> None:
    text = notification_text(ref, version, report, preview)
    inject = _state["inject"]
    delivered = False
    if inject is not None:
        session_key = watch.get("session_key") or None
        delivered = bool(inject(text, role="user", session_key=session_key))
    if not delivered:
        logger.warning(
            "speckle: could not deliver the change notice for %s to its chat; set "
            "plugins.entries.speckle.allow_gateway_injection: true in config.yaml",
            ref.key,
        )
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(store.PLUGIN_ID, "model.updated", {
            "url": service.viewer_url(ref, version.id),
            "headline": report["headline"],
            "delivered": delivered,
        })
    except Exception:
        logger.debug("speckle: desktop broadcast failed", exc_info=True)


def check_all() -> list[dict]:
    return [r for k, w in store.load_watches().items() if (r := check_watch(k, w))]


def watch_status() -> list[dict]:
    thread = _state["thread"]
    running = bool(thread and thread.is_alive())
    return [{**w, "poller_running": running, "interval_seconds": _state["interval"]}
            for w in store.load_watches().values()]
