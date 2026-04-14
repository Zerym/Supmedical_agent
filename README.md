# Supmedical WhatsApp Bot

Bot minimal pour répondre aux demandes WhatsApp et enregistrer des leads.

Installation rapide

1. Copier `.env.example` vers `.env` et renseigner les variables.
2. Installer les dépendances: `pip install -r requirements.txt`
3. Lancer en développement: `uvicorn main:app --reload --port 8000`

Configuration du Webhook (Meta / WhatsApp Cloud)

- Dans votre application Meta Developers, ajoutez un Webhook et indiquez l'URL publique exposant `/webhook`.
- `VERIFY_TOKEN` doit correspondre à la valeur renseignée dans `.env`.
- Sousscriptions: `messages`, `messaging_postbacks` etc. selon besoin.

Google Sheets (lead storage)

- Créez un service account dans Google Cloud et téléchargez le JSON.
- Donnez l'accès en édition à la feuille à l'adresse e-mail du service account.
- Renseignez `GOOGLE_SERVICE_ACCOUNT_JSON` et `GOOGLE_SHEET_ID` dans `.env`.
