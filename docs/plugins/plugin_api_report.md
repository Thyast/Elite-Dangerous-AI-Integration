# COVAS:NEXT Plugin API — Technical Report

This report answers a plan-agent questionnaire for a plugin that analyzes a
target ship build and recommends the next action (buy/retrieve modules,
travel, engineers, materials, route optimization). It is grounded in the
actual code of this repository (branch `feat/ship-upgrade-plugin-api`) and in
the working Ship Upgrade Manager plugin (`plugins/ship_upgrade_manager/`),
which serves as the reference implementation throughout.

Upstream project: https://github.com/RatherRude/Elite-Dangerous-AI-Integration
(fork under development: `Thyast/Elite-Dangerous-AI-Integration`).
Official plugin docs: https://ratherrude.github.io/Elite-Dangerous-AI-Integration/plugins/
Plugin template: https://github.com/COVAS-Labs/COVAS-NEXT-Plugin-Template

Versions: Python **3.12** (`.venv312`), Angular **17** + Electron **38** for
the UI/shell, runtime entrypoint `src/Chat.py`.

---

## 1. Plugin architecture

**Language/runtime**: Python 3.12 only. No plugin SDK beyond `PluginBase`;
plugins are plain Python modules loaded by the app at startup.

**Structure** (official — see `docs/plugins/Development.md`):

```
/plugins
  /YourPlugin
    manifest.json        # metadata + entrypoint (required)
    YourPlugin.py        # ≥ 1 class implementing PluginBase
    __init__.py          # optional, needed for relative imports
    /deps                # vendored python deps (optional)
    requirements.txt     # packaging-time only
/plugin_data
  /<plugin-guid>         # runtime-only persistent data (helper.get_plugin_data_path)
```

**manifest.json** (exact schema consumed by `PluginManifest` in
`src/lib/PluginBase.py`):

```json
{
  "guid": "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e",
  "name": "Ship Upgrade Manager",
  "author": "COVAS:NEXT",
  "version": "1.0.0",
  "repository": "",
  "description": "Manage ship upgrade plans and track progression by ship session.",
  "entrypoint": "ship_upgrade_manager.py"
}
```

**Loading** (`src/lib/PluginManager.py`, `load_plugins()`): scans
`plugins/` **one level deep**, requires `manifest.json`, loads the
`entrypoint` module (`load_plugin_module`), and instantiates the class found
in it. Two built-in plugins are always loaded (`EDCoPilotPlugin`,
`MistralPlugin`, `builtin_plugin_guids`). Settings come from
`config.json → plugin_settings[guid]`.

**Start/stop**: "started" and "stopped" correspond to the *runtime session*
of the assistant (user presses **Run** in the UI), not to app start: plugin
instances exist from app launch, but their runtime hooks only fire on
Run/Stop.

**SDKs/templates**: official cookiecutter template above; no other SDK.

---

## 2. Plugin lifecycle

Class `PluginBase` (abstract; `src/lib/PluginBase.py`). Exact contract:

| Hook | Signature | When |
| :--- | :--- | :--- |
| `__init__` | `(self, plugin_manifest: PluginManifest)` | At app launch (config state). Build `settings_config` here. |
| `on_chat_start` | `(self, helper: PluginHelper)` | User presses **Run**; register actions/projections/side effects here. |
| `on_chat_stop` | `(self, helper: PluginHelper)` | Runtime stops (also fires on reload). |
| `on_settings_changed` | `(self)` | Any setting of this plugin was updated (UI edit or `update_plugin_setting`). |
| `on_settings_button` | `(self, key: str, value: str \| None = None)` | A settings-grid button/row-action was clicked. |

Notes:

- `settings_config` is a class attribute **or** instance dict; the manager
  keeps a reference to it (`plugin_settings_configs[guid] is
  plugin.settings_config`) — mutating your grids/fields in place and calling
  the manager republish is how the UI is refreshed.
