"""main.py
Serveur FastAPI pour gérer les messages WhatsApp (Meta Cloud API)
Fonctionnalités:
- Vérification du webhook (GET /webhook)
- Réception des messages (POST /webhook)
- Envoi de textes et d'audios via l'API WhatsApp Cloud
- Enregistrement des leads dans Google Sheets via gspread

Toutes les sections sont commentées en français.
"""

import os
import json
import re
import time
from datetime import datetime
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import requests
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

# Import local database des formations
from database import formations_db, DEFAULT_GREETING, _strip_accents

try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except Exception:
    RAPIDFUZZ_AVAILABLE = False

# Charger les variables d'environnement depuis .env
load_dotenv()

# Configuration (à définir dans .env)
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")  # ex: 1234567890
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")  # ID de la feuille Google
TEST_MODE = os.getenv("TEST_MODE", "0").lower() in ("1", "true", "yes")

app = FastAPI(title="Supmedical WhatsApp Bot")

# Session mémoire (en RAM) pour suivre l'état de la conversation par numéro
# Structure: { phone_number: {"state": str, "formation": str, "ts": float} }
sessions: Dict[str, Dict[str, Any]] = {}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def is_greeting(text: str) -> bool:
    """Détecte une salutation simple en français/anglais.
    Utilisé pour renvoyer le message d'accueil professionnel.
    """
    if not text:
        return False
    text_lower = text.lower()
    greetings = ["bonjour", "bonsoir", "salut", "coucou", "hello", "hi", "bjr"]
    for g in greetings:
        if re.search(r"\b" + re.escape(g) + r"\b", text_lower):
            return True
    return False


def send_whatsapp_text(to_number: str, message: str) -> requests.Response:
    """Envoie un message texte via l'API WhatsApp Cloud.

    Remarque: `WHATSAPP_TOKEN` et `WHATSAPP_PHONE_NUMBER_ID` doivent être définis.
    """
    if TEST_MODE:
        print(f"[TEST_MODE] send_whatsapp_text -> to={to_number} message={message}")
        class _Mock:
            ok = True
            status_code = 200
            def json(self):
                return {"mock": True, "body": message}
        return _Mock()

    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured in .env")

    url = f"https://graph.facebook.com/v16.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    return resp


def send_whatsapp_audio(to_number: str, media_id: str) -> requests.Response:
    """Envoie un message audio (référence à un media ID déjà uploadé sur WhatsApp).

    ATTENTION: le `media_id` attendu par l'API WhatsApp est l'ID Meta/WhatsApp (pas l'ID WordPress).
    Si vos fichiers audio sont stockés ailleurs, il faut d'abord les uploader via l'endpoint /media
    pour obtenir un `media_id` utilisable.
    """
    if TEST_MODE:
        print(f"[TEST_MODE] send_whatsapp_audio -> to={to_number} media_id={media_id}")
        class _Mock:
            ok = True
            status_code = 200
            def json(self):
                return {"mock": True, "media_id": media_id}
        return _Mock()

    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured in .env")

    url = f"https://graph.facebook.com/v16.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "audio",
        "audio": {"id": str(media_id)},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    return resp


def upload_media_to_whatsapp(local_path: str, mime_type: str = "audio/ogg") -> str | None:
    """Upload a local file to WhatsApp Cloud and return the returned media ID.

    Requires `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` set in env.
    """
    if TEST_MODE:
        print(f"[TEST_MODE] upload_media_to_whatsapp -> local_path={local_path}")
        return f"mock-{os.path.basename(local_path)}"

    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured in .env")
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    url = f"https://graph.facebook.com/v16.0/{WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with open(local_path, "rb") as f:
        files = {"file": (os.path.basename(local_path), f, mime_type)}
        resp = requests.post(url, headers=headers, files=files, timeout=60)
    try:
        data = resp.json()
    except Exception:
        print("Upload response not JSON:", resp.status_code, resp.text)
        return None
    if resp.ok:
        return data.get("id")
    print("Upload failed:", data)
    return None


