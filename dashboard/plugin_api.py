"""Backend for the Desktop half (/api/plugins/speckle/...): viewer page, credentials, model info."""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

DEFAULT_SERVER = "https://app.speckle.systems"
_MODEL_RE = re.compile(r"/projects/(?P<p>[0-9a-zA-Z]+)/models/(?P<m>[^/@?#,]+)(?:@(?P<v>[0-9a-zA-Z]+))?")


def _server_url() -> str:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("speckle") or {}
    return str((entry.get("settings") or {}).get("server_url") or DEFAULT_SERVER).rstrip("/")


def _token_for(url: str) -> str | None:
    """Only ever for the configured server: the token must not follow a model-supplied URL."""
    from agent.secret_scope import get_secret

    if url and urlparse(url).netloc != urlparse(_server_url()).netloc:
        return None
    return (get_secret("SPECKLE_TOKEN") or "").strip() or None


def _request(url: str, token: str | None, body: dict | None = None) -> urllib.request.Request:
    # The server's edge answers urllib's default User-Agent with 403.
    headers = {"Content-Type": "application/json", "User-Agent": "hermes-speckle/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.Request(url, data=data, headers=headers)


@router.get("/viewer-page")
async def viewer_page():
    page = Path(__file__).resolve().parent.parent / "viewer" / "index.html"
    return {"html": page.read_text(encoding="utf-8")}


_VERSION_QUERY = """
query($p: String!, $m: String!, $v: String!, $byId: Boolean!) {
  project(id: $p) {
    version(id: $v) @include(if: $byId) { id referencedObject previewUrl }
    model(id: $m) @skip(if: $byId) { versions(limit: 1) { items { id referencedObject previewUrl } } }
  }
}
"""


@router.get("/model-info")
async def model_info(url: str):
    """The shown version's id and preview image (as a data URL, so the Desktop half can show it
    while the 3D model loads without ever holding the token)."""
    match = _MODEL_RE.search(url)
    if not match:
        raise HTTPException(400, "not a Speckle model URL")
    server = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    token = _token_for(url)
    variables = {"p": match["p"], "m": match["m"], "v": match["v"] or "", "byId": bool(match["v"])}
    try:
        with urllib.request.urlopen(
            _request(f"{server}/graphql", token, {"query": _VERSION_QUERY, "variables": variables}), timeout=20
        ) as resp:
            data = (json.loads(resp.read()).get("data") or {}).get("project") or {}
    except Exception as exc:
        raise HTTPException(502, f"Speckle server unreachable: {exc}") from exc
    version = data.get("version") or next(iter(((data.get("model") or {}).get("versions") or {}).get("items") or []), None)
    if not version:
        raise HTTPException(404, "model or version not found (or not visible to this token)")

    preview = None
    if version.get("previewUrl"):
        try:
            preview_req = _request(version["previewUrl"], _token_for(version["previewUrl"]))
            with urllib.request.urlopen(preview_req, timeout=20) as resp:
                kind = resp.headers.get("Content-Type") or ""
                if kind.startswith("image/"):
                    preview = f"data:{kind};base64," + base64.b64encode(resp.read()).decode()
        except Exception:
            preview = None
    return {
        "version_id": version["id"],
        "format": "bundle" if (version.get("referencedObject") or "").startswith("bundle.") else "objects",
        "preview": preview,
    }


_CORE = "hermes_speckle_core"


def _core(module: str):
    """The plugin's own modules (receive + caches), imported beside this backend: the
    dashboard mount loads plugin_api.py on its own, without the agent half's package."""
    import importlib
    import importlib.util
    import sys

    if _CORE not in sys.modules:
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            _CORE, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[_CORE] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{_CORE}.{module}")


@router.get("/viewer-objects")
def viewer_objects(url: str):
    """Root-first object list for @speckle/viewer, built from the bundle (receive3); the token
    stays here, so the sandboxed viewer frame never holds a credential."""
    if urlparse(url).netloc != urlparse(_server_url()).netloc:
        raise HTTPException(403, f"only models on the configured server ({_server_url()}) can be shown")
    speckle_api = _core("speckle_api")
    service = _core("service")
    speckle_api.configure(server_url=_server_url())
    try:
        ref = speckle_api.parse_model_ref(url)
        version = service.resolve_version(ref)
        path = service.viewer_objects_path(ref, version)
    except (ValueError, LookupError) as exc:
        # LookupError covers bundle_model.UnsupportedVersion (a pre-bundle version).
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/json")


_MAX_SELECTED = 2000


@router.post("/selection")
def selection(payload: dict = Body(...)):
    """The Desktop viewer's selection (element applicationIds), for the agent's pre_llm_call
    context and speckle_viewer get_selection."""
    url = str(payload.get("url") or "")
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > _MAX_SELECTED or not all(isinstance(i, str) and len(i) < 256 for i in ids):
        raise HTTPException(400, f"ids must be a list of at most {_MAX_SELECTED} element ids")
    if urlparse(url).netloc != urlparse(_server_url()).netloc:
        raise HTTPException(403, f"only models on the configured server ({_server_url()}) can be selected in")
    speckle_api = _core("speckle_api")
    service = _core("service")
    store = _core("store")
    speckle_api.configure(server_url=_server_url())
    try:
        ref = speckle_api.parse_model_ref(url)
        version_id = ref.version_id or (service.resolve_version(ref).id if ids else "")
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc)) from exc
    store.save_selection({
        "url": service.viewer_url(ref, version_id or None), "project_id": ref.project_id, "model_id": ref.model_id,
        "version_id": version_id, "ids": ids, "at": time.time(),
    })
    return {"selected": len(ids)}


@router.get("/watches")
async def watches():
    from plugins.plugin_storage import plugin_data_dir

    path = plugin_data_dir("speckle") / "watches.json"
    if not path.exists():
        return {"watches": []}
    try:
        return {"watches": list(json.loads(path.read_text(encoding="utf-8")).values())}
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"unreadable watch list: {exc}") from exc
