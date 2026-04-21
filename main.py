"""Serveur FastAPI pour l'agent WhatsApp Supmedical Academy.

Architecture:
- matcher.py: normalisation + fuzzy matching (rapidfuzz)
- whatsapp_service.py: envoi texte/audio/boutons + upload media
- main.py: orchestration conversationnelle + webhook + lead capture
"""

from __future__ import annotations

import json
import os
import re
import time
from ipaddress import ip_address, ip_network
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from database import DEFAULT_GREETING, formations_db
from google_sheets_service import save_lead as save_lead_to_sheet
from matcher import (
    format_formations_menu,
    looks_like_menu_request,
    match_formation,
    normalize_text,
    parse_menu_selection,
)
from session_store import (
    cleanup_expired_sessions,
    delete_session as delete_session_from_db,
    init_session_store,
    load_session as load_session_from_db,
    save_session as save_session_to_db,
)
from whatsapp_service import (
    send_whatsapp_audio as _send_whatsapp_audio,
    send_whatsapp_buttons as _send_whatsapp_buttons,
    send_whatsapp_text as _send_whatsapp_text,
    upload_media_to_whatsapp,
)


load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

YES_WORDS = {"oui", "ok", "okay", "daccord", "d accord", "yes", "ouais", "bien sur"}
NO_WORDS = {"non", "no", "nop", "pas maintenant"}

app = FastAPI(title="Supmedical WhatsApp Bot")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

# Sessions légères en mémoire
# Exemple: {"2127...": {"state": "awaiting_callback_details", "formation": "...", "ts": 12345.6}}
sessions: Dict[str, Dict[str, Any]] = {}
MEDIA_CACHE_PATH = os.getenv("WHATSAPP_MEDIA_CACHE_PATH", "media_cache.json")
SESSION_TIMEOUT_SECONDS = _int_env("SESSION_TIMEOUT_SECONDS", 1800)
SESSION_TIMEOUT_NOTICE = "⌛ Votre session a expiré. Je vous renvoie le menu pour reprendre."
COEXISTENCE_ENABLED = _bool_env("WHATSAPP_COEXISTENCE_ENABLED", True)
COEXISTENCE_AUTO_DETECT = _bool_env("WHATSAPP_COEXISTENCE_AUTO_DETECT", True)
HUMAN_OVERRIDE_TIMEOUT_SECONDS = _int_env("HUMAN_OVERRIDE_TIMEOUT_SECONDS", 1800)
BOT_OUTBOUND_MESSAGE_TTL_SECONDS = _int_env("BOT_OUTBOUND_MESSAGE_TTL_SECONDS", 7200)
HANDOFF_API_TOKEN = (os.getenv("HANDOFF_API_TOKEN") or "").strip()
HANDOFF_ALLOWED_SOURCES = (os.getenv("HANDOFF_ALLOWED_SOURCES") or "").strip()
HUMAN_TAKEOVER_NOTICE = os.getenv(
    "HUMAN_TAKEOVER_NOTICE",
    "👩‍💼 Un conseiller humain prend le relais. Je reste en pause pour eviter les reponses en double.",
)
HUMAN_RELEASE_NOTICE = os.getenv(
    "HUMAN_RELEASE_NOTICE",
    "🤖 Merci. Je reprends automatiquement la conversation.",
)
HUMAN_REQUEST_KEYWORDS = {
    "conseiller",
    "agent",
    "humain",
    "humaine",
    "support",
    "service client",
    "service clientele",
}
BOT_RESUME_KEYWORDS = {
    "bot",
    "reprendre bot",
    "reprends bot",
    "retour bot",
    "resume bot",
}
MAX_AUDIO_RECOVERY_RETRIES = 1
AUDIO_RECOVERY_NOTICE = "🔁 L'audio a été rejeté par Meta. Je le renvoie en version compatible."


