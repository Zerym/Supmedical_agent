import time
import unittest

from fastapi.testclient import TestClient

import main


class _DummyResponse:
    def __init__(self, message_id: str = ""):
        self.ok = True
        self.status_code = 200
        self._message_id = message_id

    def json(self):
        if self._message_id:
            return {"messages": [{"id": self._message_id}]}
        return {"mock": True}


class CoexistenceTests(unittest.TestCase):
    def setUp(self):
        self.sent_messages = []
        self.client = TestClient(main.app)

        self._orig_send_text = main.send_whatsapp_text
        self._orig_send_buttons = main.send_whatsapp_buttons
        self._orig_send_audio = main.send_whatsapp_audio
        self._orig_save_session = main.save_session_to_db
        self._orig_load_session = main.load_session_from_db
        self._orig_delete_session = main.delete_session_from_db
        self._orig_timeout = main.HUMAN_OVERRIDE_TIMEOUT_SECONDS
        self._orig_coexistence = main.COEXISTENCE_ENABLED
        self._orig_auto_detect = main.COEXISTENCE_AUTO_DETECT
        self._orig_handoff_allowed_networks = list(main.HANDOFF_ALLOWED_NETWORKS)
        self._orig_handoff_token = main.HANDOFF_API_TOKEN

        main.COEXISTENCE_ENABLED = True
        main.COEXISTENCE_AUTO_DETECT = True
        main.HUMAN_OVERRIDE_TIMEOUT_SECONDS = 1800
        main.HANDOFF_ALLOWED_NETWORKS = []
        main.HANDOFF_API_TOKEN = ""

        main.sessions.clear()
        main.BOT_OUTBOUND_MESSAGE_CONTEXT.clear()

        def fake_send_text(phone: str, message: str):
            self.sent_messages.append(("text", phone, message))
            return _DummyResponse()

        def fake_send_buttons(phone: str, body_text: str, button_titles: list[str]):
            self.sent_messages.append(("interactive", phone, body_text, button_titles))
            return _DummyResponse()

        def fake_send_audio(phone: str, media_id: str):
            self.sent_messages.append(("audio", phone, media_id))
            return _DummyResponse()

        main.send_whatsapp_text = fake_send_text
        main.send_whatsapp_buttons = fake_send_buttons
        main.send_whatsapp_audio = fake_send_audio

        # Keep tests isolated from the persistent SQLite store.
        main.save_session_to_db = lambda _phone, _payload: None
        main.load_session_from_db = lambda _phone, _ttl: None
        main.delete_session_from_db = lambda _phone: None

    def tearDown(self):
        self.client.close()
        main.send_whatsapp_text = self._orig_send_text
        main.send_whatsapp_buttons = self._orig_send_buttons
        main.send_whatsapp_audio = self._orig_send_audio
        main.save_session_to_db = self._orig_save_session
        main.load_session_from_db = self._orig_load_session
        main.delete_session_from_db = self._orig_delete_session
        main.HUMAN_OVERRIDE_TIMEOUT_SECONDS = self._orig_timeout
        main.COEXISTENCE_ENABLED = self._orig_coexistence
        main.COEXISTENCE_AUTO_DETECT = self._orig_auto_detect
        main.HANDOFF_ALLOWED_NETWORKS = self._orig_handoff_allowed_networks
        main.HANDOFF_API_TOKEN = self._orig_handoff_token
        main.sessions.clear()
        main.BOT_OUTBOUND_MESSAGE_CONTEXT.clear()

    def test_user_request_enables_human_mode_and_silences_bot(self):
        phone = "21260000011"

        main.handle_message(phone, "Je veux parler a un conseiller humain")
        first_batch_count = len(self.sent_messages)

        session = main.sessions.get(phone)
        self.assertIsNotNone(session)
        self.assertEqual((session or {}).get("agent_mode"), "human_active")
        self.assertGreaterEqual(first_batch_count, 1)

        main.handle_message(phone, "nutrition")
        self.assertEqual(len(self.sent_messages), first_batch_count)

    def test_human_mode_timeout_returns_to_bot_mode(self):
        phone = "21260000012"
        main.HUMAN_OVERRIDE_TIMEOUT_SECONDS = 1

        main.set_session(
            phone,
            {
                "agent_mode": "human_active",
                "human_last_activity_ts": time.time() - 120,
                "ts": time.time() - 120,
            },
        )

        main.handle_message(phone, "formations")

        session = main.sessions.get(phone)
        self.assertIsNotNone(session)
        self.assertEqual((session or {}).get("agent_mode"), "bot_mode")
        self.assertGreaterEqual(len(self.sent_messages), 2)

    def test_unknown_sent_status_activates_human_mode(self):
        phone = "21260000013"
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.human.1",
                                        "status": "sent",
                                        "recipient_id": phone,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        response = self.client.post("/webhook", json=payload)
        self.assertEqual(response.status_code, 200)

        session = main.sessions.get(phone)
        self.assertIsNotNone(session)
        self.assertEqual((session or {}).get("agent_mode"), "human_active")

    def test_known_bot_status_does_not_activate_human_mode(self):
        phone = "21260000014"
        main._remember_bot_outbound_message(phone, "wamid.bot.1", "text")

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.bot.1",
                                        "status": "sent",
                                        "recipient_id": phone,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        response = self.client.post("/webhook", json=payload)
        self.assertEqual(response.status_code, 200)

        session = main.sessions.get(phone)
        self.assertFalse(main._is_human_mode(session))

    def test_manual_takeover_and_release_endpoints(self):
        phone = "21260000015"

        takeover = self.client.post("/handoff/takeover", json={"phone": phone})
        self.assertEqual(takeover.status_code, 200)
        self.assertEqual((main.sessions.get(phone) or {}).get("agent_mode"), "human_active")

        release = self.client.post("/handoff/release", json={"phone": phone})
        self.assertEqual(release.status_code, 200)
        self.assertEqual((main.sessions.get(phone) or {}).get("agent_mode"), "bot_mode")


if __name__ == "__main__":
    unittest.main()
