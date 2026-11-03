import math
import re
import threading
import unicodedata
import uuid
from time import monotonic
from typing import Any, Callable, Mapping

from .Logger import log


POSITION_LIMITS = {
    "overlay_vr_horizontal_offset": (-2, 2),
    "overlay_vr_vertical_offset": (-1.5, 1.5),
    "overlay_vr_distance_offset": (-1.5, 1.5),
    "overlay_vr_tilt_degrees": (-90, 90),
    "overlay_vr_yaw_degrees": (-90, 90),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip().rstrip(".!?")


def normalize_command(text: str, assistant_name: str = "") -> str:
    text = normalize(text)
    if assistant_name:
        name = normalize(assistant_name)
        name_pattern = r"simon(?:e|ne)?" if name == "simone" else re.escape(name)
        text = re.sub(
            rf"^(?:(?:hey|ok(?:ay)?|dis|salut|bonjour)[,;:\s]+)?{name_pattern}[,.;:!?\s]+",
            "", text, count=1,
        )
    text = re.sub(r"\b(?:[ck]ovacs?)\b", "covas", text)
    text = re.sub(r"\boverlaid\b", "overlay", text)
    text = re.sub(r"^(?:s'il te plait|please)[,:\s]+|[,:\s]+(?:s'il te plait|please)$", "", text)
    return re.sub(
        r"^(?:on va|on peut|je veux|je voudrais|j'aimerais|tu peux|peux[- ]tu|pourrais[- ]tu|"
        r"can you|could you|let's)\s+", "", text,
    )


def is_adjustment_request(text: str, assistant_name: str = "") -> bool:
    return bool(re.fullmatch(
        r"(?:ajuste|ajutse|ajuster|regle|regler|adjust|set up) "
        r"(?:(?:la |the )?position (?:de |of )?)?"
        r"(?:(?:la|le|the) |l')?"
        r"(?:(?:fenetre|gui|interface|window)(?: (?:de |d'|of )?(?:l')?overlay)?|overlay)"
        r"(?: vr)?(?: (?:de |of )?covas(?:\s*[: -]?\s*next)?)?",
        normalize_command(text, assistant_name),
    ))


def is_recenter_request(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:(?:reinitialise[rz]?|recentre[rz]?) la vue(?: vr)?"
        r"|(?:reset|recenter|recentre) (?:the )?(?:vr )?view)",
        text,
    ))


def parse_increment(text: str, rotation: bool) -> float:
    text = re.sub(r"^(?:de|by|of)\s+", "", text).strip()
    if not text:
        return 5.0 if rotation else 0.05
    units = r"(?:degres?|degrees?|\u00b0)" if rotation else r"(?:centimetres?|centimeters?|cm|metres?|meters?|m)"
    match = re.fullmatch(rf"(.+?)\s*({units})?", text)
    if not match:
        raise ValueError("Invalid increment.")
    number, unit = match.groups()
    number = number.strip().replace(",", ".").replace("\u2212", "-")
    number = re.sub(r"^(?:moins|minus)\s+", "-", number)
    number = re.sub(r"^(?:plus|positive)\s+", "+", number)
    words = {
        "zero": 0, "un": 1, "une": 1, "one": 1, "deux": 2, "two": 2, "trois": 3, "three": 3,
        "quatre": 4, "four": 4, "cinq": 5, "five": 5, "six": 6, "sept": 7, "seven": 7,
        "huit": 8, "eight": 8, "neuf": 9, "nine": 9, "dix": 10, "ten": 10,
        "onze": 11, "eleven": 11, "douze": 12, "twelve": 12, "treize": 13, "thirteen": 13,
        "quatorze": 14, "fourteen": 14, "quinze": 15, "fifteen": 15, "seize": 16, "sixteen": 16,
        "vingt": 20, "twenty": 20, "trente": 30, "thirty": 30, "quarante": 40, "forty": 40,
        "cinquante": 50, "fifty": 50, "soixante": 60, "sixty": 60, "ninety": 90,
    }

    def numeric(part: str) -> str:
        return str(words[part]) if part in words else part

    sign = ""
    if number.startswith(("-", "+")):
        sign, number = number[0], number[1:].strip()
    decimals = re.split(r"\s+(?:virgule|point)\s+", number)
    number = sign + ".".join(numeric(part) for part in decimals)
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", number):
        raise ValueError("Use a number in metres, centimetres or degrees.")
    value = float(number)
    if not math.isfinite(value) or value == 0:
        raise ValueError("Use a finite, non-zero increment.")
    if unit and unit.startswith(("cent", "cm")):
        value /= 100
    return value


