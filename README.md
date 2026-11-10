![NOUS x SPECKLE](assets/nous-x-speckle.jpg)

# Speckle for Hermes

Work with AEC/BIM models from [Speckle](https://speckle.systems) inside Hermes: browse
projects, query and count elements, compare versions, save checks that run on every new
version, get a chat message when someone publishes, and look at the model in a live 3D
viewer in Hermes Desktop.

## Install and set up

```bash
hermes plugins install speckle
```

1. Create a personal access token just for Hermes at
   <https://app.speckle.systems/settings/user/developer-settings> with the scopes
   `profile:read` and `streams:read`. Add `streams:write` only if you want Hermes to raise
   issues on models. Don't reuse a token from another tool, so you can revoke this one on
   its own.
2. Give it to Hermes as `SPECKLE_TOKEN`.
3. Using a self-hosted Speckle server? Set the plugin's `server_url` config. The default is
   `https://app.speckle.systems`. The token only works on the server that issued it, and
   the plugin only sends it there.

### Private models

The plugin works as the account that owns the token, so it can read any model that account
can open in the browser, private ones included. It can't read a private project the
account isn't a member of: ask a project owner to invite that account. Projects in a
workspace with single sign-on also need an active SSO session: if Hermes says so, sign in
to the workspace in your browser and ask again.

Requires Hermes 0.22 or newer. The plugin reads models in Speckle's bundle format, so
versions published by older connectors need publishing again from a current connector.

## What you can ask

- "What's in this model?" with a model URL: element counts per category and level.
- "How many curtain wall mullions per level?", "Which walls have no fire rating?"
- "What changed since the last version?" The viewer colors added elements green and
  changed ones orange.
- "Check that every door has a fire rating on each new version" saves a rule that runs
  whenever a new version arrives.
- "Watch this model" posts a message in chat when someone publishes a new version.
- "Color the walls by level", "Isolate the level 3 columns", "What is this?" about
  elements you clicked in the viewer.
- "Raise an issue about this on the model" (Hermes asks before creating it).

## Tools

| Tool | What it does |
|---|---|
| `speckle_list_projects`, `speckle_list_models`, `speckle_list_versions` | Browse your projects, models and versions |
| `speckle_model_summary` | Element counts per category and the most common properties |
| `speckle_query_objects` | Filter, count and group elements; several queries in one call |
| `speckle_diff_versions` | Element-by-element diff between two versions |
| `speckle_checks` | Save, list, run and remove rules that run on every new version |
| `speckle_show_model`, `speckle_viewer` | Show the model in the Desktop 3D viewer and drive it |
| `speckle_create_issue` | Raise an issue on a model (visible to the project team) |
| `speckle_watch_model`, `speckle_unwatch_model`, `speckle_list_watches` | Version watches |

Hooks: `on_session_start` resumes watches after a restart, and `pre_llm_call` tells the agent
what you have selected in the viewer.

## What this plugin does on your machine

- **Network calls.** The plugin talks to your configured Speckle server only
  (`app.speckle.systems` by default): GraphQL for projects, models and versions, model
  downloads, preview images, and issue creation when you ask for one. Your token is sent
  only to that server, never to a URL taken from a model or a message.
- **Third-party code in the viewer.** The Desktop 3D viewer loads
  `@speckle/viewer@2.31.14` and `@speckle/objectloader2@2.31.14` from `cdn.jsdelivr.net`
  (exact versions). It runs in a sandboxed frame, gets model objects from the plugin, and
  never receives your token.
- **Background process.** While you watch at least one model, a background thread checks
  it for new versions every `poll_seconds` (60 by default, 15 minimum). Each check is one
  GraphQL call and uses no LLM tokens until there is something to report.
- **Stored data.** Everything is kept in the plugin's own Hermes data folder: watches, saved
  checks, the last viewer selection, cached element indexes and viewer objects per
  version, and preview images. Model downloads are unpacked in a temporary
  `speckle-bundle-*` folder that is deleted once the model has been read. The plugin
  authenticates with `SPECKLE_TOKEN` only; it never reads Speckle Manager's saved accounts
  or any other credential store.
- **Writes to Speckle.** Only `speckle_create_issue` writes to Speckle, and the agent is
  told to ask you first.
- **No shell commands, no telemetry.**

## Development

```bash
hermes plugins validate . --install-deps
python -m pytest tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
