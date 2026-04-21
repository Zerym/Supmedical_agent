# Supmedical WhatsApp Bot

Bot FastAPI pour repondre aux messages WhatsApp et enregistrer les leads dans Google Sheets.

## 1) Prerequis obligatoires

- Python 3.11+ (votre venv est deja en 3.14)
- Application Meta Developers avec produit WhatsApp Cloud
- Un `WHATSAPP_PHONE_NUMBER_ID` valide
- Un token Meta valide avec permissions WhatsApp
- Un Google Sheet cible
- Un service account Google avec acces en edition a ce sheet

## 2) Installation locale

```powershell
# Depuis la racine du projet
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3) Variables `.env` obligatoires

Exemple minimal:

```env
WHATSAPP_TOKEN=EAA...
WHATSAPP_PHONE_NUMBER_ID=123456789012345
VERIFY_TOKEN=your_verify_token

GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_CREDS_FILE=C:/Users/you/Documents/Supmedical_Agent/credentials.json

# Session timeout (30 min)
SESSION_TIMEOUT_SECONDS=1800

# Coexistence bot + humain
WHATSAPP_COEXISTENCE_ENABLED=1
WHATSAPP_COEXISTENCE_AUTO_DETECT=1
HUMAN_OVERRIDE_TIMEOUT_SECONDS=1800
BOT_OUTBOUND_MESSAGE_TTL_SECONDS=7200

# Optionnel: protege les endpoints /handoff/*
HANDOFF_API_TOKEN=change_me
HANDOFF_ALLOWED_SOURCES=203.0.113.10,198.51.100.0/24

# Google Sheets retry policy (1 retry => 2 tentatives)
GOOGLE_SHEETS_RETRIES=1
GOOGLE_SHEETS_RETRY_DELAY_SECONDS=1.0
```

Important:
- `GOOGLE_CREDS_FILE` doit pointer vers un vrai `credentials.json`.
- `TEST_MODE` doit etre vide (ou `0`) pour les vrais uploads Meta.
- `HANDOFF_API_TOKEN` doit etre long et aleatoire en production.
- `HANDOFF_ALLOWED_SOURCES` doit contenir uniquement les IP/CIDR de vos outils (CRM, reverse proxy, bastion). Si vide, aucune restriction IP n'est appliquee.

## 4) Webhook Meta: etapes obligatoires

1. Lancer l'API localement:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8002
```

2. Exposer local en HTTPS public (exemple ngrok):

```powershell
ngrok http 8002
```

3. Dans Meta Developers > WhatsApp > Configuration > Webhook:
- Callback URL: `https://<votre-url-publique>/webhook`
- Verify token: exactement la valeur `VERIFY_TOKEN` de `.env`
- Cliquer Verify and Save
- Souscrire le champ `messages`

4. Envoyer un message WhatsApp au numero connecte pour verifier la reception.

## 5) Google Sheets lead storage: configuration obligatoire

1. Google Cloud Console:
- Activer `Google Sheets API`
- Activer `Google Drive API`
- Creer un Service Account
- Generer une cle JSON

2. Google Sheet:
- Ouvrir le sheet
- Cliquer Share
- Ajouter l'email du service account (role Editor)

3. `.env`:
- `GOOGLE_CREDS_FILE` -> chemin reel du JSON (ou laisser la valeur par defaut `credentials.json` a la racine)
- `GOOGLE_SHEET_ID` -> ID du sheet (dans l'URL Google Sheets)

4. Test rapide:

```powershell
.\.venv\Scripts\python.exe -c "import google_sheets_service as g; from google.oauth2.service_account import Credentials; import gspread; c=Credentials.from_service_account_file(g.CREDS_FILE, scopes=g.SCOPE); sh=gspread.authorize(c).open_by_key(g.SHEET_ID); print(sh.title)"
```

## 6) Upload OGG vers Meta + update `database.py`

Les OGG locaux sont attendus dans `media/`:
- `media/parapharmacie.ogg`
- `media/dispositifs_medicaux.ogg`
- `media/nutrition.ogg`
- `media/complements_alimentaires.ogg`

Script automatique:

```powershell
.\.venv\Scripts\python.exe scripts\upload_and_update_db.py
```

Effet:
- Upload de chaque OGG sur WhatsApp Cloud
- Recuperation des `media_id`
- Ecriture des `media_id` dans `database.py`
- Sauvegarde de securite: `database.py.bak`

## 7) Diagnostic erreurs frequentes

### Erreur Meta `code 100` + `error_subcode 33`
Cause probable:
- `WHATSAPP_PHONE_NUMBER_ID` invalide
- Token sur mauvais app/business
- Permissions manquantes (`whatsapp_business_messaging` / `whatsapp_business_management`)

Verification rapide du `PHONE_NUMBER_ID`:

```powershell
$token = $env:WHATSAPP_TOKEN
$phoneId = $env:WHATSAPP_PHONE_NUMBER_ID
Invoke-RestMethod -Method Get -Uri "https://graph.facebook.com/v23.0/$phoneId?fields=id,display_phone_number,verified_name" -Headers @{ Authorization = "Bearer $token" }
```

Si cette requete echoue, corriger token/phone number id avant l'upload OGG.

### Erreur Google `No such file or directory: /path/to/service_account.json`
Cause:
- `.env` pointe sur le chemin exemple, pas sur un vrai fichier JSON.

## 8) Raccourcis conversation et sessions

- Tapez `0` ou `menu` a tout moment pour revenir au menu.
- Les sessions utilisateur sont persistees en base SQLite (`session_store.db`).
- Les sessions expirent automatiquement apres `SESSION_TIMEOUT_SECONDS` (30 min par defaut).

## 9) Commandes de test local

Mode simulation (sans appels reels Meta):

```powershell
$env:TEST_MODE='1'
$env:VERIFY_TOKEN='TEST_VERIFY'
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8002
```

Dans un autre terminal:

```powershell
.\.venv\Scripts\python.exe tests\interactive_client.py --server http://127.0.0.1:8002
```

Tests coexistence (bot/humain):

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_coexistence -v
```

## 10) Coexistence bot + humain (WhatsApp Business App + Cloud API)

Le bot supporte maintenant un mode `human_active` persiste en `session_store.db`.

Comportement:
- Si une conversation est en `human_active`, le bot reste silencieux pour eviter les conflits.
- Si aucun signe d'activite humaine n'est detecte pendant `HUMAN_OVERRIDE_TIMEOUT_SECONDS`, le bot repasse en `bot_mode`.
- Si l'utilisateur ecrit un message de type "conseiller", "agent" ou "humain", le bot bascule la conversation en mode humain.
- Si l'utilisateur envoie "bot" / "resume bot", le bot reprend la main.

Auto-detection coexistence:
- Les webhooks `statuses` avec `status=sent` non reconnus comme messages bot sont interpretes comme activite humaine (coexistence active).

Endpoints manuels (CRM / shared inbox):

1. Activer mode humain

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8002/handoff/takeover" `
	-Headers @{"x-handoff-token"="change_me"} `
	-ContentType "application/json" `
	-Body '{"phone":"21260000001","reason":"hubspot_takeover","actor":"support_agent","notify_user":true}'
```

2. Rendre la main au bot

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8002/handoff/release" `
	-Headers @{"x-handoff-token"="change_me"} `
	-ContentType "application/json" `
	-Body '{"phone":"21260000001","reason":"ticket_closed","actor":"support_agent","reset_conversation":true,"notify_user":false}'
```

