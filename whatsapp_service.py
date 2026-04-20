"""Services d'envoi WhatsApp Cloud API (texte, audio, boutons) + upload media."""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from typing import List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
TEST_MODE = os.getenv("TEST_MODE", "0").lower() in ("1", "true", "yes")


@dataclass
class MockResponse:
    status_code: int = 200
    ok: bool = True
    payload: Optional[dict] = None

    def json(self):
        return self.payload or {"mock": True}


def _messages_url() -> str:
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID not configured in .env")
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def _media_url() -> str:
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID not configured in .env")
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/media"


def _auth_headers(content_type_json: bool = True) -> dict:
    if not WHATSAPP_TOKEN:
        raise RuntimeError("WHATSAPP_TOKEN not configured in .env")
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    if content_type_json:
        headers["Content-Type"] = "application/json"
    return headers


def _guess_audio_mime_type(local_path: str, explicit_mime_type: Optional[str] = None) -> str:
    if explicit_mime_type:
        return explicit_mime_type

    extension = os.path.splitext(local_path)[1].lower()
    if extension == ".ogg":
        return "audio/ogg"
    if extension == ".mp3":
        return "audio/mpeg"

    guessed, _ = mimetypes.guess_type(local_path)
    return guessed or "application/octet-stream"


def _log_send_failure(channel: str, response: requests.Response) -> None:
    try:
        print(f"[whatsapp_service] send {channel} failed:", response.json())
    except Exception:
        print(f"[whatsapp_service] send {channel} failed:", response.status_code, response.text)


def send_whatsapp_text(to_number: str, message: str):
    if TEST_MODE:
        print(f"[TEST_MODE] send_whatsapp_text -> to={to_number} body={message}")
        return MockResponse(payload={"type": "text", "to": to_number, "body": message})

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    response = requests.post(_messages_url(), headers=_auth_headers(True), json=payload, timeout=20)
    if not response.ok:
        _log_send_failure("text", response)
    return response


def send_whatsapp_audio(to_number: str, media_id: str):
    if TEST_MODE:
        print(f"[TEST_MODE] send_whatsapp_audio -> to={to_number} media_id={media_id}")
        return MockResponse(payload={"type": "audio", "to": to_number, "media_id": media_id})

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "audio",
        "audio": {"id": str(media_id)},
    }
    response = requests.post(_messages_url(), headers=_auth_headers(True), json=payload, timeout=20)
    if not response.ok:
        _log_send_failure("audio", response)
    return response


def send_whatsapp_buttons(to_number: str, body_text: str, button_titles: List[str]):
    """Envoi d'un message interactif a boutons.

    WhatsApp Cloud accepte max 3 boutons reply, titre max 20 chars.
    """
    titles = [title.strip()[:20] for title in button_titles if title and title.strip()][:3]
    if not titles:
        return send_whatsapp_text(to_number, body_text)

    if TEST_MODE:
        print(f"[TEST_MODE] send_whatsapp_buttons -> to={to_number} body={body_text} buttons={titles}")
        return MockResponse(payload={"type": "interactive", "to": to_number, "buttons": titles})

    buttons = []
    for idx, title in enumerate(titles, start=1):
        buttons.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{idx}_{title.lower().replace(' ', '_')}",
                "title": title,
            },
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {"buttons": buttons},
        },
    }
    response = requests.post(_messages_url(), headers=_auth_headers(True), json=payload, timeout=20)
    if not response.ok:
        _log_send_failure("buttons", response)
    return response


def upload_media_to_whatsapp(local_path: str, mime_type: Optional[str] = None) -> Optional[str]:
    """Upload media local vers Meta et retourne l'ID media.

    NOTE performance:
    - Si on envoie un `local_media` a chaque message, il faut uploader a chaque fois.
    - Le plus rapide est de pre-uploader une fois et stocker le `media_id` dans `database.py`.
    """
    if TEST_MODE:
        print(f"[TEST_MODE] upload_media_to_whatsapp -> local_path={local_path}")
        return f"mock-{os.path.basename(local_path)}"

    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    mime_type = _guess_audio_mime_type(local_path, mime_type)
    headers = _auth_headers(content_type_json=False)
    with open(local_path, "rb") as file_obj:
        files = {"file": (os.path.basename(local_path), file_obj, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}
        response = requests.post(_media_url(), headers=headers, files=files, data=data, timeout=60)

    try:
        payload = response.json()
    except Exception:
        print("[whatsapp_service] Upload response non JSON:", response.status_code, response.text)
        return None

    if response.ok:
        print(f"[whatsapp_service] upload ok path={local_path} mime={mime_type} media_id={payload.get('id')}")
        return payload.get("id")

    print("[whatsapp_service] Upload failed:", payload)
    return None