def get_gspread_client() -> gspread.Client:
    """Initialise et retourne un client gspread à partir d'un JSON de service account.

    Attent: la variable `GOOGLE_SERVICE_ACCOUNT_JSON` peut être soit un chemin vers
    un fichier JSON, soit le contenu JSON lui-même (encodé en string) — pratique pour
    stocker dans des CI/CD ou un secret manager.
    """
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set in .env")

    # charger la JSON soit depuis la string soit depuis un fichier
    try:
        if GOOGLE_SERVICE_ACCOUNT_JSON.strip().startswith("{"):
            sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        else:
            with open(GOOGLE_SERVICE_ACCOUNT_JSON, "r", encoding="utf-8") as f:
                sa_info = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Service account JSON invalide: {exc}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client


def save_lead(phone: str, formation: str, availability: str) -> bool:
    """Enregistre un prospect dans la Google Sheet (ID: GOOGLE_SHEET_ID).

    Colonne enregistrée: Timestamp UTC, Numéro, Formation, Disponibilités (texte libre)
    """
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID not configured in .env")
    try:
        client = get_gspread_client()
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.sheet1
        ts = datetime.utcnow().isoformat()
        row = [ts, phone, formation, availability]
        worksheet.append_row(row)
        return True
    except Exception as exc:
        print("Erreur lors de l'enregistrement du lead:", exc)
        return False


def handle_incoming_message(from_number: str, text: str) -> None:
    """Logique principale de conversation en français.

    - Accueil si salutation
    - Filtrage par mots-clés de `formations_db`
    - Envoi audio (media_id) puis texte avec le lien d'inscription
    - Après envoi audio, on attend la réponse de l'utilisateur puis demande de
      disponibilité pour rappel et on enregistre le lead via `save_lead`.
    """
    text = (text or "").strip()
    print(f"Incoming from {from_number}: {text}")

    # 1) Accueil si salutation
    if is_greeting(text):
        greeting = (
            "Bonjour/Bonsoir, l’équipe Supmedical Academy est ravie de vous accueillir. "
            "Comment puis-je vous aider ? Souhaitez-vous des informations sur une formation spécifique ?"
        )
        send_whatsapp_text(from_number, greeting)
        return

    # 2) Si l'utilisateur vient de recevoir l'audio précédemment
    session = sessions.get(from_number)
    if session and session.get("state") == "awaiting_followup":
        # l'utilisateur répond après l'audio => demander disponibilité
        question = (
            "Souhaitez-vous qu'un conseiller vous rappelle ? Si oui, merci de nous indiquer vos jours et heures de disponibilité."
        )
        send_whatsapp_text(from_number, question)
        sessions[from_number]["state"] = "awaiting_availability"
        return

    # 3) Si l'utilisateur nous donne ses disponibilités (etat précédent)
    if session and session.get("state") == "awaiting_availability":
        formation = session.get("formation", "")
        availability_text = text
        saved = save_lead(from_number, formation, availability_text)
        if saved:
            send_whatsapp_text(from_number, "Merci — vos disponibilités ont été enregistrées. Un conseiller vous contactera prochainement.")
        else:
            send_whatsapp_text(from_number, "Désolé, une erreur est survenue lors de l'enregistrement. Nous allons réessayer de notre côté.")
        # terminer la session
        sessions.pop(from_number, None)
        return

    # 4) Filtrage par mots-clés (database.py) with fuzzy fallback
    def find_best_formation(query: str, threshold: int = 75):
        q_raw = (query or "").strip()
        q_lower = q_raw.lower()
        q_norm = _strip_accents(q_lower)

        # exact substring (fast)
        for formation_name, info in formations_db.items():
            for kw in info.get("keywords", []):
                if kw and (kw in q_lower or kw in q_norm):
                    return formation_name, 100

        # fuzzy match using RapidFuzz if available
        if not RAPIDFUZZ_AVAILABLE:
            return None, 0

        choices = []
        kw_to_formation = {}
        for formation_name, info in formations_db.items():
            for kw in info.get("keywords", []):
                if not kw:
                    continue
                k = kw
                choices.append(k)
                kw_to_formation[k] = formation_name

        if not choices:
            return None, 0

        # use token_set_ratio for robustness
        match = process.extractOne(q_norm, choices, scorer=fuzz.token_set_ratio)
        if match:
            best_kw, score, _ = match
            formation = kw_to_formation.get(best_kw)
            if score >= threshold:
                return formation, int(score)
        return None, 0

    formation_name, score = find_best_formation(text, threshold=70)
    if formation_name:
        info = formations_db.get(formation_name, {})
        media_id = info.get("media_id")
        # if no media_id but local_media exists, try uploading
        if not media_id and info.get("local_media"):
            try:
                local_path = info.get("local_media")
                if os.path.exists(local_path):
                    uploaded_id = upload_media_to_whatsapp(local_path)
                    if uploaded_id:
                        info["media_id"] = uploaded_id
                        media_id = uploaded_id
            except Exception as exc:
                print("Upload media error:", exc)

        try:
            if media_id:
                send_whatsapp_audio(from_number, media_id)
        except Exception as exc:
            print("Erreur en envoyant l'audio:", exc)

        # Envoyer texte avec lien d'inscription
        link = info.get("registration_link", "")
        msg = f"Voici les détails en audio. Vous pouvez aussi consulter le programme et vous inscrire ici : {link}"
        send_whatsapp_text(from_number, msg)

        # Marquer la session en attente de réponse utilisateur
        sessions[from_number] = {"state": "awaiting_followup", "formation": formation_name, "ts": time.time()}
        return

    # 5) Réponse par défaut si aucun mot-clé trouvé
    fallback = (
        "Je n'ai pas trouvé de formation correspondant à votre demande. "
        "Souhaitez-vous consulter la liste des formations disponibles ?"
    )
    send_whatsapp_text(from_number, fallback)