- `PluginBase.settings: dict[str, Any]` holds your persisted settings.
  **Careful**: the manager replaces `plugin.settings` wholesale whenever the
  generic config reloads (`on_settings_changed(new_config)` path). Anything
  volatile must not live there — keep in-memory state in instance attributes.
- **Error handling**: every hook is wrapped in try/except by the manager
  (`log('error', …)`), a failing plugin does not crash the app, and load
  failures produce a red "error" settings card listing the traceback
  (`register_settings`). There is **no automatic restart** of a plugin: the
  instance is created once per app start; a new run re-calls
  `on_chat_start` on the same instance.
- **Persistent state**: three levels:
  1. `helper.get_plugin_data_path(manifest)` → `plugin_data/<guid>/` —
     survives updates (writable, e.g. SQLite DB — used by the Ship Upgrade
     Manager: `ship_upgrade_manager.db`).
  2. `config.json → plugin_settings[guid]` — user-facing settings; persist a
     value with `helper._plugin_manager.update_plugin_setting(guid, key,
     value)` (also refreshes+republishes the UI and calls
     `on_settings_changed`).
  3. In-memory instance attributes — lost at app restart.

---

## 3. Available game data

**Journal reader**: `src/lib/EDJournal.py` (`EDJournal(logs_path)`,
`get_ed_journals_path(config)` in `src/lib/Config.py` resolves the
`Saved Games/Frontier Developments/Elite Dangerous` folder, or
`config.ed_journal_path`).

- A reading thread tails the newest `Journal.*.log` continuously from app
  start; `load_history()` ingests the latest log's entries at startup.
- Every journal line becomes a `GameEvent` (subclass of `Event`;
  `event.content` = the raw journal entry dict + a generated `id` per
  file-line).
- **Augmentation**: on related journal events, the reader merges these files
  into the event (same timestamp merge) —
  `Market.json`, `Outfitting.json`, `Shipyard.json`, `Cargo.json`,
  `ModulesInfo.json`, `ShipLocker.json`, `Backpack.json`
  (`EDJournal.augment_event_from_file`). Outfitting/Shipyard are **station
  catalogues**, not ship state; `ModulesInfo.json` is the current-loadout
  snapshot; `ShipLocker.json`/`Backpack.json` cover stored items and Odyssey
  consumables.
- **Status.json** is parsed separately by `src/lib/StatusParser.py`
  (`Status` TypedDict: flags, `BodyName`, pips, docked/landed, gear, heat…)
  and fed as `StatusEvent`.

**Event names relevant to this project** (raw journal names): `Location`,
`FSDJump`, `Docked`, `Undocked`, `CarrierStats`, `Loadout`, `ModuleInfo`,
`ModuleBuy`, `ModuleSell`, `ModuleSwap`, `ModuleStore`, `ModuleRetrieve`,
`EngineerProgress`, `Materials`, `MaterialCollected`, `Cargo`, `MarketSell`,
`MarketBuy`, `ShipyardSell`, `ShipyardSwap`, `Rank`, `Progress`,
`Reputation`, `Music`, `Commander`.

**Delivery to plugins**: via `EventManager` (`src/lib/EventManager.py`)
side effects — `def sideeffect(event: Event, projected_states: dict[str,
BaseModel]) -> None` — registered with
`helper.register_sideeffect(...)`. Events flow **in real time** (reading
thread) but **only while the runtime runs** (Run pressed): side effects are
registered at `on_chat_start` into the chat's `EventManager`.

**History at startup**: `load_history()` re-reads the newest journal, so
events from the current game session replay through the pipeline on Run.
Older sessions are **not** replayed. (Workaround used by the Ship Upgrade
Manager: read the newest `Loadout` directly from the journals at session
start — "baseline detection".)

**Duplicates/ordering**: event ids are stable (`<file index>:<line>`);
processing is sequential in file order. There is no built-in dedup beyond
idempotency of your handlers — treat events as **at-least-once, append
only** and make handlers idempotent (the upgrade plugin checks
`completed_steps` before completing). Missing events (app was closed) must
be reconstructed from files — see §4.

