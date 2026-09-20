# COVAS:NEXT Agent Guide

This document is the working guide for agents extending COVAS:NEXT and its
Ship Upgrade Manager plugin. It records the architecture, integration points,
domain rules, supported input formats, development workflow, and validation
requirements established during the implementation.

## Handoff Snapshot

The current work is on branch `feat/ship-upgrade-plugin-api`. The repository
may contain uncommitted implementation changes when an agent starts:

- `plugins/ship_upgrade_manager/ship_upgrade_manager.py`
- `src/lib/PluginManager.py`
- `test/plugins/test_ship_upgrade_manager.py`
- `test/lib/test_plugin_manager_settings.py`
- this `AGENT.md`

These files belong to the Ship Upgrade Manager work and should be reviewed
before editing. The untracked `src/KeyboardLayoutExperiment.py` is unrelated
pre-existing user work and must remain untouched.

The import flow redesign described in the "Import Flow (Implemented)"
section below is implemented on this branch, on top of the i18n
infrastructure branch `feat/i18n-app-ngx-translate`. The test baseline is
155 Python tests in the global suite **plus** 31 tests in
`test/plugins/test_ship_upgrade_manager.py`, which the global run does not
collect (see Testing Expectations).

The latest validated baseline is **155 passing Python tests**. Start with:

```powershell
Set-Location 'G:\covas\Elite-Dangerous-AI-Integration'
. .\.venv312\Scripts\Activate.ps1
$env:PYTHONPATH='.'
python -m pytest -q
```

Do not assume the working tree is clean or that the latest commit contains all
current changes. Run `git status --short` and inspect the diff before staging
anything.

## Project Scope

COVAS:NEXT is an Elite Dangerous AI integration application with:

- A Python backend for the assistant, game journal ingestion, events, plugins,
  projections, side effects, and voice actions.
- An Angular/Electron UI for configuration and runtime interaction.
- A plugin architecture that allows features to be added without refactoring
  the core application.

The Ship Upgrade Manager is an additive plugin. Prefer extending the existing
plugin APIs and lifecycle over changing core behavior or introducing a parallel
framework.

## Repository Layout

Important locations:

- `src/`: Python application and shared plugin infrastructure.
- `src/lib/PluginBase.py`: plugin base class and manifest contract.
- `src/lib/PluginManager.py`: plugin loading, settings registration, settings
  persistence, settings buttons, and lifecycle dispatch.
- `src/lib/PluginHelper.py`: runtime registrations for actions, projections,
  side effects, status generators, and plugin data paths.
- `src/lib/EventManager.py`: event processing and projection/side-effect
  dispatch.
- `src/lib/EDJournal.py`: reads Elite Dangerous `Journal.*.log` files and
  enriches state events from files such as `ModulesInfo.json`.
- `src/Chat.py`: starts the chat/session runtime. Plugin `on_chat_start` is
  called after the user presses **Run**.
- `plugins/ship_upgrade_manager/`: Ship Upgrade Manager implementation.
- `test/`: Python tests.
- `ui/`: Angular frontend.
- `CONTRIBUTING.md`: environment setup and common development commands.

Do not modify or commit the pre-existing untracked
`src/KeyboardLayoutExperiment.py` unless the user explicitly requests it.

## Plugin Contract

The plugin manifest is:

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

The implementation entry point is
`plugins/ship_upgrade_manager/ship_upgrade_manager.py`, class
`ShipUpgradeManagerPlugin`.

The plugin currently provides:

- Plan import, listing, filtering, update, and deletion.
- Import versioning through `plan_version`, `import_date`, and
  `last_modified`.
- Detailed plan diffs: added, removed, changed, and unchanged steps.
- Migration of progress when a plan is updated.
- Concurrent sessions for multiple plans and ships.
- Session history, pause/resume, stop, completion tracking, and status output.
- Automatic module detection from journal events.
- Voice actions for previewing/applying changes and controlling sessions.
- Settings UI sections, collapsible grids, button icons, and status paragraphs.

The registered runtime actions are:

- `ship_upgrade_start_session`
- `ship_upgrade_complete_step`
- `ship_upgrade_pause_session`
- `ship_upgrade_resume_session`
- `ship_upgrade_stop_session`
- `ship_upgrade_next_step`
- `ship_upgrade_list_plans`
- `ship_upgrade_preview_changes`
- `ship_upgrade_apply_changes`

The settings buttons are `import_plan`, `preview_diff`, `apply_changes`,
`delete_plan`, and `reimport_plans`.

## Lifecycle: Config State Versus Run State

This distinction is essential:

1. COVAS:NEXT loads plugins and exposes settings.
2. The application remains in `config` state while the user prepares the
   session.
3. The user may import or edit plans before pressing **Run**.
4. Only after **Run** does `PluginManager.on_chat_start(plugin_helper)` run.

Settings buttons are therefore callable before `on_chat_start`. The Ship
Upgrade Manager must not require a runtime `PluginHelper` for configuration
operations.

The plugin initializes its SQLite database lazily/independently and uses the
same `plugin_data/<plugin-guid>` location before and after runtime startup.
After startup it uses `PluginHelper.get_plugin_data_path(...)` as the normal
path provider.

When a settings button changes plugin-owned paragraph values, the manager
refreshes and republishes the plugin settings configuration so import status,
plan lists, and session summaries are visible immediately in the UI.

Do not reintroduce a guard that simply rejects settings actions before
`on_chat_start`; that breaks the intended import workflow.

## Plugin Manager Settings Integration

`PluginManager.update_plugin_setting(...)` is the public path for changing a
plugin setting from backend code. It must:

- Persist the setting in the application configuration.
- Update the in-memory plugin settings.
- Refresh the relevant settings configuration.
- Invoke `PluginBase.on_settings_changed()` when appropriate.

Plugin settings buttons are routed through `PluginManager.on_settings_button`.
A plugin button callback must raise explicit errors for invalid input; do not
silently return a success-shaped fallback.

## Ship Upgrade Manager Data Model

The plugin owns a SQLite database in its plugin data directory. The main
tables are:

- `plans`: the latest imported plan, source JSON, stable plan ID, source hash,
  ship model, plan name, version, import date, and modification date.
- `plan_applications`: records plan application by plan and ship instance.
- `active_session`: one row per active `(plan, ship instance)` context.
- `step_history`: completion records scoped by `plan_session_id`.
- `session_history`: archived sessions and completion percentages.

Active sessions are not global. Their stable ID is derived from the plan ID
and ship instance ID, using a SHA-256 hash of:

```text
plan_id + "\0" + ship_instance_id
```

The old schema used the singleton ID `active`. Database initialization
migrates that schema by rebuilding the affected tables while preserving the
existing session and history data.

The current status paragraph intentionally summarizes the most recently active
session only. `list_sessions()` is the authoritative API for displaying all
active sessions; a future multi-session UI should use it instead of treating
`session_summary` as the complete state.

When updating a plan, migrate every active session associated with that plan.
Only steps whose IDs and step content are unchanged retain completion status.
Changed, removed, or otherwise incompatible steps must become incomplete.

## Session Selection Rules

Existing APIs preserve default behavior where possible, but session-aware
operations accept an optional `session_id` and/or context containing
`session_id` or `ship_instance_id`.

Available operations include:

- `start_session(plan_name, ship_instance_id, ship_custom_name="")`
- `get_session(session_id=None, context=None)`
- `list_sessions()`
- `complete_step(step_id, notes="", session_id=None, context=None)`
- `set_session_paused(paused, session_id=None, context=None)`
- `stop_session(session_id=None, context=None)`

Without an explicit ID, selection is deterministic: a matching ship context is
preferred, otherwise the most recently active session is used. New voice
actions should expose `session_id` when ambiguity matters.

## Journal Event Integration

The plugin listens to:

- `Loadout`
- `ModuleInfo`
- `ModuleBuy`
- `ModuleSwap`
- `ModuleStore`
- `ModuleRetrieve`

`EDJournal` reads the actual Elite Dangerous journal files directly. EDDI and
EDMC are optional; they are not required for module detection.

Important real-world payload differences:

- `Loadout` and `ModuleInfo` generally use canonical module IDs such as
  `hpt_beamlaser_gimbal_large`.