# ------------------------------------------------------------------
# Endpoints Webhook
# ------------------------------------------------------------------

@app.get("/webhook")
async def webhook_verify(request: Request):
    """Vérification du webhook (procédure demandée par Meta lors de l'enregistrement).

    Meta envoie: hub.mode, hub.challenge, hub.verify_token
    """
    params = request.query_params
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    token = params.get("hub.verify_token")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        # Retourner le challenge en clair
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@app.post("/webhook")
async def webhook_receive(request: Request):
    """Point d'entrée pour les notifications WhatsApp envoyées par Meta.

    Le payload contient généralement: entry -> changes -> value -> messages
    Nous extrayons le numéro (wa_id) et le texte, puis appelons handle_incoming_message.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # parcourir les entrées (robuste face aux variantes du payload)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages") or []
            contacts = value.get("contacts") or []

            for msg in messages:
                # numéro WhatsApp
                wa_id = None
                if contacts and isinstance(contacts, list):
                    wa_id = contacts[0].get("wa_id")
                # fallback
                if not wa_id:
                    wa_id = msg.get("from")

                if not wa_id:
                    continue

                # extraire le texte si disponible
                text_body = ""
                if msg.get("type") == "text":
                    text_body = msg.get("text", {}).get("body", "")
                elif msg.get("type") == "interactive":
                    interactive = msg.get("interactive", {})
                    if "button_reply" in interactive:
                        text_body = interactive["button_reply"].get("title", "")
                    elif "list_reply" in interactive:
                        text_body = interactive["list_reply"].get("title", "")
                else:
                    text_body = msg.get("text", {}).get("body", "")

                # traiter le message (fonction sync: ne bloque pas beaucoup)
                try:
                    handle_incoming_message(wa_id, text_body)
                except Exception as exc:
                    print("Erreur traitement message:", exc)

    return JSONResponse({"status": "received"})


if __name__ == "__main__":
    # Pour le développement local: uvicorn main:app --reload
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