def log_event(level: str, phone: str | None, event: str, **fields: Any) -> None:
    payload = {
        "level": level.upper(),
        "phone": phone,
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


def load_media_cache() -> Dict[str, str]:
    if not os.path.exists(MEDIA_CACHE_PATH):
        return {}
    try:
        with open(MEDIA_CACHE_PATH, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items() if value}
    except Exception as exc:
        log_event("error", None, "media_cache_load_failed", path=MEDIA_CACHE_PATH, error=str(exc))
    return {}


def save_media_cache(cache: Dict[str, str]) -> None:
    try:
        with open(MEDIA_CACHE_PATH, "w", encoding="utf-8") as file_obj:
            json.dump(cache, file_obj, ensure_ascii=False, indent=2)
    except Exception as exc:
        log_event("error", None, "media_cache_save_failed", path=MEDIA_CACHE_PATH, error=str(exc))


def _normalize_session(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    try:
        ts = float(data.get("ts") or 0)
    except Exception:
        ts = 0.0
    if ts <= 0:
        data["ts"] = time.time()

    agent_mode = str(data.get("agent_mode") or "bot_mode").strip().lower()
    if agent_mode not in {"bot_mode", "human_active"}:
        agent_mode = "bot_mode"
    data["agent_mode"] = agent_mode

    if agent_mode == "human_active":
        try:
            human_ts = float(data.get("human_last_activity_ts") or 0)
        except Exception:
            human_ts = 0.0
        if human_ts <= 0:
            data["human_last_activity_ts"] = data["ts"]
    else:
        data.pop("human_last_activity_ts", None)

    return data


def set_session(phone: str, payload: Dict[str, Any]) -> None:
    data = _normalize_session(payload)
    sessions[phone] = data
    try:
        save_session_to_db(phone, data)
    except Exception as exc:
        log_event("error", phone, "session_persist_failed", error=str(exc))


def clear_session(phone: str) -> None:
    sessions.pop(phone, None)
    try:
        delete_session_from_db(phone)
    except Exception as exc:
        log_event("error", phone, "session_delete_failed", error=str(exc))


def _is_session_expired(payload: Dict[str, Any]) -> bool:
    try:
        ts = float(payload.get("ts") or 0)
    except Exception:
        return False
    if ts <= 0:
        return False
    if SESSION_TIMEOUT_SECONDS <= 0:
        return False
    return (time.time() - ts) > SESSION_TIMEOUT_SECONDS


def get_active_session(phone: str) -> tuple[Optional[Dict[str, Any]], bool]:
    session = sessions.get(phone)

    if not session:
        try:
            session = load_session_from_db(phone, SESSION_TIMEOUT_SECONDS)
        except Exception as exc:
            log_event("error", phone, "session_load_failed", error=str(exc))
            session = None
        if session:
            sessions[phone] = session

    if session and _is_session_expired(session):
        clear_session(phone)
        return None, True

    return session, False


def touch_session(phone: str, session: Optional[Dict[str, Any]]) -> None:
    if not session:
        return
    refreshed = dict(session)
    refreshed["ts"] = time.time()
    set_session(phone, refreshed)


def _is_human_mode(session: Optional[Dict[str, Any]]) -> bool:
    return str((session or {}).get("agent_mode") or "bot_mode").strip().lower() == "human_active"


def _is_human_mode_timed_out(session: Optional[Dict[str, Any]]) -> bool:
    if not _is_human_mode(session):
        return False
    if HUMAN_OVERRIDE_TIMEOUT_SECONDS <= 0:
        return False
    try:
        last_activity = float((session or {}).get("human_last_activity_ts") or 0)
    except Exception:
        last_activity = 0.0
    if last_activity <= 0:
        return False
    return (time.time() - last_activity) > HUMAN_OVERRIDE_TIMEOUT_SECONDS


def _activate_human_mode(
    phone: str,
    reason: str,
    actor: Optional[str] = None,
    session: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = dict(session or {})
    payload["agent_mode"] = "human_active"
    payload["human_last_activity_ts"] = time.time()
    payload["ts"] = time.time()
    set_session(phone, payload)
    log_event("info", phone, "human_mode_activated", reason=reason, actor=actor)
    return payload


def _release_to_bot_mode(
    phone: str,
    reason: str,
    actor: Optional[str] = None,
    session: Optional[Dict[str, Any]] = None,
    reset_conversation: bool = True,
) -> Dict[str, Any]:
    payload = dict(session or {})
    payload["agent_mode"] = "bot_mode"
    payload.pop("human_last_activity_ts", None)
    payload["ts"] = time.time()
    if reset_conversation:
        payload.pop("state", None)
        payload.pop("formation", None)
        payload.pop("score", None)
        payload.pop("lead_name", None)
    set_session(phone, payload)
    log_event("info", phone, "human_mode_released", reason=reason, actor=actor, reset=reset_conversation)
    return payload


def _looks_like_human_request(normalized: str) -> bool:
    text = normalized or ""
    return any(keyword in text for keyword in HUMAN_REQUEST_KEYWORDS)


def _looks_like_bot_resume_request(normalized: str) -> bool:
    text = normalized or ""
    return any(keyword in text for keyword in BOT_RESUME_KEYWORDS)


MEDIA_ID_CACHE = load_media_cache()
AUDIO_MESSAGE_CONTEXT: Dict[str, Dict[str, Any]] = {}
AUDIO_RECOVERY_ATTEMPTS: Dict[str, int] = {}
BOT_OUTBOUND_MESSAGE_CONTEXT: Dict[str, Dict[str, Any]] = {}

try:
    init_session_store()
    cleanup_expired_sessions(SESSION_TIMEOUT_SECONDS)
except Exception as exc:
    log_event("error", None, "session_store_init_failed", error=str(exc))


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "Supmedical WhatsApp Bot",
            "status": "ok",
            "webhook": "/webhook",
            "port": 8002,
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "supmedical-whatsapp-bot"})


def is_greeting(message: str) -> bool:
    text = normalize_text(message)
    greetings = {"bonjour", "bonsoir", "salut", "coucou", "hello", "hi", "bjr"}
    return any(word in text.split() for word in greetings)


def is_menu_reset_command(message: str) -> bool:
    normalized = normalize_text(message)
    tokens = normalized.split()
    if normalized == "0":
        return True
    if "0" in tokens:
        return True
    if "menu" in tokens:
        return True
    return False


def parse_name_and_callback(raw_text: str) -> Tuple[str, str]:
    text = (raw_text or "").strip()
    if not text:
        return "Prospect WhatsApp", "Non précisé"

    # Format attendu: "Nom - Date/Créneau"
    parts = re.split(r"\s*[-,;|]\s*", text, maxsplit=1)
    if len(parts) == 2:
        name = parts[0].strip() or "Prospect WhatsApp"
        callback = parts[1].strip() or "Non précisé"
        return name, callback

    return "Prospect WhatsApp", text


def send_formations_menu(phone: str) -> None:
    intro = "🧭 D'accord, je vous guide."
    send_whatsapp_text(phone, f"{intro}\n\n{format_formations_menu(formations_db)}")


def _extract_message_id(response: Any) -> Optional[str]:
    try:
        payload = response.json()
        messages = payload.get("messages") or []
        if messages and isinstance(messages[0], dict):
            msg_id = messages[0].get("id")
            if msg_id:
                return str(msg_id)
    except Exception:
        return None
    return None


def _cleanup_old_bot_outbound_messages() -> None:
    if not BOT_OUTBOUND_MESSAGE_CONTEXT:
        return

    now = time.time()
    stale_ids = [
        message_id
        for message_id, context in BOT_OUTBOUND_MESSAGE_CONTEXT.items()
        if (now - float(context.get("ts") or 0)) > BOT_OUTBOUND_MESSAGE_TTL_SECONDS
    ]
    for message_id in stale_ids:
        BOT_OUTBOUND_MESSAGE_CONTEXT.pop(message_id, None)


def _remember_bot_outbound_message(phone: str, message_id: str, channel: str) -> None:
    if not message_id:
        return
    BOT_OUTBOUND_MESSAGE_CONTEXT[message_id] = {
        "phone": phone,
        "channel": channel,
        "ts": time.time(),
    }
    if len(BOT_OUTBOUND_MESSAGE_CONTEXT) > 1500:
        _cleanup_old_bot_outbound_messages()
        if len(BOT_OUTBOUND_MESSAGE_CONTEXT) > 1200:
            oldest = sorted(
                BOT_OUTBOUND_MESSAGE_CONTEXT.items(),
                key=lambda item: float(item[1].get("ts") or 0),
            )[:300]
            for old_message_id, _ in oldest:
                BOT_OUTBOUND_MESSAGE_CONTEXT.pop(old_message_id, None)


def _track_bot_outbound_response(phone: str, channel: str, response: Any) -> None:
    message_id = _extract_message_id(response)
    if not message_id:
        return
    _remember_bot_outbound_message(phone, message_id, channel)


def _is_known_bot_message_id(message_id: str) -> bool:
    if not message_id:
        return False
    _cleanup_old_bot_outbound_messages()
    context = BOT_OUTBOUND_MESSAGE_CONTEXT.get(message_id)
    if not context:
        return False
    context["last_status_ts"] = time.time()
    return True


def send_whatsapp_text(to_number: str, message: str):
    response = _send_whatsapp_text(to_number, message)
    _track_bot_outbound_response(to_number, "text", response)
    return response


def send_whatsapp_audio(to_number: str, media_id: str):
    response = _send_whatsapp_audio(to_number, media_id)
    _track_bot_outbound_response(to_number, "audio", response)
    return response


def send_whatsapp_buttons(to_number: str, body_text: str, button_titles: list[str]):
    response = _send_whatsapp_buttons(to_number, body_text, button_titles)
    _track_bot_outbound_response(to_number, "interactive", response)
    return response


def _track_audio_message(phone: str, formation_name: str, media_id: str, response: Any) -> None:
    message_id = _extract_message_id(response)
    if not message_id:
        return

    AUDIO_MESSAGE_CONTEXT[message_id] = {
        "phone": phone,
        "formation": formation_name,
        "media_id": media_id,
        "ts": time.time(),
    }

    # Evite une croissance infinie en mémoire.
    if len(AUDIO_MESSAGE_CONTEXT) > 200:
        oldest = sorted(AUDIO_MESSAGE_CONTEXT.items(), key=lambda item: item[1].get("ts", 0))[:50]
        for msg_id, _ in oldest:
            AUDIO_MESSAGE_CONTEXT.pop(msg_id, None)


def _session_formation(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    session, _ = get_active_session(phone)
    return (session or {}).get("formation")


def _is_media_processing_failure(errors: list[dict]) -> bool:
    for error in errors:
        code = error.get("code")
        if code == 131053:
            return True
        details = ((error.get("error_data") or {}).get("details") or "").lower()
        if "media upload error" in details:
            return True
    return False


def _recover_failed_audio_status(recipient_id: Optional[str], status: Dict[str, Any]) -> None:
    errors = status.get("errors") or []
    if not _is_media_processing_failure(errors):
        return

    failed_message_id = str(status.get("id") or "")
    context = AUDIO_MESSAGE_CONTEXT.pop(failed_message_id, None) if failed_message_id else None

    phone = (context or {}).get("phone") or recipient_id
    formation_name = (context or {}).get("formation") or _session_formation(phone)

    if not phone or not formation_name:
        log_event(
            "error",
            recipient_id,
            "audio_recovery_skipped",
            reason="missing_context",
            failed_message_id=failed_message_id,
        )
        return

    retry_key = f"{phone}:{formation_name}"
    current_attempts = AUDIO_RECOVERY_ATTEMPTS.get(retry_key, 0)
    if current_attempts >= MAX_AUDIO_RECOVERY_RETRIES:
        log_event(
            "error",
            phone,
            "audio_recovery_skipped",
            reason="retry_limit_reached",
            formation=formation_name,
            failed_message_id=failed_message_id,
            attempts=current_attempts,
        )
        return

    AUDIO_RECOVERY_ATTEMPTS[retry_key] = current_attempts + 1
    log_event(
        "info",
        phone,
        "audio_recovery_started",
        formation=formation_name,
        failed_message_id=failed_message_id,
        retry=AUDIO_RECOVERY_ATTEMPTS[retry_key],
    )

    try:
        send_whatsapp_text(phone, AUDIO_RECOVERY_NOTICE)
    except Exception as exc:
        log_event("error", phone, "audio_recovery_notice_failed", error=str(exc))

    try:
        send_formation_multimodal(
            phone,
            formation_name,
            send_intro_text=False,
            force_audio_refresh=True,
        )
    except Exception as exc:
        log_event("error", phone, "audio_recovery_failed", formation=formation_name, error=str(exc))


def send_formation_multimodal(
    phone: str,
    formation_name: str,
    send_intro_text: bool = True,
    force_audio_refresh: bool = False,
) -> None:
    """Envoi texte puis audio (media_id prioritaire, sinon local_media)."""
    info = formations_db.get(formation_name, {})
    link = info.get("registration_link", "")

    if send_intro_text:
        text_msg = (
            f"🎓 *{formation_name}*\n"
            f"🔗 Programme et inscription : {link}\n"
            "🎧 Je vous envoie l'audio explicatif juste après."
        )
        send_whatsapp_text(phone, text_msg)
        time.sleep(1)

    media_id = None if force_audio_refresh else (MEDIA_ID_CACHE.get(formation_name) or info.get("media_id"))
    local_media = info.get("local_media")

    if force_audio_refresh:
        log_event("info", phone, "audio_refresh_forced", formation=formation_name)

    def _resp_error_details(resp: Any) -> str:
        try:
            return json.dumps(resp.json(), ensure_ascii=False)
        except Exception:
            return getattr(resp, "text", "unknown error")

    def _try_send_audio(mid: Any) -> bool:
        if not mid:
            return False
        response = send_whatsapp_audio(phone, str(mid))
        if getattr(response, "ok", False):
            log_event("info", phone, "audio_send_ok", formation=formation_name, media_id=str(mid))
            _track_audio_message(phone, formation_name, str(mid), response)
            return True
        log_event(
            "error",
            phone,
            "audio_send_failed",
            formation=formation_name,
            media_id=str(mid),
            response=_resp_error_details(response),
        )
        return False

    audio_sent = False

    # Priorité au media_id (plus rapide).
    if media_id:
        try:
            audio_sent = _try_send_audio(media_id)
        except Exception as exc:
            print("[media] send audio raised error:", exc)

    # Si media_id absent OU invalide, fallback upload local + resend.
    if (not audio_sent) and local_media:
        try:
            uploaded_id = upload_media_to_whatsapp(local_media)
            if uploaded_id:
                MEDIA_ID_CACHE[formation_name] = uploaded_id
                save_media_cache(MEDIA_ID_CACHE)
                info["media_id"] = uploaded_id
                audio_sent = _try_send_audio(uploaded_id)
                if audio_sent:
                    log_event(
                        "info",
                        phone,
                        "audio_uploaded_and_cached",
                        formation=formation_name,
                        media_id=uploaded_id,
                        cache_path=MEDIA_CACHE_PATH,
                    )
        except Exception as exc:
            log_event("error", phone, "audio_local_upload_failed", formation=formation_name, error=str(exc))

    if not audio_sent:
        send_whatsapp_text(
            phone,
            "ℹ️ L'audio n'est pas encore disponible sur Meta. Un conseiller peut vous l'envoyer rapidement.",
        )
        log_event("error", phone, "audio_not_sent", formation=formation_name)


def ask_callback_preference(phone: str, formation_name: str) -> None:
    prompt = (
        f"📞 Souhaitez-vous être rappelé au sujet de *{formation_name}* ?\n"
        "Répondez Oui ou Non."
    )
    try:
        send_whatsapp_buttons(phone, prompt, ["Oui", "Non", "Formations"])
    except Exception:
        send_whatsapp_text(phone, prompt)


def handle_message(phone: str, text: str) -> None:
    message = (text or "").strip()
    normalized = normalize_text(message)
    session, session_expired = get_active_session(phone)
    log_event(
        "info",
        phone,
        "incoming_message",
        message=message,
        normalized=normalized,
        session_state=session.get("state") if session else None,
        agent_mode=(session or {}).get("agent_mode", "bot_mode"),
    )

    if session_expired:
        send_whatsapp_text(phone, SESSION_TIMEOUT_NOTICE)
        send_formations_menu(phone)
        session = None

    if COEXISTENCE_ENABLED and _is_human_mode(session):
        if _is_human_mode_timed_out(session):
            session = _release_to_bot_mode(phone, reason="human_timeout", actor="system", session=session)
            send_whatsapp_text(phone, HUMAN_RELEASE_NOTICE)
        elif _looks_like_bot_resume_request(normalized):
            _release_to_bot_mode(phone, reason="user_resume_request", actor=phone, session=session)
            send_whatsapp_text(phone, HUMAN_RELEASE_NOTICE)
            send_formations_menu(phone)
            return
        else:
            log_event("info", phone, "bot_silenced_human_mode", message=message)
            return

    if COEXISTENCE_ENABLED and _looks_like_human_request(normalized):
        _activate_human_mode(phone, reason="user_requested_human", actor=phone, session=session)
        send_whatsapp_text(phone, HUMAN_TAKEOVER_NOTICE)
        return

    # Raccourci global: retour menu à tout moment via "0" ou "menu".
    if is_menu_reset_command(message):
        clear_session(phone)
        send_formations_menu(phone)
        return

    touch_session(phone, session)

    # 1) Etat: confirmation d'une suggestion fuzzy
    if session and session.get("state") == "awaiting_suggestion_confirmation":
        suggested_formation = session.get("formation")
        if normalized in YES_WORDS and suggested_formation:
            send_formation_multimodal(phone, suggested_formation)
            set_session(phone, {
                "state": "awaiting_callback_consent",
                "formation": suggested_formation,
                "ts": time.time(),
            })
            ask_callback_preference(phone, suggested_formation)
            return

        if normalized in NO_WORDS:
            clear_session(phone)
            send_formations_menu(phone)
            return
        # Sinon, on continue le flux normal avec le texte libre.

    # 2) Etat: attente du choix oui/non pour rappel
    if session and session.get("state") == "awaiting_callback_consent":
        if normalized in YES_WORDS:
            set_session(phone, {
                "state": "awaiting_callback_details",
                "formation": session.get("formation"),
                "ts": time.time(),
            })
            send_whatsapp_text(
                phone,
                "📝 Parfait. Envoyez votre *nom* et votre *créneau de rappel* (ex: `Salim - demain 15h`).",
            )
            return

        if normalized in NO_WORDS:
            clear_session(phone)
            send_whatsapp_text(phone, "👌 Très bien. Tapez *formations* si vous voulez voir toutes les options.")
            return

        if looks_like_menu_request(message):
            clear_session(phone)
            send_formations_menu(phone)
            return

        send_whatsapp_text(phone, "Je n'ai pas compris 🤔. Répondez *Oui* ou *Non*.")
        return

    # 3) Etat: attente détail lead
    # - awaiting_callback_details: l'utilisateur envoie "Nom - créneau"
    # - awaiting_time: mode compatibilité, nom déjà connu en session
    if session and session.get("state") in {"awaiting_callback_details", "awaiting_time"}:
        if session.get("state") == "awaiting_time":
            lead_name = (session.get("lead_name") or "Prospect WhatsApp").strip()
            callback_date = message or "Non précisé"
        else:
            lead_name, callback_date = parse_name_and_callback(message)
        lead_formation = session.get("formation", "Non précisé")

        saved = save_lead_to_sheet(lead_name, phone, callback_date, lead_formation)
        if saved:
            send_whatsapp_text(phone, "✅ C'est noté, un conseiller reviendra vers vous.")
        else:
            send_whatsapp_text(phone, "⚠️ Je n'ai pas pu enregistrer votre demande pour le moment. Un conseiller prendra le relais.")
            log_event(
                "error",
                phone,
                "lead_save_failed",
                formation=lead_formation,
                callback_date=callback_date,
            )
        clear_session(phone)
        return

    # 4) Intentions globales: salutations + menu
    if is_greeting(message):
        send_whatsapp_text(
            phone,
            (
                f"👋 {DEFAULT_GREETING}\n\n"
                "Tapez *formations* pour afficher le menu, "
                "ou envoyez directement un mot-clé (ex: *nutrition*)."
            ),
        )
        return

    if looks_like_menu_request(message):
        send_formations_menu(phone)
        return

    # Choix guide via numero/emoji du menu (ex: "2" ou "2️⃣" ou "je choisis 2")
    selected_from_menu = parse_menu_selection(message, formations_db)
    if selected_from_menu:
        send_formation_multimodal(phone, selected_from_menu)
        set_session(phone, {
            "state": "awaiting_callback_consent",
            "formation": selected_from_menu,
            "ts": time.time(),
        })
        ask_callback_preference(phone, selected_from_menu)
        return

    # 5) Matching intelligent selon les seuils demandés
    mode, formation_name, score = match_formation(message, formations_db)

    if mode == "direct" and formation_name:
        send_formation_multimodal(phone, formation_name)
        set_session(phone, {
            "state": "awaiting_callback_consent",
            "formation": formation_name,
            "ts": time.time(),
        })
        ask_callback_preference(phone, formation_name)
        return

    if mode == "suggestion" and formation_name:
        suggestion_msg = (
            f"🤔 Vouliez-vous dire *{formation_name}* ?\n"
            "Tapotez *Oui* ou tapez le nom correctement."
        )
        send_whatsapp_text(phone, suggestion_msg)
        set_session(phone, {
            "state": "awaiting_suggestion_confirmation",
            "formation": formation_name,
            "score": score,
            "ts": time.time(),
        })
        return

    # mode menu (<55)
    fallback = "Désolé, je n'ai pas bien saisi."
    send_whatsapp_text(
        phone,
        f"{fallback}\n\n{format_formations_menu(formations_db)}",
    )


def handle_incoming_message(from_number: str, text: str) -> None:
    """Compatibilité avec l'ancien nom de fonction."""
    handle_message(from_number, text)


def _parse_handoff_allowed_networks(raw_sources: str) -> list:
    networks = []
    for item in (raw_sources or "").split(","):
        source = item.strip()
        if not source:
            continue
        try:
            if "/" in source:
                networks.append(ip_network(source, strict=False))
                continue

            networks.append(ip_network(f"{source}/32", strict=False))
        except Exception:
            try:
                networks.append(ip_network(f"{source}/128", strict=False))
            except Exception:
                continue
    return networks


HANDOFF_ALLOWED_NETWORKS = _parse_handoff_allowed_networks(HANDOFF_ALLOWED_SOURCES)


def _request_source_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop

    client = request.client
    if client and client.host:
        return str(client.host)
    return None


def _assert_handoff_source(request: Request) -> None:
    if not HANDOFF_ALLOWED_NETWORKS:
        return

    source_ip = _request_source_ip(request)
    if not source_ip:
        raise HTTPException(status_code=403, detail="Missing source IP")

    try:
        parsed_ip = ip_address(source_ip)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid source IP")

    for network in HANDOFF_ALLOWED_NETWORKS:
        if parsed_ip in network:
            return

    raise HTTPException(status_code=403, detail="Source IP not allowed")


def _assert_handoff_auth(request: Request) -> None:
    _assert_handoff_source(request)
    if not HANDOFF_API_TOKEN:
        return
    incoming_token = (request.headers.get("x-handoff-token") or "").strip()
    if incoming_token != HANDOFF_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid handoff token")


@app.post("/handoff/takeover")
async def handoff_takeover(request: Request) -> JSONResponse:
    _assert_handoff_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    phone = str(payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")

    reason = str(payload.get("reason") or "manual_takeover")
    actor = str(payload.get("actor") or "manual")
    notify_user = bool(payload.get("notify_user", False))

    session, _ = get_active_session(phone)
    _activate_human_mode(phone, reason=reason, actor=actor, session=session)
    if notify_user:
        send_whatsapp_text(phone, HUMAN_TAKEOVER_NOTICE)

    return JSONResponse({"status": "ok", "phone": phone, "agent_mode": "human_active"})


@app.post("/handoff/release")
async def handoff_release(request: Request) -> JSONResponse:
    _assert_handoff_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    phone = str(payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")

    reason = str(payload.get("reason") or "manual_release")
    actor = str(payload.get("actor") or "manual")
    reset_conversation = bool(payload.get("reset_conversation", True))
    notify_user = bool(payload.get("notify_user", False))

    session, _ = get_active_session(phone)
    _release_to_bot_mode(
        phone,
        reason=reason,
        actor=actor,
        session=session,
        reset_conversation=reset_conversation,
    )
    if notify_user:
        send_whatsapp_text(phone, HUMAN_RELEASE_NOTICE)

    return JSONResponse({"status": "ok", "phone": phone, "agent_mode": "bot_mode"})


@app.get("/webhook")
async def webhook_verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")

    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@app.post("/webhook")
async def webhook_receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages") or []
            contacts = value.get("contacts") or []
            statuses = value.get("statuses") or []

            for status in statuses:
                status_value = status.get("status")
                recipient_id = status.get("recipient_id")
                status_message_id = str(status.get("id") or "")
                errors = status.get("errors") or []
                known_bot_status = _is_known_bot_message_id(status_message_id)
                if status_value == "failed":
                    log_event(
                        "error",
                        recipient_id,
                        "whatsapp_delivery_failed",
                        message_id=status_message_id,
                        errors=errors,
                        raw_status=status,
                        source="bot" if known_bot_status else "external",
                    )
                    _recover_failed_audio_status(recipient_id, status)
                else:
                    log_event(
                        "info",
                        recipient_id,
                        "whatsapp_delivery_status",
                        status=status_value,
                        message_id=status_message_id,
                        source="bot" if known_bot_status else "external",
                    )

                if (
                    COEXISTENCE_ENABLED
                    and COEXISTENCE_AUTO_DETECT
                    and status_value == "sent"
                    and recipient_id
                    and status_message_id
                    and not known_bot_status
                ):
                    recipient = str(recipient_id)
                    existing_session, _ = get_active_session(recipient)
                    _activate_human_mode(
                        recipient,
                        reason="coexistence_external_sent_status",
                        actor="coexistence_status",
                        session=existing_session,
                    )

            for msg in messages:
                wa_id = None
                if contacts and isinstance(contacts, list):
                    wa_id = contacts[0].get("wa_id")
                if not wa_id:
                    wa_id = msg.get("from")
                if not wa_id:
                    continue

                text_body = ""
                msg_type = msg.get("type")
                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    if "button_reply" in interactive:
                        text_body = interactive["button_reply"].get("title", "")
                    elif "list_reply" in interactive:
                        text_body = interactive["list_reply"].get("title", "")
                else:
                    text_body = msg.get("text", {}).get("body", "")

                try:
                    handle_message(wa_id, text_body)
                except Exception as exc:
                    print("[webhook] message handling error:", exc)

    return JSONResponse({"status": "received"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