- `ModuleBuy`, `ModuleSwap`, `ModuleStore`, and `ModuleRetrieve` may use
  localized IDs such as `$hpt_beamlaser_gimbal_large_name;`.
- Normalize localized IDs by removing the leading `$` and the `_name;` suffix.
- Treat `ToItem: "Null"` as no installed module and do not complete a step from
  it.
- Ship identity may be supplied as `ShipID`, `ShipIdent`, `ShipName`, or
  `Ship`, depending on the event.

An event is routed to every compatible active session for the identified ship.
If an event has no ship identifier while active sessions belong to multiple
ships, skip automatic detection and log a warning rather than risking
progress on the wrong ship.

`ModulesInfo.json` is a useful current-loadout snapshot. `Outfitting.json` is
a station catalogue, not a ship progression snapshot; `Cargo.json` and
`ShipLocker.json` are unrelated to module installation tracking.

## Supported Plan Inputs

Plan parsing is implemented in
`plugins/ship_upgrade_manager/parsers.py`.

Supported inputs include:

- Normalized JSON plan payloads.
- SLEF envelopes of the form
  `[{"header": {...}, "data": {...}}]`, retaining source header metadata.
- Coriolis JSON, including nested component structures.
- Inara loadout structures with nested loadout trees.
- Compact EDSY URLs.
- Compact Coriolis URLs.

The compact EDSY decoder currently supports the supplied codec/hash case and
rejects unknown hashes rather than generating potentially incorrect modules.
The compact Coriolis support currently covers the supplied Kestrel Mk II link
shape and extracts its normalized module list and `bn` build name. Do not claim
that every possible compact Coriolis/EDSY payload is supported without adding
catalogue/codec coverage and regression tests.

## UI and Settings

The plugin settings definition supports:

- Paragraph/status fields.
- Action buttons.
- Collapsible settings grids.
- Default collapsed state.
- Material icon names.

Related frontend types and rendering are in:

- `ui/src/app/services/plugin-settings.ts`
- `ui/src/app/components/settings-grid/`
- `ui/src/app/components/settings-field/`

Keep backend settings keys and frontend setting types synchronized. When
adding a setting field, update both the backend definition and the frontend
type/rendering only when the field shape requires it.

## Import Flow (Implemented)

The Ship Upgrade Manager settings were redesigned with the user on 2026-09-20
and implemented on this branch. The interactive mockup
`maquette-import-flow.html` (untracked, repository root) remains the visual
reference and can be deleted once the UI is validated.

Structure:

1. The import entry point is a header action (`import_plan`, icon `add`)
   rendered to the right of the `plans` grid label; the tunnel grid is
   omitted entirely while idle. The header action disappears while the
   tunnel is open.
2. Grid `import` ("plugin.sum.grid.import"): a data-entry state (textarea,
   analyze, cancel); a diff state (rendered diff, confirm, modify, cancel);
   an imported state (keyed banner, the applied diff kept visible for
   tracking, new-import entry point).
3. Grid `plans` ("plugin.sum.grid.plans", displayed as "Plans"): search, a
   `list` settings field with one row per plan and a per-row trash action
   routed as `delete_plan:<plan id>`, refresh, and a delete status
   paragraph.
4. Grid `session` ("plugin.sum.grid.session"): unchanged summary.

The last applied import (banner params and diff HTML) persists in the
plugin database (`plugin_meta` table) so the confirmed diff stays visible
across restarts. Deleting the last imported plan resets the tunnel to idle.

Progress under each plan row reflects two criteria computed from the ship's
current loadout (runtime snapshot of `Loadout`/`ModuleInfo` events, falling
back to a journal read in config state): modules matched one-to-one against
the plan, and engineering levels summed as target vs currently reached — a
level only counts when the blueprint matches the target (strict). Event
completions still drive the step order and the suggested next module.

## Internationalization (i18n) Policy

All user-facing strings must be internationalized; hardcoding English text is
not acceptable:

- Plugin `settings_config` must transmit i18n keys — grid labels, field
  labels, placeholders, button labels, and paragraph content — not literal
  strings.