---

## 4. Player and ship state

| Need | Source | Notes |
| :--- | :--- | :--- |
| Commander | `Commander` journal event; `config.commander_name` | Set at startup. |
| Current system | `Location` / `FSDJump` events (`StarSystem`, `SystemAddress`) | Projected by `src/lib/projections/` (ship_info etc.). |
| Docked / station / body | `Docked` (`StationName`), `Status.json` (`Docked` flag, `BodyName`) | `StatusEvent` updates every ~s. |
| Current ship | `Loadout` event: `Ship` (internal id), `ShipID`, `ShipName` (custom name), `ShipIdent`, plus `Modules[]` | `ModulesInfo.json` = current-loadout snapshot. |
| Module engineering | `Loadout.Modules[].Engineering` (`BlueprintName`, `Level`, `Quality`, `ExperimentalEffect`, `Modifiers[]`) | Exact grades. |
| Bought/stored/retrieved modules | `ModuleBuy/ModuleSell/ModuleSwap/ModuleStore/ModuleRetrieve` | May use localized ids (`$hpt_x_name;`) — normalize (strip `$`, `_name;`). `ToItem: "Null"` = empty slot. |
| Cargo | `Cargo.json` (merged into events) | |
| Materials/data | `Materials`, `MaterialCollected`, `Backpack.json`, `ShipLocker.json` | `src/assets/materials.json` maps ids. |
| Fleet Carrier | `CarrierStats` event (jump info, docks), `FSDJump` with carrier fields, `Location` when docked at carrier | Ownership/market via `CarrierStats`/`CarrierTradeOrder`; not centrally projected — read from journal or add your own projection. |
| Stored modules (storage) | `ShipLocker.json` (`Resources`), `ModuleStore/ModuleRetrieve` events, station `Outfitting.json` | No persistent "stored modules" projection — build one if needed. |

The clean way to maintain state: register a **projection**
(`helper.register_projection(projection)`) — subclass
`EventManager.Projection` with a Pydantic `StateModel` and `process(event)`;
the manager keeps one state object per projection class and exposes them to
side effects as `projected_states[<ClassName>]`.

---

## 5. COVAS interaction

- **Display text**: the AI's chat replies render in the UI automatically.
  Plugins can push UI messages with `lib.UI.emit_message` /
  `send_message` (thread-safe, single writer lock) — message types include
  `event` (`emit_message("event", event=...)`), `ui` (`show` panel), and
  `genui` (code rendered by the `gen-ui-render` component). For settings
  UI, republish plugin settings instead:
  `PluginManager.update_plugin_setting(...)` or `republish_settings()`.
- **Spoken responses**: the **Assistant** TTS speaks the AI reply. A plugin
  does not have a dedicated "say" API — the supported path is an **action**
  whose returned string is consumed by the LLM, which then answers (spoken).
  `helper._assistant` exposes the TTS stack internally but is not an
  official plugin surface.
- **Commands/intents**: `helper.register_action(name, description,
  parameters, method, action_type="ship", permission=None,
  input_template=None, cache_prefill=None)` — LLM function calling
  (OpenAI-compatible tools). `parameters` is a **pydantic** BaseModel (each
  field gets a JSON-schema description → typed parameters). `action_type`
  ("ship", …) buckets the action in the UI. `input_template(args, states)`
  can render a pre-filled prompt instead of a tool call ("cache" actions);
  `cache_prefill` pre-registers cached user phrases.
- **Confirmation**: no dedicated confirm dialog API. Patterns used: ask via
  the LLM reply ("confirm purchase?") then a follow-up action call, or a
  settings button, or ActionManager `permission` keys to gate actions
  (`allowed_actions` map).
- **Interruptions**: handled by the assistant/STT loop (barge-in), not
  exposed to plugins.
- **Panels/overlays**: the overlay windows exist in the Electron shell
  (desktop/VR overlay, `@covas-labs/electron-vr`) and `genui` messages can
  render generated UI in the chat area — plugin-facing docs are minimal;
  treat as experimental.

