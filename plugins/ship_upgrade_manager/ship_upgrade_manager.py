import hashlib
from html import escape
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from lib.Event import Event, GameEvent, ProjectedEvent
from lib.EventManager import Projection
from lib.Logger import log
from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import PluginSettings
from .parsers import PlanParseError, parse_plan_input


PLUGIN_GUID = "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e"


class ShipUpgradeState(BaseModel):
    plan_id: str | None = None
    plan_name: str | None = None
    ship_model: str | None = None
    ship_instance_id: str | None = None
    ship_custom_name: str | None = None
    current_step: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    total_steps: int = 0
    paused: bool = False


class ShipUpgradeProjection(Projection[ShipUpgradeState]):
    StateModel = ShipUpgradeState

    def process(self, event: Event) -> None:
        if not isinstance(event, ProjectedEvent):
            return
        if event.content.get("projection") != "ship_upgrade_manager":
            return
        payload = event.content.get("state")
        if isinstance(payload, dict):
            self.state = ShipUpgradeState.model_validate(payload)


class StartSessionParams(BaseModel):
    plan_name: str = Field(description="Name of the ship upgrade plan to start")
    ship_instance_id: str = Field(description="Unique identifier of the current ship")
    ship_custom_name: str = Field(default="", description="Optional ship name")


class CompleteStepParams(BaseModel):
    step_id: str = Field(description="Identifier of the completed upgrade module")
    notes: str = Field(default="", description="Optional completion notes")


class SessionActionParams(BaseModel):
    pass