- Backend messages surfaced in the UI (status paragraphs, error text) must
  follow the same rule.
- The UI currently has no translation framework (no transloco/ngx-translate).
  Implementing this policy requires introducing one, or the project's chosen
  equivalent, with the import redesign.
- Additive changes must not introduce new user-facing hardcoded strings.

## Development Environment

Use the project Python 3.12 environment on Windows:

```powershell
. .\.venv312\Scripts\Activate.ps1
```

Run the backend test suite:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q
```

If invoking the environment explicitly:

```powershell
.\.venv312\Scripts\python.exe -m pytest -q
```

Node may be installed but absent from `PATH`. For the Angular build:

```powershell
$env:Path = 'C:\Program Files\nodejs;' + $env:Path
npm run build
```

The Angular build may report existing budget and CommonJS warnings. Treat
actual compilation failures as errors; do not hide them by broadening budgets
or suppressing diagnostics without a specific requirement.

## Testing Expectations

At minimum, add or update tests for:

- Import and CRUD before `on_chat_start`.
- Settings paragraph refresh after a button callback.
- Multiple plans on one ship.
- Multiple ships with simultaneous sessions.
- Session selection by ID and ship context.
- Event completion across all compatible sessions.
- Ambiguous events without ship identity.
- Migration of all sessions after plan changes.
- Legacy singleton database migration.
- Real journal field variants and localized module identifiers.
- Every newly supported external plan format.
- The import tunnel states (analyze, confirm, cancel, invalid input).
- Per-row plan deletion through the list field.

The global suite (`python -m pytest -q`) reports 155 passing tests but does
**not** collect `test/plugins/` because `pytest.ini` excludes any directory
named `plugins` through `norecursedirs`. Always run the plugin tests
explicitly (31 tests) in addition to the global suite. Do not "fix" this by
renaming directories without a lead-dev decision.

Useful targeted commands:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q test/plugins/ test/lib/test_plugin_manager_settings.py test/lib/test_ship_upgrade_plugin_registration.py
python -m pytest -q
```

For a frontend-only change, build the Angular application with the Node path
setup described above. A successful build can still emit the documented budget
and CommonJS warnings.

## Recommended Handoff Procedure

When taking over an unfinished task:

1. Read this file and inspect `git status --short`.
2. Review the current diff; preserve unrelated user changes.
3. Run the full Python suite before changing persistence or event behavior.
4. Reproduce the requested scenario with a focused regression test.
5. Make the smallest additive change in the existing architecture.
6. Run the focused test, then the full suite and `git diff --check`.
7. Report unsupported input or ambiguous ship identity explicitly; never guess.

When adding ignored plugin files to a commit, use `git add -f` only for the
intended files. Never stage `src/KeyboardLayoutExperiment.py` as part of this
feature.

## Coding and Change Policy

- Make additive, surgical changes; do not refactor COVAS:NEXT without a
  concrete requirement.
- Reuse existing plugin manager, event, settings, and helper abstractions.
- Preserve type safety and existing public behavior.
- Surface invalid input and operational errors explicitly.
- Avoid broad exception handling and silent fallbacks.
- Keep comments limited to non-obvious behavior.
- Use `git diff --check` before committing.
- Plugin directories are ignored by repository rules in this checkout; use
  `git add -f` for intended plugin files when committing.
- Commits follow Conventional Commits and include:

  ```text
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```

## Known Limitations

- The rendered plan-diff HTML (section titles "Added", "Removed",
  "Changed", the version line, and the new-plan stub) is still generated in
  English by the backend. This is a documented deviation from the i18n
  policy: keying rendered reports requires a backend-side translation
  surface that does not exist yet. Tunnel chrome, labels, placeholders,
  banners, and status messages are fully keyed.
- The compact EDSY and Coriolis URL implementations are not universal decoders.
  Unknown or unsupported payloads must fail explicitly.
- The status paragraph presents the most recently active session, while the
  database and `list_sessions()` retain all active sessions. UI work that
  needs simultaneous session management should expose an explicit session
  selector rather than assuming a single global session.
- Existing Angular build warnings concerning bundle budget and CommonJS
  dependencies remain non-blocking unless the project policy changes.
