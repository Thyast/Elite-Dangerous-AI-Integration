import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.Logger import log
from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import PluginSettings


PLUGIN_GUID = "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e"


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

    def get_db_path(self) -> Path:
        if self.helper is None:
            raise RuntimeError("Ship Upgrade Manager is not started")
        return Path(self.helper.get_plugin_data_path(self.plugin_manifest)) / "ship_upgrade_manager.db"

    def get_db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.get_db_path())
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

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
                    plan_version_at_session_start INTEGER NOT NULL
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
