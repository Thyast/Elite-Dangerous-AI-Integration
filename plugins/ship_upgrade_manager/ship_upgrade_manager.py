import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from lib.Event import Event, ProjectedEvent
from lib.EventManager import Projection
from lib.Logger import log
from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import PluginSettings


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
    step_id: str = Field(description="Identifier of the completed upgrade step")
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
                    "label": "Active session",
                    "fields": [
                        {
                            "key": "session_summary",
                            "label": "Session",
                            "type": "paragraph",
                            "readonly": True,
                            "placeholder": None,
                            "content": "No active session.",
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
            description="Mark a ship upgrade step as completed",
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
        self._publish_status()

    def on_chat_stop(self, helper: PluginHelper) -> None:
        self.helper = None

    def on_settings_changed(self) -> None:
        # Settings updates are already reflected in self.settings by PluginManager.
        pass

    def on_settings_button(self, key: str) -> None:
        if key == "reimport_plans":
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
                "SELECT id, plan_version, source_hash FROM plans "
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

            db.execute(
                """
                UPDATE plans
                SET source_json = ?, source_hash = ?, plan_version = ?,
                    last_modified = ?
                WHERE id = ?
                """,
                (source_json, source_hash, existing["plan_version"] + 1, now, existing["id"]),
            )
            return existing["id"]

    def list_plans(self) -> list[dict[str, Any]]:
        with self.get_db() as db:
            rows = db.execute(
                "SELECT id, ship_model, plan_name, plan_version FROM plans "
                "ORDER BY ship_model, plan_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def start_session(
        self,
        plan_name: str,
        ship_instance_id: str,
        ship_custom_name: str = "",
    ) -> dict[str, Any]:
        if not plan_name.strip() or not ship_instance_id.strip():
            raise ValueError("plan_name and ship_instance_id are required")

        with self.get_db() as db:
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
            db.execute("DELETE FROM active_session WHERE id = 'active'")
        self._publish_status()

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
        self.helper._plugin_manager.update_plugin_setting(
            self.plugin_manifest.guid, "import_status", status
        )
        self.helper._plugin_manager.update_plugin_setting(
            self.plugin_manifest.guid, "session_summary", session_summary
        )
