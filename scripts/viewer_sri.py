"""Pin the viewer's CDN modules with Subresource Integrity.

The viewer page loads @speckle/viewer and @speckle/objectloader2 from jsDelivr inside the
sandboxed frame. This script walks their whole module graph, hashes every file, and writes the
hashes into the page's import map, so the browser refuses any file whose bytes differ.

    python scripts/viewer_sri.py          # rewrite the import map in viewer/index.html
    python scripts/viewer_sri.py --check  # exit 1 if the CDN no longer matches the page
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "viewer" / "index.html"
ENTRIES = re.findall(r"import\('(https://cdn\.jsdelivr\.net/[^']+)'\)", PAGE.read_text(encoding="utf-8"))
# Static `from"…"`, bare `import"…"` and dynamic `import("…")` specifiers in jsDelivr's +esm output.
_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'](/npm/[^"']+|https://cdn\.jsdelivr\.net/[^"']+)["']""")
_MAP = re.compile(r'<script type="importmap">.*?</script>', re.S)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-speckle-sri/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def graph() -> dict[str, str]:
    hashes: dict[str, str] = {}
    todo = list(ENTRIES)
    while todo:
        url = todo.pop()
        if url in hashes:
            continue
        body = fetch(url)
        hashes[url] = "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
        for spec in _SPECIFIER.findall(body.decode("utf-8", "replace")):
            todo.append(urllib.parse.urljoin(url, spec))
    return dict(sorted(hashes.items()))


def render(hashes: dict[str, str]) -> str:
    body = json.dumps({"integrity": hashes}, indent=2)
    return f'<script type="importmap">\n{body}\n</script>'


def main() -> int:
    if not ENTRIES:
        print("no jsDelivr import() found in viewer/index.html", file=sys.stderr)
        return 1
    page = PAGE.read_text(encoding="utf-8")
    block = render(graph())
    if "--check" in sys.argv:
        current = _MAP.search(page)
        if current and current.group(0) == block:
            print(f"ok: {block.count('sha384-')} modules match")
            return 0
        print("viewer import map is out of date with the CDN; run scripts/viewer_sri.py", file=sys.stderr)
        return 1
    if _MAP.search(page):
        page = _MAP.sub(lambda _m: block, page, count=1)
    else:
        page = page.replace('<script type="module">', block + '\n<script type="module">', 1)
    PAGE.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {block.count('sha384-')} integrity hashes to {PAGE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
