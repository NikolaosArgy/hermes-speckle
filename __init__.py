"""Speckle for Hermes: AEC model tools, a version watcher, and a Desktop 3D viewer."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from . import speckle_api, tools, watcher

    speckle_api.configure(server_url=ctx.get_config("server_url"))
    watcher.configure(inject=ctx.inject_message, interval_seconds=ctx.get_config("poll_seconds", 60))
    for schema, handler in tools.TOOLS:
        ctx.register_tool(name=schema["name"], toolset=tools.TOOLSET, schema=schema, handler=handler,
                          emoji="🏗️")

    # Watches outlive the gateway process; resume polling in the first session after a restart.
    def _on_session_start(**_kwargs) -> None:
        try:
            watcher.ensure_running()
        except Exception:
            logger.debug("speckle: could not resume watcher", exc_info=True)

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _selection_context)
    ctx.on_unload(watcher.stop)

    ctx.register_system_prompt_section("speckle-display", _DISPLAY_GUIDE)


# A click in the viewer is the user pointing at the model, so "what is this?" must resolve without
# the user naming the element; an hour-old selection is no longer what they are looking at.
_SELECTION_MAX_AGE = 3600
_SELECTION_LINES = 5


def _selection_context(**_kwargs):
    try:
        from . import service

        found = service.current_selection(max_age_seconds=_SELECTION_MAX_AGE)
    except Exception:
        logger.debug("speckle: could not read the viewer selection", exc_info=True)
        return None
    if found is None:
        return None
    selection, records = found
    shown = records[:_SELECTION_LINES]
    try:
        details = service.selection_details(selection, shown)
    except Exception:
        logger.debug("speckle: could not explain the viewer selection", exc_info=True)
        details = {}
    lines = []
    for r in shown:
        props = r["props"]
        bits = [r["label"], props.get("family"), props.get("type"), props.get("level")]
        line = "- " + " · ".join(str(b) for b in bits if b) + f" (id {r['key']})"
        extra = details.get(r["key"], {})
        change = extra.get("change")
        if change and change["status"] != "unchanged":
            names = [p.rsplit(".", 1)[-1] for p in (change.get("properties") or {})]
            line += f"; {change['status']} since {change['since_version']}"
            line += f" ({', '.join(names[:4])})" if names else (" (geometry/placement)" if change["status"] == "changed" else "")
        if extra.get("fails_checks"):
            line += "; fails: " + ", ".join(extra["fails_checks"])
        lines.append(line)
    if len(records) > _SELECTION_LINES:
        lines.append(f"- …and {len(records) - _SELECTION_LINES} more")
    return {"context": (
        f"[Speckle viewer] The user has {len(records)} element(s) selected in the Hermes Desktop 3D "
        f"viewer, model {selection['url']}:\n" + "\n".join(lines) + "\n"
        "If they say this/these/the selection, they mean these elements. For their properties call "
        "speckle_viewer with action get_selection."
    )}


# The Speckle web app renders with WebGPU, so a headless scrape of a model URL (web tools, or an
# @url: attachment) captures only "Failed to get GPU adapter"; the agent used to relay that as
# the plugin's viewer failing.
_DISPLAY_GUIDE = """\
Speckle (AEC/BIM models): the speckle_* tools read, diff, watch and raise issues on models.
Speckle model URLs (…/projects/<id>/models/<id>) are data, not web pages: always handle them
with the speckle_* tools. Never open, fetch, scrape or browse them with web or browser tools.
If attached page content for a Speckle URL says "Failed to get GPU adapter" or mentions WebGPU,
ignore it: that is a headless scraper failing on the Speckle web app, not a problem with the
model or with the Speckle viewer in Hermes. Do not mention it to the user; call
speckle_show_model instead.
In Hermes Desktop, speckle_viewer drives the 3D viewer: color_by a property (with a legend),
isolate or select elements by the same filters as speckle_query_objects, reset, and
get_selection for what the user clicked. speckle_diff_versions paints the change in the viewer
(added green, changed orange). speckle_checks saves rules ("walls need a fire rating") that run on
every new version; offer one when the user asks for the same check twice. Prefer showing an
answer in the viewer when the user asks where things are or how they are distributed.
Several questions about one model (counts per category, per level, per type) go in ONE
speckle_query_objects call with queries; never batch speckle_* calls in tool_call.
When you talk about a specific Speckle model in Hermes Desktop, show it inline by putting
::speckle-model{url="<model url>"} alone on its own line (use the url the tool returned).
On messaging surfaces, show a model by ending the reply with MEDIA:<preview_image path> when
a tool returned one. Ask before creating issues; they are visible to the whole project team."""