---

## 6. User commands

Voice → STT → LLM with the registered actions as **tools**; parsing is the
LLM's function-calling, guided by your `description` and pydantic parameter
descriptions. So:

- **Typed parameters**: any field type pydantic supports (module name, engineer
  name, system name, waypoint, priority…) — e.g.
  `class StartSessionParams(BaseModel): plan_name: str = Field(description=…)`.
- **Distinguishing intents**: separate action names ("what should I do next?"
  → `ship_upgrade_next_step`; "add waypoint" → an existing navigation
  action; "ignore" / "confirm purchase" → your own actions or arguments).
  Ambiguity resolution is the LLM's job — write crisp descriptions and
  pre-fill with `cache_prefill` for fixed phrases.
- **Existing actions are merged** with the built-ins (docking, targeting,
  navigation…) registered by `ActionManager`/assistant.

---

## 7. Configuration

- **Storage**: `config.json` (repo root) → `plugin_settings[guid][key]`.
  Read via `self.settings`; write via `update_plugin_setting` (persists +
  emits + hook).
- **UI**: `settings_config` (class/instance) — grids of typed fields,
  rendered by `ui/src/app/components/settings-grid`/`settings-field`.
  Supported field types (this fork):
  `text`, `textarea`, `number`, `toggle`, `select`, `paragraph`, `error`,
  `button` (optional `icon`), `filter` (compact live filter), `list`
  (rows with `group`, per-row actions routed as `action:<rowKey>`, optional
  `progress` blocks). Grids may be `collapsible` with `default_collapsed`,
  and can declare a `header_action` (button rendered beside the grid label).
