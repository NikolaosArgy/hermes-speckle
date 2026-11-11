"""Thin layer over specklepy: auth, model-URL parsing, receive, previews, issues."""

from __future__ import annotations

import contextlib
import re
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

DEFAULT_SERVER = "https://app.speckle.systems"

_settings: dict[str, Any] = {}
_clients: dict[tuple[str, str], Any] = {}
_clients_lock = threading.Lock()


class SpeckleConfigError(RuntimeError):
    pass


class SpeckleAccessError(LookupError):
    """The server refused or could not find what was asked for; the message says what to do."""


def configure(**settings: Any) -> None:
    _settings.update({k: v for k, v in settings.items() if v not in (None, "")})


def server_url() -> str:
    return str(_settings.get("server_url") or DEFAULT_SERVER).rstrip("/")


def _host(url: str) -> str:
    return urlparse(url if "://" in url else f"https://{url}").netloc.lower()


def is_configured_server(url: str) -> bool:
    """The token belongs to the configured server and must never be sent anywhere else."""
    return _host(url) == _host(server_url())


class OffServerRequest(ValueError):
    pass


class _StayOnServer(urllib.request.HTTPRedirectHandler):
    """Every hop is re-checked: urllib would follow a redirect to any host with every header,
    Authorization included, so a redirect could leak the token or point the local backend at a
    LAN or cloud-metadata address (hermes-agent PR #152842 review)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_configured_server(newurl):
            raise OffServerRequest(f"refused a redirect off the configured Speckle server to {_host(newurl)}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_StayOnServer)


def open_url(req: urllib.request.Request, timeout: float):
    """urlopen for the plugin's own HTTP calls. They only ever reach the configured Speckle server,
    redirects included: URLs here can come from model output (a ::speckle-model directive), and
    the server is the one host the user chose, LAN-hosted or not."""
    if not is_configured_server(req.full_url):
        raise OffServerRequest(f"refused a request off the configured Speckle server to {_host(req.full_url)}")
    return _opener.open(req, timeout=timeout)


_SSO_CODES = {"SSO_SESSION_MISSING_OR_EXPIRED_ERROR"}
_UNAUTHORIZED_CODES = {"UNAUTHORIZED"}
_FORBIDDEN_CODES = {"FORBIDDEN"}
_NOT_FOUND_CODES = {"STREAM_NOT_FOUND", "PROJECT_NOT_FOUND", "BRANCH_NOT_FOUND", "MODEL_NOT_FOUND",
                    "COMMIT_NOT_FOUND", "VERSION_NOT_FOUND", "NOT_FOUND_ERROR"}


def _graphql_errors(exc: BaseException) -> list[dict]:
    errors = getattr(exc, "errors", None) or []
    return [e for e in errors if isinstance(e, dict)]


@contextlib.contextmanager
def access(what: str) -> Iterator[None]:
    """Turn the server's refusals into messages that say what to do, instead of raw GraphQL dumps.

    Private projects are the common case: the token can only see what its account can see."""
    from gql.transport.exceptions import TransportQueryError
    from specklepy.logging.exceptions import GraphQLException

    try:
        yield
    except (GraphQLException, TransportQueryError) as exc:
        errors = _graphql_errors(exc)
        codes = {str((e.get("extensions") or {}).get("code") or "") for e in errors}
        detail = "; ".join(str(e.get("message")) for e in errors if e.get("message")) or str(exc)
        server = server_url()
        if codes & _SSO_CODES:
            raise SpeckleAccessError(
                f"{what} is in a workspace that requires single sign-on, and the account behind "
                f"SPECKLE_TOKEN has no active SSO session. Sign in to that workspace at {server} "
                "in your browser, then try again."
            ) from None
        if codes & _UNAUTHORIZED_CODES:
            raise SpeckleAccessError(
                f"{server} rejected SPECKLE_TOKEN (revoked or expired). Create a new personal access "
                f"token at {server}/settings/user/developer-settings and update SPECKLE_TOKEN."
            ) from None
        if codes & _FORBIDDEN_CODES:
            raise SpeckleAccessError(
                f"No access to {what}: it is private, or the token lacks a scope ({detail}). The "
                "token sees only what its account can see: ask a project owner to invite that "
                "account, or check the token has streams:read (streams:write to raise issues)."
            ) from None
        if codes & _NOT_FOUND_CODES:
            raise SpeckleAccessError(
                f"{what} was not found on {server}. Check the link, and that it is on this server "
                "and shared with the account behind SPECKLE_TOKEN."
            ) from None
        raise


def token() -> str:
    # get_secret, not os.environ: one gateway process serves several profiles.
    from agent.secret_scope import get_secret

    value = (get_secret("SPECKLE_TOKEN") or "").strip()
    if not value:
        raise SpeckleConfigError(
            "SPECKLE_TOKEN is not set. Create a personal access token at "
            f"{server_url()}/settings/user/developer-settings and run "
            "`hermes plugins install speckle` again, or add it to your Hermes .env."
        )
    return value


def client():
    from specklepy.api.client import SpeckleClient

    key = (server_url(), token())
    with _clients_lock:
        existing = _clients.get(key)
        if existing is None:
            existing = SpeckleClient(host=key[0])
            existing.authenticate_with_token(key[1])
            _clients[key] = existing
        return existing


@dataclass(frozen=True)
class ModelRef:
    server: str
    project_id: str
    model_id: str
    version_id: Optional[str] = None

    @property
    def url(self) -> str:
        suffix = f"@{self.version_id}" if self.version_id else ""
        return f"{self.server}/projects/{self.project_id}/models/{self.model_id}{suffix}"

    @property
    def key(self) -> str:
        return f"{self.project_id}/{self.model_id}"


_URL_RE = re.compile(r"/projects/(?P<p>[0-9a-zA-Z]+)/models/(?P<m>[^/?#]+)")


def parse_model_ref(value: str) -> ModelRef:
    """Accepts a Speckle model URL (optionally `@version`) or `project_id/model_id[@version]`."""
    value = (value or "").strip()
    match = _URL_RE.search(value)
    if match:
        server = value[: match.start()] or server_url()
        if not is_configured_server(server):
            raise ValueError(
                f"That model is on {_host(server)}, but the Speckle plugin is set up for "
                f"{_host(server_url())} and SPECKLE_TOKEN only works there. Change the plugin's "
                "server_url setting (with a token from that server) to use it."
            )
        project_id, models = match.group("p"), match.group("m")
    elif re.fullmatch(r"[0-9a-zA-Z]+/[^/\s]+", value):
        server = server_url()
        project_id, models = value.split("/", 1)
    else:
        raise ValueError(
            f"Not a Speckle model reference: {value!r}. Use a model URL like "
            f"{server_url()}/projects/<project>/models/<model>"
        )
    first = models.split(",")[0]
    model_id, _, version_id = first.partition("@")
    return ModelRef(server.rstrip("/"), project_id, model_id, version_id or None)


BUNDLE_PREFIX = "bundle."


def is_bundle(version: Any) -> bool:
    """Bundle versions are the only ones receive3 reads; legacy object versions are refused."""
    return (getattr(version, "referenced_object", None) or "").startswith(BUNDLE_PREFIX)


def latest_versions(ref: ModelRef, limit: int = 2) -> list[Any]:
    with access(f"Model {ref.key}"):
        return list(client().version.get_versions(ref.model_id, ref.project_id, limit=limit).items)


def get_version(ref: ModelRef, version_id: str) -> Any:
    with access(f"Version {version_id} of {ref.key}"):
        return client().version.get(version_id, ref.project_id)


def serialize_for_viewer(root: Any) -> str:
    """The tree as the flat, root-first object list @speckle/viewer loads from memory
    (ObjectLoader2Factory.createFromObjects)."""
    from specklepy.api import operations
    from specklepy.transports.memory import MemoryTransport

    memory = MemoryTransport()
    root_id = operations.send(root, [memory], use_default_cache=False)
    objects = memory.objects
    ordered = [objects[root_id]] + [s for oid, s in objects.items() if oid != root_id]
    return "[" + ",".join(ordered) + "]"


def download_preview(ref: ModelRef, version: Any, dest_dir: Path) -> Optional[Path]:
    """The server renders previews asynchronously; returns None when none exists yet."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{ref.project_id}-{version.id}.png"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = version.preview_url or f"{server_url()}/preview/{ref.project_id}/commits/{version.id}"
    if not is_configured_server(url):
        return None
    # The server's edge answers urllib's default User-Agent with 403.
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token()}", "User-Agent": "hermes-speckle/0.1"}
    )
    try:
        with open_url(req, timeout=30) as resp:
            if "image" not in (resp.headers.get("Content-Type") or ""):
                return None
            dest.write_bytes(resp.read())
    except Exception:
        return None
    return dest


_CREATE_ISSUE = """
mutation CreateIssue($input: CreateIssueInput!) {
  projectMutations { issues { createIssue(input: $input) { id title } } }
}
"""


def _prosemirror(text: str) -> dict:
    paragraphs = [p for p in text.split("\n") if p.strip()] or [text]
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs
        ],
    }


def create_issue(ref: ModelRef, title: str, description: str, version_id: Optional[str]) -> dict:
    from gql import gql

    resource = f"{ref.model_id}@{version_id}" if version_id else ref.model_id
    variables = {
        "input": {
            "projectId": ref.project_id,
            "resourceIdString": resource,
            "title": title,
            "description": _prosemirror(description) if description else None,
        }
    }
    with access(f"Model {ref.key}"):
        result = client().httpclient.execute(gql(_CREATE_ISSUE), variable_values=variables)
    return result["projectMutations"]["issues"]["createIssue"]
