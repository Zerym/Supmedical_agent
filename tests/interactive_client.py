"""Interactive webhook tester for local development.

Usage:
  # interactive mode
  .\.venv\Scripts\python.exe tests\interactive_client.py

  # one-off message
  .\.venv\Scripts\python.exe tests\interactive_client.py --phone 21260000001 --text "nutrition"

The script sends a payload that mimics WhatsApp Cloud webhook events to `/webhook`.
"""

import argparse
import requests
import time
import sys


def send_message(server: str, phone: str, text: str):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": phone,
                                    "id": "wamid.1",
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

    try:
        r = requests.post(f"{server}/webhook", json=payload, timeout=10)
        print(f"-> {r.status_code} {r.text}")
    except Exception as exc:
        print("Request failed:", exc)


def repl(server: str, phone: str):
    print("Interactive webhook tester. Type messages to send to the local /webhook endpoint.")
    print("Press Ctrl+C to quit.")
    while True:
        try:
            text = input('message> ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\nExiting.')
            return
        if not text:
            continue
        send_message(server, phone, text)


def main():
    p = argparse.ArgumentParser(description="Interactive webhook tester")
    p.add_argument("--server", default="http://127.0.0.1:8000", help="Base URL of local server")
    p.add_argument("--phone", default="21260000001", help="WhatsApp phone id to emulate")
    p.add_argument("--text", help="If provided, send single message and exit")
    args = p.parse_args()

    if args.text:
        send_message(args.server, args.phone, args.text)
        return
    repl(args.server, args.phone)


if __name__ == "__main__":
    main()
