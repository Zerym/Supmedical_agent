"""End-to-end local test script for Supmedical WhatsApp Bot.

Run after starting the server with TEST_MODE=1 and VERIFY_TOKEN=TEST_VERIFY:

PowerShell example to start server (in project root):
$env:TEST_MODE='1'; $env:VERIFY_TOKEN='TEST_VERIFY'; \
& .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

Then run this script in another shell:
.\.venv\Scripts\python.exe tests\e2e_test.py
"""

import os
import time
import requests

SERVER = os.environ.get("TEST_SERVER", "http://127.0.0.1:8000")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "TEST_VERIFY")


def test_verify():
    url = f"{SERVER}/webhook?hub.mode=subscribe&hub.challenge=CHALLENGE123&hub.verify_token={VERIFY_TOKEN}"
    r = requests.get(url)
    print("VERIFY ->", r.status_code, r.text[:200])


def post_message(phone: str, text: str):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": phone,
                                    "id": "wamid.123",
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                            "contacts": [{"wa_id": phone}],
                        }
                    }
                ]
            }
        ]
    }
    r = requests.post(f"{SERVER}/webhook", json=payload)
    print(f"POST '{text[:30]}' ->", r.status_code, r.text[:400])


def run_all():
    print("Waiting 1s for server readiness...")
    time.sleep(1)
    test_verify()
    samples = [
        "Bonjour",
        "nutrition",
        "compléments alimentaires",
        "complements alimentairess",  # typo/plural
        "dispositifs médicaux",
    ]
    for i, s in enumerate(samples, start=1):
        post_message(f"2126000000{i}", s)
        time.sleep(0.5)


if __name__ == "__main__":
    run_all()