- **Settings must be i18n keys** (this fork's policy): labels, placeholders
  and paragraph content are keys like `plugin.sum.btn.import`, resolved by
  the UI against `ui/src/app/services/i18n-translations.ts` (EN/FR).
- **Changeable at any time** (config state *and* runtime) — buttons are
  callable before Run (the upgrade plugin imports plans before Run). Do not
  add guards that reject settings actions before `on_chat_start`.
- **Data sources / route prefs / cache / verbosity / confirmations**: expose
  them as normal settings (select/toggle/number) — no special API.

Settings definition example (python, from the plugin):

```python
from lib.PluginSettingDefinitions import (
    SettingsGrid, ListSetting, ListAction, ListRow, ListRowProgress,
)

"grids": [{
    "key": "plans",
    "label": "plugin.sum.grid.plans",
    "header_action": {"key": "import_plan", "icon": "add",
                      "label": "plugin.sum.btn.import"},
    "fields": [{
        "key": "available_plans", "type": "list",
        "items": [{"key": "<plan_id>", "title": "Build",
                   "group": "Kestrel Mk II", "pending": False}],
        "row_actions": [{"action": "delete_plan", "icon": "delete",
                         "label": "plugin.sum.btn.delete", "danger": True}],
        "unit": "plan",
    }],
}]
```

---

## 8. External services

- **HTTP**: plain Python — `requests` and `httpx` are in the venv
  (`requirements.txt`). No sandbox, no domain allow-list, standard TLS.
- **Async**: not asyncio-based — use **threads** (the app is threaded:
  journal reader, watcher precedent). Long calls must not run on the event
  processing thread.
- **Rate limits / auth**: handle yourself (tokens in settings; do not commit
  secrets).
- **Third-party E:D databases**: the app already talks to Inara-style data
  via plugins; Spansh/EDSM reachable over HTTPS. A local cache (SQLite) is
  recommended (see §9).

---

## 9. File and data access

- **Writable**: `helper.get_plugin_data_path(manifest)` (your guid folder) —
  the only supported persistent-write location. DB files there survive
  plugin updates (documented in Development.md).
- **Read**: everything readable by the process: journals
  (`get_ed_journals_path`), assets
  (`lib.Config.get_asset_path("ship_names.json")` → `src/assets/`), app
  `config.json` (via `self.config` on the manager, or `self.settings`).
- **File watching**: no built-in watcher API — the Ship Upgrade Manager
  ships `_JournalWatcher` (daemon thread, binary tail with offset +
  remainder, 1 s poll) as the in-repo pattern to copy.
- **SQLite**: yes — used in production by the plugin
  (`plugin_data/<guid>/ship_upgrade_manager.db`, `sqlite3` stdlib,
  `PRAGMA foreign_keys`, one short-lived connection per operation via a
  `contextmanager`).
- **Packaging**: plugin folder is copied with the app when building
  (`build.ps1`); plugin dirs are `.gitignore`d in this repo — stage with
  `git add -f` when committing.

---

## 10. Performance

- **Threading model**: journal reading thread → `EventManager.process` →
  projections/side effects run **sequentially on that thread**; assistant
  actions run on the assistant loop thread; UI emits are lock-protected.
- **Rules of thumb**: keep side effects non-blocking (< a few ms); never
  call the network or big route algorithms inside a side effect — offload to
  your own daemon thread and push results back via
  `update_plugin_setting`/`republish_settings` (both thread-safe).
- **Route calculations**: background thread + debounce; consider caching in
  SQLite; there are **no cancellation tokens** — use `threading.Event`
  flags (watcher precedent) and idempotent writes.
- **Memory**: avoid unbounded event buffers; the journal reader keeps a
  `deque(maxlen=…)` for history (check exact limit in `EDJournal`).

---

## 11. Inter-plugin communication

- **No official bus.** The manager exposes
  `helper._plugin_manager.plugin_list` (all plugin instances) — technically
  accessible, used in-repo by the EDCoPilot plugin (reads TTS config of the
  app and coordinates mode selection with other plugins' settings).
- **Shared data**: the projected states (other projections registered by
  other plugins/core) are passed to every side effect
  (`projected_states[<ProjectionClass>]`) — this is the de-facto shared
  model.
- **Recommendation**: read other plugins' **settings** (`plugin_settings`
  of their guid) and project states, but write only your own.

---

## 12. Logging and diagnostics

- **Backend**: `from lib.Logger import log; log('info'|'warning'|'error'|'debug',
  message)` — emits structured JSON lines on stdout; the Electron wrapper
  (pino/pino-pretty) mirrors them into `logs/`.
- **Diagnostic console**: none for plugins (there is an internal
  `app_debug`/`debug_view` TUI hook in the fork, not plugin-facing).
- **Support export**: copy `logs/` + `config.json` (redact API keys). The
  Ship Upgrade Manager's DB can also be attached (SQLite, no credentials).

---

## 13. Packaging and distribution

- **Install**: copy the folder into `plugins/`; restart the app.
- **Update**: replace files (plugin_data survives). **Remove**: delete the
  folder (settings persist in config.json until cleaned manually).
- **Signing**: none.
- **Compatibility**: manifest `version`; Python 3.12. Settings migrations
  are your responsibility — two patterns in-repo: SQL table rebuilds in
  `_initialize_database` (the legacy `'ACTIVE'` singleton → v2 sessions
  migration) and a `plugin_meta` key-value table for metadata
  (`last_import`).
- **Dependencies**: vendor pure-python deps under `/deps` or add
  `requirements.txt` for the build step (`build:py`).

---

## 14. Limitations (official vs workarounds)

**Officially supported**:
- One folder = one plugin, loaded at app start; runtime hooks tied to
  Run/Stop.
- Side effects/projections/actions/status generators via `PluginHelper`.
- Settings UI via `settings_config`; persistence via `plugin_data` +
  `config.json`.
- Journal/status data as above.

**Fork additions (this checkout, not upstream)**:
- `list`/`filter`/`header_action` settings field types; i18n-key policy with
  `i18n-translations.ts`; per-row actions (`action:<rowKey>` + optional
  `value` for inline edit); two-criteria progress contract; `PluginBase._manager`
  backref + `PluginManager.republish_settings()` (broadcast without
  persisting).

**Hard limitations / data you cannot get directly**:
- Voice actions only exist after **Run** — config-state interactivity
  requires the journal-watcher workaround.
- No direct "plugin says something via TTS" API; no confirm-dialog API; no
  cancellation tokens; no official inter-plugin bus; no file-watcher API.
- Journal **replay only covers the newest log**; older history is lost
  unless your plugin reads files itself (baseline pattern).
- Station/catalogue files (`Outfitting.json`, `Shipyard.json`) are
  **catalogues**, not ship progression snapshots.
- UI language (EN/FR) is resolved **client-side** — the backend cannot know
  it; send i18n keys, not translated strings.
- No signing; no auto-update; plugin dirs are gitignored in this repo.

---

## 15. Minimal prototype

`plugins/MiniNav/manifest.json`:

```json
{
  "guid": "a1b2c3d4-0000-4000-8000-000000000001",
  "name": "MiniNav",
  "author": "demo",
  "version": "0.1.0",
  "repository": "",
  "description": "Smallest COVAS plugin: reads system+ship, one command, one setting.",
  "entrypoint": "mini_nav.py"
}
```

`plugins/MiniNav/mini_nav.py`:

```python
from pydantic import BaseModel, Field

from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginSettingDefinitions import PluginSettings, SettingsGrid, ToggleSetting
from lib.Event import GameEvent
from lib.Logger import log


class AdviceParams(BaseModel):
    waypoint: str | None = Field(default=None, description="Optional system to route to")


class MiniNavPlugin(PluginBase):
    """a) starts b) reads system+ship c) one voice command
    d) returns text (spoken) e) one persistent setting."""

    settings_config: PluginSettings = {
        "key": "mini-nav-guid",
        "label": "MiniNav",
        "icon": "explore",
        "grids": [{
            "key": "general",
            "label": "plugin.mininav.general",
            "fields": [{
                "key": "verbose", "label": "plugin.mininav.verbose",
                "type": "toggle", "readonly": False, "placeholder": None,
                "default_value": False,
            }],
        }],
    }

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.current_system = None   # from Location/FSDJump side effect
        self.current_ship = None     # from Loadout side effect

    def on_chat_start(self, helper) -> None:
        helper.register_sideeffect(self._on_event)          # (e) reacts to events
        helper.register_action(
            name="mini_nav_advice",
            description="Recommend the next action; optionally route to a system",
            parameters=AdviceParams,
            method=self._advice,                            # (c) voice command
            action_type="ship",
        )
        # (e) persistent setting demo
        manager = helper._plugin_manager
        manager.update_plugin_setting(
            self.plugin_manifest.guid, "verbose", self.settings.get("verbose", False)
        )

    def _on_event(self, event: Event, projected_states: dict) -> None:
        if not isinstance(event, GameEvent):
            return
        if event.content.get("event") in ("Location", "FSDJump", "CarrierJump"):
            self.current_system = event.content.get("StarSystem")
        elif event.content.get("event") == "Loadout":
            self.current_ship = event.content.get("Ship")

    def _advice(self, args: AdviceParams, context: dict) -> str:   # (d) spoken reply
        return (
            f"You are in {self.current_system or 'an unknown system'} "
            f"on your {self.current_ship or 'ship'}. "
            f"{'Route set to ' + args.waypoint if args.waypoint else 'Buy modules next.'}"
        )

    def on_settings_button(self, key: str, value: str | None = None) -> None:
        if key.startswith("verbose:"):
            self._verbose = True
            log("info", "MiniNav verbose enabled")
```

What it does NOT include (see §14): overlay panels, direct TTS, confirm
dialogs, historical replay.

**Validation baseline**: the repo test suite currently reports **155
passing tests** in the global run plus **54 tests** in
`test/plugins/`+`test/lib/test_plugin_manager_settings.py`+
`test_ship_upgrade_plugin_registration.py` (the global run does not collect
`test/plugins` — `pytest.ini` excludes any directory named `plugins`).
