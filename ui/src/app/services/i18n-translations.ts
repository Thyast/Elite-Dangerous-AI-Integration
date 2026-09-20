// Single source of truth for UI translations (EN/FR).
// This file is part of the build graph: adding a key hot-reloads under ng serve,
// unlike JSON files under src/assets which are only copied at dev-server startup.
export const TRANSLATIONS: Record<string, object> = {
  "en": {
    "common": {
      "cancel": "Cancel",
      "confirm": "Confirm"
    },
    "plugin": {
      "sum": {
        "label": "Ship Upgrade Manager",
        "grid": {
          "import": "Import plan",
          "plans": "Plans",
          "session": "Active session (modules)"
        },
        "step": {
          "data": "Plan data",
          "diff": "Diff & confirmation",
          "imported": "Imported"
        },
        "btn": {
          "import": "Import plan",
          "analyze": "Analyze plan",
          "confirm": "Confirm import",
          "modify": "Modify data",
          "newImport": "New import",
          "delete": "Delete this plan",
          "rename": "Rename this plan",
          "startSession": "Activate this plan"
        },
        "noImportYet": "No import yet.",
        "idleHint": "The import form only appears when needed — nothing is shown by default.",
        "planDataLabel": "Plan data — Coriolis / EDSY / Inara JSON or URL",
        "planDataPlaceholder": "Paste an exported JSON loadout or an embedded export URL",
        "analyzeHint": "\"Analyze\" computes the diff without importing.",
        "planChanges": "Plan changes",
        "confirmHint": "\"Confirm import\" applies the plan and migrates active sessions.",
        "lastChanges": "Last changes applied",
        "errFormat": "Import error: unrecognized plan format.",
        "errParse": "Plan analysis failed: {{detail}}",
        "errApply": "Import failed: {{detail}}",
        "errDelete": "Deletion failed: {{detail}}",
        "noPlans": "No plans imported.",
        "noSession": "No active session.",
        "search": "Search",
        "session": "Session",
        "searchPlaceholder": "Search by name or ship",
        "groupOne": "{{count}} plan",
        "groupMany": "{{count}} plans",
        "progress": {
          "inProgress": "In progress",
          "paused": "Paused",
          "nextModule": "Next module:",
          "suggestedModule": "Suggested first module:",
          "modulesLabel": "Modules",
          "engineeringLabel": "Engineering"
        },
        "msg": {
          "diffBanner": "Diff computed against {{plan}} — version {{from}} → {{to}}",
          "doneBanner": "Plan imported — {{plan}} · v{{version}} · {{modules}} modules · {{sessions}} active session(s) migrated",
          "imported": "Plan \"{{plan}}\" imported ({{modules}} modules; {{changes}}).",
          "deleted": "Plan \"{{plan}}\" deleted.",
          "deletedNotFound": "Plan \"{{plan}}\" not found — the name must match exactly.",
          "sessionActive": "{{plan}} — {{ship}} · step {{step}}/{{total}} · {{pct}}% completed",
          "renamed": "Plan renamed to \"{{name}}\".",
          "sessionStarted": "Session started for \"{{plan}}\"."
        },
        "errRename": "Rename failed: {{detail}}",
        "errStart": "Could not start the session: {{detail}}",
        "errNoShip": "No ship detected from the journal yet."
      }
    }
  },
  "fr": {
    "common": {
      "cancel": "Annuler",
      "confirm": "Confirmer"
    },
    "plugin": {
      "sum": {
        "label": "Ship Upgrade Manager",
        "grid": {
          "import": "Import de plan",
          "plans": "Plans",
          "session": "Session active (modules)"
        },
        "step": {
          "data": "Données du plan",
          "diff": "Diff & confirmation",
          "imported": "Importé"
        },
        "btn": {
          "import": "Importer un plan",
          "analyze": "Analyser le plan",
          "confirm": "Confirmer l'import",
          "modify": "Modifier les données",
          "newImport": "Nouvel import",
          "delete": "Supprimer ce plan",
          "rename": "Renommer ce plan",
          "startSession": "Activer ce plan"
        },
        "noImportYet": "Aucun import effectué.",
        "idleHint": "Le formulaire d'import n'apparaît qu'au besoin — rien n'est affiché par défaut.",
        "planDataLabel": "Données du plan — Coriolis / EDSY / Inara JSON ou URL",
        "planDataPlaceholder": "Collez un JSON de loadout exporté ou une URL d'export embarquée",
        "analyzeHint": "« Analyser » calcule le diff sans importer.",
        "planChanges": "Changements du plan",
        "confirmHint": "« Confirmer » applique le plan et migre les sessions actives.",
        "lastChanges": "Derniers changements appliqués",
        "errFormat": "Erreur d'import : format de plan non reconnu.",
        "errParse": "Échec de l'analyse du plan : {{detail}}",
        "errApply": "Échec de l'import : {{detail}}",
        "errDelete": "Échec de la suppression : {{detail}}",
        "noPlans": "Aucun plan importé.",
        "noSession": "Aucune session active.",
        "search": "Rechercher",
        "session": "Session",
        "searchPlaceholder": "Rechercher par nom ou vaisseau",
        "groupOne": "{{count}} plan",
        "groupMany": "{{count}} plans",
        "progress": {
          "inProgress": "En cours",
          "paused": "En pause",
          "nextModule": "Prochain module :",
          "suggestedModule": "Premier module suggéré :",
          "modulesLabel": "Modules",
          "engineeringLabel": "Ingénierie"
        },
        "msg": {
          "diffBanner": "Diff calculé contre {{plan}} — version {{from}} → {{to}}",
          "doneBanner": "Plan importé — {{plan}} · v{{version}} · {{modules}} modules · {{sessions}} session(s) active(s) migrée(s)",
          "imported": "Plan « {{plan}} » importé ({{modules}} modules ; {{changes}}).",
          "deleted": "Plan « {{plan}} » supprimé.",
          "deletedNotFound": "Plan « {{plan}} » introuvable — le nom doit correspondre exactement.",
          "sessionActive": "{{plan}} — {{ship}} · étape {{step}}/{{total}} · {{pct}}% complété",
          "renamed": "Plan renommé « {{name}} ».",
          "sessionStarted": "Session démarrée pour « {{plan}} »."
        },
        "errRename": "Échec du renommage : {{detail}}",
        "errStart": "Impossible de démarrer la session : {{detail}}",
        "errNoShip": "Aucun vaisseau détecté via le journal pour le moment."
      }
    }
  }
};