class ShipUpgradeManagerPlugin(PluginBase):
    """Persist generic ship plans and per-ship upgrade sessions."""

    settings_schema_version = 1

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.helper: PluginHelper | None = None
        self.settings_config: PluginSettings = {
            "key": plugin_manifest.guid,
            "label": "Ship Upgrade Manager",
            "icon": "rocket_launch",
            "grids": [
                {
                    "key": "status",
                    "label": "Status",
                    "fields": [
                        {
                            "key": "plan_input",
                            "label": "Coriolis / EDSY / Inara JSON or URL",
                            "type": "textarea",
                            "readonly": False,
                            "placeholder": "Paste an exported JSON loadout or an embedded export URL",
                            "default_value": "",
                            "rows": 8,
                            "cols": 60,
                        },
                        {
                            "key": "import_plan",
                            "label": "Import plan",
                            "type": "button",
                            "readonly": False,
                            "placeholder": None,
                        },
                        {
                            "key": "preview_diff",
                            "label": "Preview plan changes",
                            "type": "button",
                            "readonly": False,
                            "placeholder": None,
                        },
                        {
                            "key": "diff_status",
                            "label": "Plan changes",
                            "type": "paragraph",
                            "readonly": True,
                            "placeholder": None,
                            "content": "No diff calculated.",
                        },
                        {
                            "key": "import_status",
                            "label": "Import status",
                            "type": "paragraph",
                            "readonly": True,
                            "placeholder": None,
                            "content": "No plans imported.",
                        },
                        {
                            "key": "reimport_plans",
                            "label": "Refresh plan status",
                            "type": "button",
                            "readonly": False,
                            "placeholder": None,
                        },
                    ],
                },
                {
                    "key": "session",
                    "label": "Active session (modules)",
                    "fields": [
                        {
                            "key": "session_summary",
                            "label": "Session",
                            "type": "paragraph",
                            "readonly": True,
                            "placeholder": None,
                            "content": "No active session.",
                        },
                        {
                            "key": "plan_filter",
                            "label": "Search plans",
                            "type": "text",
                            "readonly": False,
                            "placeholder": "Name or ship type",
                            "default_value": "",
                            "max_length": 100,
                            "min_length": 0,
                            "hidden": False,
                        },
                        {
                            "key": "plan_to_delete",
                            "label": "Plan to delete",
                            "type": "text",
                            "readonly": False,
                            "placeholder": "Exact plan name",
                            "default_value": "",
                            "max_length": 100,
                            "min_length": 0,
                            "hidden": False,
                        },
                        {
                            "key": "delete_plan",
                            "label": "Delete selected plan",
                            "type": "button",
                            "readonly": False,
                            "placeholder": None,
                            "icon": "delete",
                        },
                        {
                            "key": "available_plans",
                            "label": "Available plans",
                            "type": "paragraph",
                            "readonly": True,
                            "placeholder": None,
                            "content": "No plans imported.",
                        },
                    ],
                },
            ],
        }

    def on_chat_start(self, helper: PluginHelper) -> None:
        self.helper = helper
        self._initialize_database()
        helper.register_projection(ShipUpgradeProjection())
        helper.register_action(
            name="ship_upgrade_start_session",
            description="Start a ship upgrade plan for the current ship",
            parameters=StartSessionParams,
            method=self._start_session_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_complete_step",
            description="Mark a ship upgrade module as completed",
            parameters=CompleteStepParams,
            method=self._complete_step_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_pause_session",
            description="Pause the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._session_pause_action(),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_resume_session",
            description="Resume the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._session_resume_action(),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_stop_session",
            description="Stop the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._session_stop_action(),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_next_step",
            description="Describe the next module of the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._next_step_action(),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_list_plans",
            description="List available ship upgrade plans",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._list_plans_action(),
            action_type="ship",
        )
        helper.register_sideeffect(self._on_event)
        helper.register_status_generator(self._status_generator)
        self._publish_status()

    def on_chat_stop(self, helper: PluginHelper) -> None:
        self.helper = None

    def on_settings_changed(self) -> None:
        plan_filter = self.settings.get("plan_filter", "")
        if plan_filter != getattr(self, "_last_plan_filter", ""):
            self._publish_status()

    def on_settings_button(self, key: str) -> None:
        if key == "import_plan":
            self._import_from_settings()
        elif key == "preview_diff":
            self._preview_diff_from_settings()
        elif key == "delete_plan":
            try:
                self.delete_plan(self.settings.get("plan_to_delete", ""))
                self._set_status("Plan deleted.")
            except ValueError as error:
                self._set_status(f"Delete error: {error}")
                log("error", f"Ship Upgrade Manager plan deletion failed: {error}")
        elif key == "reimport_plans":
            self._publish_status()
        else:
            log("warning", f"Unknown Ship Upgrade Manager settings button: {key}")

    def _start_session_action(
        self, args: StartSessionParams, _context: dict[str, Any]
    ) -> str:
        session = self.start_session(
            args.plan_name, args.ship_instance_id, args.ship_custom_name
        )
        return (
            f"Started {session['plan_name']} on "
            f"{session['ship_custom_name'] or session['ship_instance_id']}. "
            f"{session['total_steps']} steps available."
        )

    def _complete_step_action(
        self, args: CompleteStepParams, _context: dict[str, Any]
    ) -> str:
        session = self.complete_step(args.step_id, args.notes)
        return (
            f"Completed {args.step_id}. "
            f"Progress: {len(session['completed_steps'])}/{session['total_steps']}."
        )

    def _session_pause_action(self) -> str:
        self.set_session_paused(True)
        return "Ship upgrade session paused."

    def _session_resume_action(self) -> str:
        self.set_session_paused(False)
        return "Ship upgrade session resumed."

    def _session_stop_action(self) -> str:
        self.stop_session()
        return "Ship upgrade session stopped."

    def _next_step_action(self) -> str:
        session = self.get_session()
        if session is None:
            return "There is no active ship upgrade session."
        remaining = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        if not remaining:
            return f"Plan {session['plan_name']} is complete."
        step = remaining[0]
        return (
            f"Next module, {len(session['completed_steps']) + 1} of "
            f"{session['total_steps']}: {step.get('label', step['id'])}."
        )

    def _list_plans_action(self) -> str:
        plans = self.list_plans()
        if not plans:
            return "No ship upgrade plans are imported."
        return "Available plans: " + "; ".join(
            f"{plan['plan_name']} for {plan['ship_model']} version {plan['plan_version']}"
            for plan in plans
        )

    def _import_from_settings(self) -> None:
        value = self.settings.get("plan_input", "")
        try:
            normalized = parse_plan_input(value)
            diff = self.diff_plan(
                normalized["plan_name"],
                normalized,
            )
            plan_id = self.import_plan(
                normalized["ship_model"],
                normalized["plan_name"],
                normalized,
            )
            change_summary = (
                f"{diff['added_count']} added, "
                f"{diff['removed_count']} removed, "
                f"{diff['changed_count']} changed"
                if diff["current_version"] is not None
                else "new plan"
            )
            self._set_status(
                f"Imported '{normalized['plan_name']}' for {normalized['ship_model']} "
                f"({len(normalized['steps'])} modules; {change_summary})."
            )
            log("info", f"Imported Ship Upgrade Manager plan {plan_id}")
        except (PlanParseError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._set_status(f"Import error: {error}")
            log("error", f"Ship Upgrade Manager plan import failed: {error}")

    def _preview_diff_from_settings(self) -> None:
        value = self.settings.get("plan_input", "")
        try:
            normalized = parse_plan_input(value)
            diff = self.diff_plan(normalized["plan_name"], normalized)
            rendered = self._format_plan_diff(diff)
            self.settings["diff_status"] = rendered
            if self.helper is not None:
                self.helper._plugin_manager.update_plugin_setting(
                    self.plugin_manifest.guid, "diff_status", rendered
                )
        except (PlanParseError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._set_diff_status(f"Diff error: {error}")
            log("error", f"Ship Upgrade Manager plan diff failed: {error}")

    def _set_diff_status(self, status: str) -> None:
        self.settings["diff_status"] = status
        if self.helper is not None:
            self.helper._plugin_manager.update_plugin_setting(
                self.plugin_manifest.guid, "diff_status", status
            )

    @staticmethod
    def _format_plan_diff(diff: dict[str, Any]) -> str:
        if diff["current_version"] is None:
            return "<p>New plan; all modules will be added.</p>"

        def module_label(module: dict[str, Any]) -> str:
            label = module.get("item") or module.get("label") or module.get("id", "")
            slot = module.get("slot")
            return escape(f"{label} ({slot})" if slot else str(label))

        sections = [
            ("Added", diff["added"], "added"),
            ("Removed", diff["removed"], "removed"),
        ]
        body = "".join(
            f"<p><strong>{title} ({len(modules)})</strong></p><ul>"
            + "".join(f"<li>{module_label(module)}</li>" for module in modules)
            + "</ul>"
            for title, modules, _ in sections
            if modules
        )
        changed_body = "".join(
            f"<li>{module_label(item['before'])} → {module_label(item['after'])}</li>"
            for item in diff["changed"]
        )
        if changed_body:
            body += f"<p><strong>Changed ({len(diff['changed'])})</strong></p><ul>{changed_body}</ul>"
        return (
            f"<p>Version {diff['current_version']} → {diff['next_version']}</p>"
            + (body or "<p>No module changes.</p>")
        )

    def _set_status(self, status: str) -> None:
        self.settings["import_status"] = status
        if self.helper is not None:
            self.helper._plugin_manager.update_plugin_setting(
                self.plugin_manifest.guid, "import_status", status
            )
        self._publish_status()

    def _on_event(self, event: Event, _context: dict[str, Any]) -> None:
        if not isinstance(event, GameEvent):
            return
        event_name = event.content.get("event")
        if event_name not in {"Loadout", "ModuleBuy", "ModuleSwap"}:
            return
        session = self.get_session()
        if session is None or session["paused"]:
            return
        ship_id = str(event.content.get("ShipID") or event.content.get("ShipIdent") or "")
        if ship_id and ship_id not in {
            str(session["ship_instance_id"]),
            str(session.get("ship_custom_name") or ""),
        }:
            return
        modules = self._event_modules(event.content)
        for step in session["steps"]:
            if step["id"] in session["completed_steps"]:
                continue
            if any(self._module_matches_step(step, module) for module in modules):
                try:
                    self.complete_step(step["id"], "Detected from Loadout event")
                except ValueError as error:
                    log("warning", f"Could not auto-complete ship upgrade step: {error}")

    @staticmethod
    def _event_modules(content: dict[str, Any]) -> list[dict[str, Any]]:
        event_name = content.get("event")
        if event_name == "Loadout":
            modules = content.get("Modules", [])
            return [
                {
                    "item": module.get("Item"),
                    "slot": module.get("Slot"),
                    "engineering": module.get("Engineering"),
                }
                for module in modules
                if isinstance(module, dict)
            ] if isinstance(modules, list) else []
        if event_name == "ModuleBuy":
            return [{"item": content.get("BuyItem"), "slot": content.get("Slot")}]
        if event_name == "ModuleSwap":
            return [{"item": content.get("ToItem"), "slot": content.get("ToSlot")}]
        return []

    @staticmethod
    def _module_matches_step(step: dict[str, Any], module: dict[str, Any]) -> bool:
        item = step.get("item")
        slot = step.get("slot")
        if not isinstance(item, str) or not item:
            return False
        if module.get("item") != item or (slot and module.get("slot") != slot):
            return False
        expected_engineering = step.get("engineering")
        if not expected_engineering:
            return True
        actual_engineering = module.get("engineering")
        if not isinstance(actual_engineering, dict):
            return False
        for key in ("BlueprintName", "Level", "ExperimentalEffect"):
            expected = expected_engineering.get(key)
            if expected is not None and actual_engineering.get(key) != expected:
                return False
        return True

    def _status_generator(self, _states: dict[str, BaseModel]) -> list[tuple[str, Any]]:
        session = self.get_session()
        if session is None:
            return []
        remaining = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        next_step = remaining[0].get("label", remaining[0]["id"]) if remaining else "complete"
        return [
            (
                "Ship upgrade",
                f"{session['plan_name']} on "
                f"{session['ship_custom_name'] or session['ship_instance_id']}: "
                f"{len(session['completed_steps'])}/{session['total_steps']} complete; "
                f"next: {next_step}",
            )
        ]

    def get_db_path(self) -> Path:
        if self.helper is None:
            raise RuntimeError("Ship Upgrade Manager is not started")
        return Path(self.helper.get_plugin_data_path(self.plugin_manifest)) / "ship_upgrade_manager.db"

    @contextmanager
    def get_db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.get_db_path())
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self.get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    ship_model TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    source_json TEXT,
                    source_hash TEXT,
                    import_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_modified TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ship_model, plan_name)
                );

                CREATE TABLE IF NOT EXISTS plan_applications (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    applied_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    plan_version_applied INTEGER NOT NULL,
                    UNIQUE(plan_id, ship_instance_id)
                );

                CREATE TABLE IF NOT EXISTS active_session (
                    id TEXT PRIMARY KEY CHECK (id = 'active'),
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    completed_steps TEXT NOT NULL DEFAULT '[]',
                    session_start TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    plan_version_at_session_start INTEGER NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS step_history (
                    id TEXT PRIMARY KEY,
                    plan_session_id TEXT NOT NULL REFERENCES active_session(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    plan_version_at_completion INTEGER NOT NULL,
                    notes TEXT,
                    UNIQUE(plan_session_id, step_id)
                );

                CREATE TABLE IF NOT EXISTS session_history (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    completed_steps TEXT NOT NULL DEFAULT '[]',
                    session_start TEXT NOT NULL,
                    session_end TEXT NOT NULL,
                    plan_version_at_session_start INTEGER NOT NULL,
                    completion_percent REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_plan_applications_ship
                    ON plan_applications(ship_instance_id);
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(active_session)").fetchall()
            }
            if "paused" not in columns:
                db.execute(
                    "ALTER TABLE active_session ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
                )

    def import_plan(
        self,
        ship_model: str,
        plan_name: str,
        source: dict[str, Any],
    ) -> str:
        """Insert or update a generic plan and return its stable plan id."""
        if not ship_model.strip() or not plan_name.strip():
            raise ValueError("ship_model and plan_name are required")

        source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
        source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        with self.get_db() as db:
            existing = db.execute(
                "SELECT id, plan_version, source_hash, source_json FROM plans "
                "WHERE ship_model = ? AND plan_name = ?",
                (ship_model, plan_name),
            ).fetchone()
            if existing is None:
                plan_id = hashlib.sha256(
                    f"{ship_model}\0{plan_name}".encode("utf-8")
                ).hexdigest()
                db.execute(
                    """
                    INSERT INTO plans
                        (id, ship_model, plan_name, source_json, source_hash,
                         import_date, last_modified)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (plan_id, ship_model, plan_name, source_json, source_hash, now, now),
                )
                return plan_id

            if existing["source_hash"] == source_hash:
                return existing["id"]

            old_steps = self._plan_steps(existing["source_json"])
            new_steps = self._plan_steps(source_json)
            new_version = existing["plan_version"] + 1
            db.execute(
                """
                UPDATE plans
                SET source_json = ?, source_hash = ?, plan_version = ?,
                    last_modified = ?
                WHERE id = ?
                """,
                (source_json, source_hash, new_version, now, existing["id"]),
            )
            self._migrate_active_session(
                db, existing["id"], old_steps, new_steps, new_version, now
            )
            return existing["id"]

    def diff_plan(
        self,
        plan_name: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare an imported source with the currently stored plan."""
        if not plan_name.strip():
            raise ValueError("plan_name is required")
        incoming = self._plan_steps(json.dumps(source, sort_keys=True))
        ship_model = next(
            (
                value.strip()
                for value in (
                    source.get("ship_model"),
                    source.get("ship"),
                    source.get("Ship"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        with self.get_db() as db:
            if ship_model:
                row = db.execute(
                    "SELECT source_json, plan_version FROM plans "
                    "WHERE plan_name = ? AND ship_model = ? "
                    "ORDER BY last_modified DESC LIMIT 1",
                    (plan_name.strip(), ship_model),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT source_json, plan_version FROM plans "
                    "WHERE plan_name = ? ORDER BY last_modified DESC LIMIT 1",
                    (plan_name.strip(),),
                ).fetchone()
        if row is None:
            return {
                "plan_name": plan_name,
                "current_version": None,
                "next_version": 1,
                **self._diff_steps([], incoming),
            }
        return {
            "plan_name": plan_name,
            "current_version": row["plan_version"],
            "next_version": row["plan_version"] + 1,
            **self._diff_steps(self._plan_steps(row["source_json"]), incoming),
        }

    def list_plans(self) -> list[dict[str, Any]]:
        with self.get_db() as db:
            rows = db.execute(
                "SELECT id, ship_model, plan_name, plan_version FROM plans "
                "ORDER BY ship_model, plan_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Return one complete plan, including normalized modules."""
        if not plan_id.strip():
            raise ValueError("plan_id is required")
        with self.get_db() as db:
            row = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown plan: {plan_id}")
        plan = dict(row)
        plan["source"] = json.loads(plan.pop("source_json") or "{}")
        plan["modules"] = self._plan_steps(json.dumps(plan["source"]))
        return plan

    def update_plan(
        self,
        plan_id: str,
        ship_model: str,
        plan_name: str,
        source: dict[str, Any],
    ) -> str:
        """Update an existing plan without creating a different record."""
        current = self.get_plan(plan_id)
        if not ship_model.strip() or not plan_name.strip():
            raise ValueError("ship_model and plan_name are required")
        if (ship_model, plan_name) != (
            current["ship_model"],
            current["plan_name"],
        ):
            with self.get_db() as db:
                duplicate = db.execute(
                    "SELECT id FROM plans WHERE ship_model = ? AND plan_name = ?",
                    (ship_model, plan_name),
                ).fetchone()
            if duplicate is not None and duplicate["id"] != plan_id:
                raise ValueError("A plan with this ship model and name already exists")
        source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
        source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        old_steps = current["modules"]
        with self.get_db() as db:
            new_version = current["plan_version"] + (
                1 if current["source_hash"] != source_hash else 0
            )
            db.execute(
                """
                UPDATE plans
                SET ship_model = ?, plan_name = ?, source_json = ?,
                    source_hash = ?, plan_version = ?, last_modified = ?
                WHERE id = ?
                """,
                (
                    ship_model,
                    plan_name,
                    source_json,
                    source_hash,
                    new_version,
                    now,
                    plan_id,
                ),
            )
            if new_version != current["plan_version"]:
                self._migrate_active_session(
                    db,
                    plan_id,
                    old_steps,
                    self._plan_steps(source_json),
                    new_version,
                    now,
                )
        self._publish_status()
        return plan_id

    def delete_plan(self, plan_name: str) -> bool:
        """Delete a plan and its applications/session through foreign keys."""
        if not isinstance(plan_name, str) or not plan_name.strip():
            raise ValueError("plan_to_delete is required")
        with self.get_db() as db:
            plan = db.execute(
                "SELECT id FROM plans WHERE plan_name = ?", (plan_name.strip(),)
            ).fetchone()
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_name}")
            db.execute("DELETE FROM plans WHERE id = ?", (plan["id"],))
        self._publish_status()
        return True

    def start_session(
        self,
        plan_name: str,
        ship_instance_id: str,
        ship_custom_name: str = "",
    ) -> dict[str, Any]:
        if not plan_name.strip() or not ship_instance_id.strip():
            raise ValueError("plan_name and ship_instance_id are required")

        with self.get_db() as db:
            self._archive_active_session(db, datetime.now(timezone.utc).isoformat())
            plan = db.execute(
                "SELECT * FROM plans WHERE plan_name = ? ORDER BY last_modified DESC LIMIT 1",
                (plan_name,),
            ).fetchone()
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_name}")
            steps = self._plan_steps(plan["source_json"])
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                """
                INSERT INTO active_session
                    (id, plan_id, ship_instance_id, ship_custom_name,
                     completed_steps, session_start, last_activity,
                     plan_version_at_session_start, paused)
                VALUES ('active', ?, ?, ?, '[]', ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    ship_instance_id = excluded.ship_instance_id,
                    ship_custom_name = excluded.ship_custom_name,
                    current_step = 0,
                    completed_steps = '[]',
                    session_start = excluded.session_start,
                    last_activity = excluded.last_activity,
                    plan_version_at_session_start = excluded.plan_version_at_session_start,
                    paused = 0
                """,
                (
                    plan["id"],
                    ship_instance_id,
                    ship_custom_name or None,
                    now,
                    now,
                    plan["plan_version"],
                ),
            )
            db.execute(
                """
                INSERT INTO plan_applications
                    (id, plan_id, ship_instance_id, ship_custom_name,
                     plan_version_applied)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id, ship_instance_id) DO UPDATE SET
                    ship_custom_name = excluded.ship_custom_name,
                    applied_date = CURRENT_TIMESTAMP,
                    plan_version_applied = excluded.plan_version_applied
                """,
                (
                    f"{plan['id']}:{ship_instance_id}",
                    plan["id"],
                    ship_instance_id,
                    ship_custom_name or None,
                    plan["plan_version"],
                ),
            )
        self._publish_status()
        return self.get_session() or {}

    def get_session(self) -> dict[str, Any] | None:
        with self.get_db() as db:
            row = db.execute(
                """
                SELECT s.*, p.plan_name, p.ship_model, p.plan_version,
                       p.source_json
                FROM active_session s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.id = 'active'
                """
            ).fetchone()
        if row is None:
            return None
        session = dict(row)
        session["completed_steps"] = json.loads(session["completed_steps"] or "[]")
        session["steps"] = self._plan_steps(session.pop("source_json"))
        session["paused"] = bool(session["paused"])
        session["total_steps"] = len(session["steps"])
        return session

    def complete_step(self, step_id: str, notes: str = "") -> dict[str, Any]:
        if not step_id.strip():
            raise ValueError("step_id is required")
        session = self.get_session()
        if session is None:
            raise ValueError("No active upgrade session")
        if session["paused"]:
            raise ValueError("The active upgrade session is paused")
        valid_step_ids = {step["id"] for step in session["steps"]}
        if step_id not in valid_step_ids:
            raise ValueError(f"Unknown step: {step_id}")
        if step_id in session["completed_steps"]:
            return session

        completed = [*session["completed_steps"], step_id]
        current_step = next(
            (
                index
                for index, step in enumerate(session["steps"])
                if step["id"] not in completed
            ),
            len(session["steps"]),
        )
        now = datetime.now(timezone.utc).isoformat()
        with self.get_db() as db:
            db.execute(
                """
                UPDATE active_session
                SET current_step = ?, completed_steps = ?, last_activity = ?
                WHERE id = 'active'
                """,
                (current_step, json.dumps(completed), now),
            )
            db.execute(
                """
                INSERT INTO step_history
                    (id, plan_session_id, step_id, completed_at,
                     plan_version_at_completion, notes)
                VALUES (?, 'active', ?, ?, ?, ?)
                ON CONFLICT(plan_session_id, step_id) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    plan_version_at_completion = excluded.plan_version_at_completion,
                    notes = excluded.notes
                """,
                (
                    f"active:{step_id}",
                    step_id,
                    now,
                    session["plan_version"],
                    notes or None,
                ),
            )
        self._publish_status()
        return self.get_session() or {}

    def set_session_paused(self, paused: bool) -> dict[str, Any]:
        if self.get_session() is None:
            raise ValueError("No active upgrade session")
        with self.get_db() as db:
            db.execute(
                "UPDATE active_session SET paused = ?, last_activity = ? WHERE id = 'active'",
                (int(paused), datetime.now(timezone.utc).isoformat()),
            )
        self._publish_status()
        return self.get_session() or {}

    def stop_session(self) -> None:
        with self.get_db() as db:
            self._archive_active_session(db, datetime.now(timezone.utc).isoformat())
            db.execute("DELETE FROM active_session WHERE id = 'active'")
        self._publish_status()

    def list_session_history(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.get_db() as db:
            rows = db.execute(
                """
                SELECT h.*, p.plan_name, p.ship_model
                FROM session_history h
                JOIN plans p ON p.id = h.plan_id
                ORDER BY h.session_end DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["completed_steps"] = json.loads(item["completed_steps"] or "[]")
            history.append(item)
        return history

    def _archive_active_session(
        self, db: sqlite3.Connection, session_end: str
    ) -> None:
        row = db.execute(
            """
            SELECT s.*, p.plan_name, p.source_json
            FROM active_session s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.id = 'active'
            """
        ).fetchone()
        if row is None:
            return
        completed = json.loads(row["completed_steps"] or "[]")
        total = len(self._plan_steps(row["source_json"]))
        history_id = hashlib.sha256(
            f"{row['session_start']}\0{row['plan_id']}\0{row['ship_instance_id']}".encode()
        ).hexdigest()
        db.execute(
            """
            INSERT OR REPLACE INTO session_history
                (id, plan_id, ship_instance_id, ship_custom_name,
                 completed_steps, session_start, session_end,
                 plan_version_at_session_start, completion_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                row["plan_id"],
                row["ship_instance_id"],
                row["ship_custom_name"],
                json.dumps(completed),
                row["session_start"],
                session_end,
                row["plan_version_at_session_start"],
                (len(completed) / total * 100) if total else 100,
            ),
        )

    @staticmethod
    def _plan_steps(source_json: str | None) -> list[dict[str, Any]]:
        if not source_json:
            return []
        source = json.loads(source_json)
        raw_steps = source.get("steps", []) if isinstance(source, dict) else []
        if not isinstance(raw_steps, list):
            raise ValueError("Plan steps must be a list")
        steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                raise ValueError(f"Plan step {index} must be an object")
            step = dict(raw_step)
            step_id = step.get("id", f"step_{index}")
            if not isinstance(step_id, str) or not step_id.strip():
                raise ValueError(f"Plan step {index} has an invalid id")
            step["id"] = step_id
            steps.append(step)
        if len({step["id"] for step in steps}) != len(steps):
            raise ValueError("Plan step ids must be unique")
        return steps

    @staticmethod
    def _step_signature(step: dict[str, Any]) -> str:
        comparable = {key: value for key, value in step.items() if key != "id"}
        return json.dumps(comparable, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _diff_steps(
        cls,
        old_steps: list[dict[str, Any]],
        new_steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        old_by_id = {step["id"]: step for step in old_steps}
        new_by_id = {step["id"]: step for step in new_steps}
        added = [new_by_id[key] for key in new_by_id.keys() - old_by_id.keys()]
        removed = [old_by_id[key] for key in old_by_id.keys() - new_by_id.keys()]
        changed = [
            {"before": old_by_id[key], "after": new_by_id[key]}
            for key in old_by_id.keys() & new_by_id.keys()
            if cls._step_signature(old_by_id[key]) != cls._step_signature(new_by_id[key])
        ]
        changed_ids = {item["after"]["id"] for item in changed}
        unchanged = [
            new_by_id[key]
            for key in old_by_id.keys() & new_by_id.keys()
            if key not in changed_ids
        ]
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": unchanged,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
        }

    def _migrate_active_session(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        old_steps: list[dict[str, Any]],
        new_steps: list[dict[str, Any]],
        new_version: int,
        now: str,
    ) -> None:
        session = db.execute(
            "SELECT completed_steps FROM active_session "
            "WHERE id = 'active' AND plan_id = ?",
            (plan_id,),
        ).fetchone()
        if session is None:
            return

        completed = json.loads(session["completed_steps"] or "[]")
        old_by_id = {step["id"]: step for step in old_steps}
        new_by_id = {step["id"]: step for step in new_steps}
        migrated = [
            step_id
            for step_id in completed
            if step_id in new_by_id
            and step_id in old_by_id
            and self._step_signature(old_by_id[step_id])
            == self._step_signature(new_by_id[step_id])
        ]
        current_step = next(
            (
                index
                for index, step in enumerate(new_steps)
                if step["id"] not in migrated
            ),
            len(new_steps),
        )
        db.execute(
            """
            UPDATE active_session
            SET completed_steps = ?, current_step = ?,
                plan_version_at_session_start = ?, last_activity = ?
            WHERE id = 'active'
            """,
            (json.dumps(migrated), current_step, new_version, now),
        )
        db.execute(
            "DELETE FROM step_history WHERE plan_session_id = 'active' "
            "AND step_id NOT IN ({})".format(
                ",".join("?" for _ in migrated) or "''"
            ),
            migrated,
        )

    def _publish_status(self) -> None:
        if self.helper is None:
            return

        with self.get_db() as db:
            plan_count = db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            session = db.execute(
                """
                SELECT p.plan_name, s.ship_custom_name, s.ship_instance_id,
                       s.current_step
                FROM active_session s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.id = 'active'
                """
            ).fetchone()

        status = f"{plan_count} plan(s) available."
        plan_filter = str(self.settings.get("plan_filter", "")).strip().lower()
        self._last_plan_filter = plan_filter
        available_plans = self._format_available_plans(plan_filter)
        if session is None:
            session_summary = "No active session."
        else:
            ship = session["ship_custom_name"] or session["ship_instance_id"]
            session_summary = (
                f"{session['plan_name']} on {ship}; "
                f"current step {session['current_step']}."
            )

        self.settings["import_status"] = status
        self.settings["session_summary"] = session_summary
        manager = self.helper._plugin_manager
        manager.update_plugin_setting(self.plugin_manifest.guid, "import_status", status)
        manager.update_plugin_setting(
            self.plugin_manifest.guid, "session_summary", session_summary
        )
        manager.update_plugin_setting(
            self.plugin_manifest.guid, "available_plans", available_plans
        )

    def _format_available_plans(self, plan_filter: str = "") -> str:
        plans = [
            plan for plan in self.list_plans()
            if not plan_filter
            or plan_filter in plan["plan_name"].lower()
            or plan_filter in plan["ship_model"].lower()
        ]
        if not plans:
            return "No plans match the current filter." if plan_filter else "No plans imported."
        grouped: dict[str, list[dict[str, Any]]] = {}
        for plan in plans:
            grouped.setdefault(plan["ship_model"], []).append(plan)
        return "".join(
            f"<details open><summary>{ship_model} ({len(ship_plans)})</summary>"
            + "".join(
                f"<div><strong>{plan['plan_name']}</strong> · v{plan['plan_version']} "
                f"· {self._plan_module_count(plan['id'])} modules</div>"
                for plan in ship_plans
            )
            + "</details>"
            for ship_model, ship_plans in sorted(grouped.items())
        )

    def _plan_module_count(self, plan_id: str) -> int:
        with self.get_db() as db:
            row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return 0
        return len(self._plan_steps(row["source_json"]))