def parse_movement(text: str) -> tuple[str, float]:
    text = normalize(text)
    directions = (
        (r"(?:plus haut|up|higher)", "vertical", 1),
        (r"(?:plus bas|down|lower)", "vertical", -1),
        (r"(?:a gauche|gauche|left)", "horizontal", -1),
        (r"(?:a droite|droite|right)", "horizontal", 1),
        (r"(?:plus pres|plus proche|rapproche|closer|nearer)", "distance", -1),
        (r"(?:plus loin|eloigne|farther|further)", "distance", 1),
        (r"(?:rotation (?:sur |autour de )?(?:l'axe |axe )?horizontal|pitch)", "pitch", 1),
        (r"(?:rotation (?:sur |autour de )?(?:l'axe |axe )?vertical|yaw)", "yaw", 1),
    )
    for pattern, axis, sign in directions:
        match = re.fullmatch(rf"{pattern}(?:\s+(.+))?", text)
        if match:
            return axis, sign * parse_increment(match[1] or "", axis in ("pitch", "yaw"))
    raise ValueError("Unknown VR placement command.")


def reduction_request(text: str) -> str | None:
    match = re.fullmatch(
        r"(?:(?:(?:regle|mets|fixe) )?(?:le )?facteur de (?:reduction|zoom)"
        r"(?: de (?:la )?fenetre)?(?: (?:a|sur))?"
        r"|reduis (?:la )?fenetre (?:d'un |par un |par le )facteur(?: de)?"
        r"|(?:set )?(?:the )?(?:window )?(?:area )?(?:reduction|zoom) factor(?: to)?)"
        r"(?:\s+(.*))?",
        normalize(text),
    )
    return (match[1] or "") if match else None


