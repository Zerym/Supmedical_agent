import datetime
import os
import time

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

# Configuration
SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CREDS_FILE = os.path.join(BASE_DIR, "credentials.json")
CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", DEFAULT_CREDS_FILE)
# Option: .env peut définir GOOGLE_SHEET_ID, sinon fallback sur la valeur ci-dessous
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1WTZKW0Wwzjej7dWuCv29SdPXLL-yL3LMYSSslflQRbY")
GOOGLE_SHEETS_RETRIES = max(0, int(os.getenv("GOOGLE_SHEETS_RETRIES", "1")))
GOOGLE_SHEETS_RETRY_DELAY_SECONDS = float(os.getenv("GOOGLE_SHEETS_RETRY_DELAY_SECONDS", "1.0"))


def _validate_config() -> None:
    if not os.path.exists(CREDS_FILE):
        raise FileNotFoundError(f"credentials.json introuvable: {CREDS_FILE}")
    if not SHEET_ID or not SHEET_ID.strip():
        raise RuntimeError("GOOGLE_SHEET_ID manquant")


def save_lead(name: str, phone: str, callback_time: str, formation: str) -> bool:
    """Enregistre une nouvelle ligne dans le Google Sheet"""
    max_attempts = GOOGLE_SHEETS_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        try:
            _validate_config()
            creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPE)
            client = gspread.authorize(creds)

            # Ouvre le document et la première feuille
            sheet = client.open_by_key(SHEET_ID).sheet1

            # Prépare la ligne (ajoute une date si tu veux)
            date_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

            row = [date_now, name, phone, callback_time, formation]

            # Ajoute la ligne à la fin
            sheet.append_row(row)
            print(f"✅ Lead enregistré pour {name}")
            return True
        except Exception as e:
            print(f"❌ Erreur Google Sheets (tentative {attempt}/{max_attempts}) : {e}")
            if attempt >= max_attempts:
                return False
            time.sleep(GOOGLE_SHEETS_RETRY_DELAY_SECONDS)

    return False