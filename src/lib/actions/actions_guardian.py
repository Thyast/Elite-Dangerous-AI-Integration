from ..ActionManager import ActionManager
from ..Projections import get_state_dict, ProjectedStates
from ..projections.guardian_unlocks import _catalog_keys, display_label, get_override_store, unlock_key, valid_module_names


def set_guardian_unlock(obj, projected_states: ProjectedStates):
    """Manually mark a Guardian module as unlocked or locked when the journal no longer holds the unlock."""
    module: str = (obj or {}).get('module', '')
    unlocked = (obj or {}).get('unlocked')

    if not isinstance(unlocked, bool):
        return "ERROR: 'unlocked' must be true or false."

    key = unlock_key(module) if isinstance(module, str) and module else None
    if key is None:
        return f"ERROR: Unknown Guardian module '{module}'. Expected one of: {valid_module_names()}."

    label = display_label(key, module)
    store = get_override_store()
    store.set(key, unlocked)
    for legacy, entitlement in _catalog_keys().legacy_keys.items():
        if entitlement == key:
            store.delete(legacy)

    journal_unlocked = get_state_dict(projected_states, 'GuardianUnlocks').get('Unlocked', {})
    if key in journal_unlocked:
        state = "unlocked" if unlocked else "locked"
        return (f"{label} is marked {state} manually, but the journal already records it as unlocked, "
                f"so the journal value is kept.")

    state = "unlocked" if unlocked else "locked"
    return f"{label} is now marked as {state}."


def register_guardian_actions(actionManager: ActionManager):
    actionManager.registerAction(
        'setGuardianUnlock',
        "Manually mark a Guardian module as unlocked or locked. Use this when the player says a Guardian module is unlocked or locked and the journal history does not show it.",
        {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": f"The Guardian module. Valid values: {valid_module_names()}",
                },
                "unlocked": {
                    "type": "boolean",
                    "description": "True if the module is unlocked, false if it is locked.",
                },
            },
            "required": ["module", "unlocked"]
        },
        set_guardian_unlock,
        'global',
    )