class VrOverlayAdjustment:
    def __init__(
        self, *, get_config: Callable[[], Mapping[str, Any]], save: Callable[[dict[str, float]], None],
        emit: Callable[[dict[str, Any]], None], reply: Callable[[str, bool], None],
        recenter: Callable[[], bool],
        busy: Callable[[], bool], clock: Callable[[], float] = monotonic,
        assistant_name: str = "",
    ):
        self.get_config = get_config
        self.save = save
        self.emit = emit
        self.reply = reply
        self.recenter = recenter
        self.busy = busy
        self.clock = clock
        self.assistant_name = assistant_name
        self.active = False
        self.pending: tuple[str, str, float] | None = None
        self.draft: dict[str, float] = {}
        self.original: dict[str, float] = {}
        self.saved = False
        self.session_id = ""
        self.french = True
        self.lock = threading.RLock()

    def say(self, french: str, english: str, error: bool = False) -> None:
        message = french if self.french else english
        if error:
            log("error", message)
        self.reply(message, error)

    def request(self, operation: str, **values: Any) -> None:
        request_id = uuid.uuid4().hex
        self.pending = (request_id, operation, self.clock())
        self.emit({"type": "vr_placement_request", "request_id": request_id, "session_id": self.session_id, "operation": operation, **values})

    def publish(self) -> None:
        values = self.draft if self.active else {
            key: self.get_config().get(key, 0.0) for key in POSITION_LIMITS
        }
        self.emit({"type": "vr_placement_state", "active": self.active, "config": values})

    def request_recenter(self, command: str) -> str:
        with self.lock:
            if not is_recenter_request(normalize_command(command, self.assistant_name)):
                raise ValueError("VR recentering requires an explicit command such as 'recenter the VR view'; game events and window-focus requests cannot recenter VR.")
            config = self.get_config()
            if not config.get("game_actions_var") or config.get("allowed_actions", {}).get("recenterVRView") is not True:
                raise ValueError("Enable game actions and recenterVRView to recenter the view.")
            if self.pending:
                raise RuntimeError("The previous VR request is still pending.")
            self.request("recenter")
            return "Coordinated VR recenter requested. Await the completion message; this is not a confirmation of success."

    def handle_input(self, text: str) -> bool:
        with self.lock:
            text = normalize_command(text, self.assistant_name)
            if is_recenter_request(text):
                self.french = text.startswith(("reinitialis", "recentre la"))
                config = self.get_config()
                if not config.get("game_actions_var") or config.get("allowed_actions", {}).get("recenterVRView") is not True:
                    self.say("Active les actions de jeu et l'action recenterVRView pour recentrer la vue.", "Enable game actions and recenterVRView to recenter the view.", True)
                elif self.pending or self.busy():
                    self.say("Attends la fin de l'action en cours, puis redemande le recentrage.", "Wait for the current action to finish, then request recentering again.", True)
                else:
                    try:
                        self.request_recenter(text)
                    except Exception as error:
                        self.say(f"Recentrage VR impossible : {error}", f"Cannot recenter the VR view: {error}", True)
                return True
            start = is_adjustment_request(text)
            factor_text = reduction_request(text)
            if not self.active and not start and factor_text is None:
                return False
            if self.pending:
                self.say("Le r\u00e9glage pr\u00e9c\u00e9dent est encore en cours.", "The previous adjustment is still pending.", True)
                return True
            if not self.active:
                self.french = not normalize(text).startswith(("adjust", "set ", "reduction ", "zoom ", "area ", "window ", "the "))
                if not self.get_config().get("ui_actions_var", False):
                    self.say("Active les actions UI pour r\u00e9gler la fen\u00eatre.", "Enable UI actions to adjust the window.", True)
                    return True
                if self.busy():
                    self.say("Attends la fin de l'action en cours, puis redemande le r\u00e9glage.", "Wait for the current action to finish, then request adjustment again.", True)
                    return True
            if factor_text is not None:
                try:
                    if not factor_text or re.search(r"\b(?:degres?|degrees?)\b", factor_text):
                        raise ValueError("Missing or invalid factor.")
                    factor = parse_increment(factor_text, True)
                    if factor < 1:
                        raise ValueError("The factor must be at least 1.")
                except ValueError:
                    self.say("Indique un facteur de r\u00e9duction de surface sup\u00e9rieur ou \u00e9gal \u00e0 un, par exemple cinq.", "Specify an area reduction factor of at least one, for example five.", True)
                else:
                    self.request("set_reduction", factor=factor)
                return True
            if start and not self.active:
                self.active = True
                self.session_id = uuid.uuid4().hex
                self.original = {key: self.get_config().get(key, 0.0) for key in POSITION_LIMITS}
                self.saved = False
                self.request("begin")
                return True
            command = normalize(text)
            if command in ("annuler", "annule", "annuler la position", "cancel", "cancel adjustment"):
                self.request("cancel")
            elif command in ("valider", "valide", "valider la position", "valide la position", "validate position", "confirm position", "save position"):
                self.request("finish" if self.saved else "commit")
            elif self.saved:
                self.say("La position est enregistr\u00e9e. Dis valider la position pour terminer, ou annuler.", "The position is saved. Say confirm position to finish, or cancel.", True)
            else:
                try:
                    axis, delta = parse_movement(text)
                except ValueError:
                    self.say(
                        "Commande non reconnue. Dis plus haut, plus bas, \u00e0 gauche, \u00e0 droite, plus pr\u00e8s, plus loin, "
                        "rotation sur l'axe horizontal ou vertical, avec une valeur optionnelle en m\u00e8tres ou degr\u00e9s. "
                        "Tu peux aussi dire facteur de r\u00e9duction \u00e0 cinq. "
                        "Termine par valider la position ou annuler.",
                        "Unknown command. Say up, down, left, right, closer, farther, pitch or yaw, optionally with metres or degrees. "
                        "You can also say set reduction factor to five. "
                        "Finish with confirm position or cancel.", True,
                    )
                else:
                    self.request("move", axis=axis, delta=delta)
            return True

    def handle_result(self, message: dict[str, Any]) -> None:
        with self.lock:
            if not self.pending or message.get("request_id") != self.pending[0]:
                log("warn", "Ignoring an outdated VR placement result.")
                return
            operation = self.pending[1]
            self.pending = None
            if operation == "recenter":
                if message.get("success") is not True or message.get("recentered") is not True:
                    reason = str(message.get("error", "Invalid COVAS recenter acknowledgement"))
                    self.say(
                        f"Recentrage C:N non confirm\u00e9 : {reason}. Aucune commande envoy\u00e9e au jeu ni \u00e0 OpenKneeboard.",
                        f"COVAS recenter not confirmed: {reason}. No commands were sent to the game or OpenKneeboard.", True,
                    )
                    return
                try:
                    kneeboard_sent = self.recenter()
                except Exception as error:
                    self.say(f"C:N repositionn\u00e9e, mais recentrage incomplet : {error}", f"COVAS repositioned, but recentering is incomplete: {error}", True)
                else:
                    if kneeboard_sent:
                        self.say(
                            "C:N repositionn\u00e9e. Commandes de recentrage envoy\u00e9es au jeu et \u00e0 OpenKneeboard.",
                            "COVAS repositioned. Recenter commands sent to the game and OpenKneeboard.",
                        )
                    else:
                        self.say(
                            "C:N repositionn\u00e9e. Commande de recentrage envoy\u00e9e au jeu. OpenKneeboard indisponible, ignor\u00e9.",
                            "COVAS repositioned. Recenter command sent to the game. OpenKneeboard unavailable, skipped.",
                        )
                return
            if message.get("success") is not True:
                if operation == "begin":
                    self.active = False
                reason = str(message.get("error", "Unknown VR runtime error"))
                self.say(f"Impossible de r\u00e9gler la fen\u00eatre : {reason}", f"Cannot adjust the window: {reason}", True)
                self.publish()
                return
            if operation in ("set_reduction", "restore_reduction"):
                factor = message.get("reduction_factor")
                previous = message.get("previous_factor")
                if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1 for value in (factor, previous)):
                    self.say("R\u00e9ponse VR invalide pour le facteur de r\u00e9duction.", "Invalid VR reduction factor response.", True)
                    return
                if operation == "restore_reduction":
                    self.say("Ancien facteur de r\u00e9duction restaur\u00e9.", "Previous reduction factor restored.")
                else:
                    try:
                        self.save({"overlay_vr_gaze_reduction_factor": factor})
                    except (OSError, ValueError) as error:
                        self.say(f"Enregistrement du facteur impossible : {error}. Restauration de l'ancien facteur.", f"Cannot save the factor: {error}. Restoring the previous factor.", True)
                        self.request("restore_reduction", factor=previous)
                    else:
                        self.say(f"Facteur de r\u00e9duction de surface r\u00e9gl\u00e9 sur {factor:g} et enregistr\u00e9.", f"Area reduction factor set to {factor:g} and saved.")
                self.publish()
                return
            values = message.get("config")
            expected_active = operation not in ("finish", "cancel")
            if message.get("active") is not expected_active or not isinstance(values, dict) or set(values) != set(POSITION_LIMITS) or any(
                isinstance(values[key], bool) or not isinstance(values[key], (int, float))
                or not math.isfinite(values[key]) or not low <= values[key] <= high
                for key, (low, high) in POSITION_LIMITS.items()
            ):
                self.say("R\u00e9ponse VR invalide. Dis annuler pour restaurer la position.", "Invalid VR response. Say cancel to restore the position.", True)
                return
            self.draft = dict(values)
            if operation == "begin":
                self.say(
                    "D'accord. Tu peux dire plus haut, plus bas, \u00e0 gauche, \u00e0 droite, plus pr\u00e8s, plus loin, "
                    "rotation sur l'axe horizontal ou vertical. Les pas par d\u00e9faut sont cinq centim\u00e8tres et cinq degr\u00e9s. "
                    "Tu peux pr\u00e9ciser une valeur en m\u00e8tres, centim\u00e8tres ou degr\u00e9s, ou dire facteur de r\u00e9duction \u00e0 cinq. "
                    "Termine par valider la position ou annuler.",
                    "Okay. Say up, down, left, right, closer, farther, pitch or yaw. Default steps are five centimetres and five degrees. "
                    "You can specify metres, centimetres or degrees, or say set reduction factor to five. Finish with confirm position or cancel.",
                )
            elif operation == "commit":
                try:
                    self.save(self.draft)
                except (OSError, ValueError) as error:
                    self.say(f"Enregistrement impossible : {error}. Tu peux r\u00e9essayer ou annuler.", f"Cannot save: {error}. Retry or cancel.", True)
                else:
                    self.saved = True
                    self.request("finish")
            elif operation in ("finish", "cancel"):
                if operation == "cancel" and self.saved:
                    try:
                        self.save(self.original)
                    except (OSError, ValueError) as error:
                        self.say(f"Position VR restaur\u00e9e, mais restauration de la configuration impossible : {error}", f"VR position restored, but configuration rollback failed: {error}", True)
                        self.publish()
                        return
                self.active = False
                self.saved = False
                self.say(
                    "Position valid\u00e9e et enregistr\u00e9e." if operation == "finish" else "R\u00e9glage annul\u00e9, position initiale restaur\u00e9e.",
                    "Position confirmed and saved." if operation == "finish" else "Adjustment cancelled; original position restored.",
                )
            else:
                self.say("Position ajust\u00e9e.", "Position adjusted.")
            self.publish()

    def tick(self) -> None:
        with self.lock:
            if self.pending and self.clock() - self.pending[2] > 5:
                operation = self.pending[1]
                self.pending = None
                if operation == "recenter":
                    self.say(
                        "Recentrage C:N non confirm\u00e9 : le syst\u00e8me VR ne r\u00e9pond pas. Aucune commande envoy\u00e9e au jeu ni \u00e0 OpenKneeboard.",
                        "COVAS recenter was not confirmed: the VR system did not respond. No commands were sent to the game or OpenKneeboard.", True,
                    )
                    return
                self.say(
                    ("Le syst\u00e8me VR ne r\u00e9pond pas. Le mode de r\u00e9glage reste actif. Dis annuler pour restaurer la position."
                     if self.active else "Le syst\u00e8me VR ne r\u00e9pond pas. Le facteur n'est pas enregistr\u00e9. R\u00e9essaie la commande."),
                    ("The VR system did not respond. Adjustment mode remains active. Say cancel to restore the position."
                     if self.active else "The VR system did not respond. The factor was not saved. Retry the command."), True,
                )
