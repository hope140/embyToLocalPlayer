import configparser
import tempfile
import unittest
from pathlib import Path

from utils.watch_together_coordinator import WatchTogetherCoordinator, WatchTogetherHttpService
from utils.watch_together_store import WatchTogetherStore


class FakeApi:
    server_url = "https://media.test"
    server_id = "sid"

    def verify_admin_user(self, user_id, token):
        return user_id == "admin" and token == "browser-token"

    def get_users_for_ui(self):
        return [{"id": "admin", "name": "Admin"}, {"id": "guest", "name": "Guest"}]

    def get_system_info(self):
        return {"Id": "sid"}

    def get_sessions(self):
        return []


class WatchTogetherHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = configparser.ConfigParser()
        config["watch_together"] = {
            "enable": "true", "admin_enable": "true",
            "server_url": "https://media.test/emby", "admin_api_key": "admin-secret",
        }
        self.config = config
        self.coordinator = WatchTogetherCoordinator(
            store=WatchTogetherStore(Path(self.temp.name) / "rooms.json"), api=FakeApi(),
            config=config,
        )
        self.service = WatchTogetherHttpService(self.coordinator, config=config)
        status, result = self.service.handle(
            "/watch-together/auth",
            {"server_url": "https://media.test", "user_id": "admin", "api_key": "browser-token"},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 200)
        self.token = result["token"]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, path, body=None, token=None, address=("127.0.0.1", 1)):
        headers = {"X-ETLP-Watch-Token": token or self.token}
        return self.service.handle(path, body, headers=headers, client_address=address)

    def test_loopback_token_cors_and_room_lifecycle(self):
        self.assertEqual(self.call("/watch-together/rooms/list")[0], 200)
        status, result = self.call(
            "/watch-together/rooms/create",
            {"name": "x", "participant_user_ids": ["admin", "guest"], "primary_user_id": "admin"},
        )
        self.assertEqual(status, 200)
        room_id = result["room"]["id"]
        self.assertEqual(self.call("/watch-together/rooms/action", {"room_id": room_id, "action": "resync"})[0], 200)
        self.assertEqual(self.call("/watch-together/rooms/delete", {"room_id": room_id})[0], 200)
        self.assertEqual(self.call("/watch-together/rooms/list", address=("192.0.2.1", 1))[0], 403)
        self.assertEqual(self.service.handle("/watch-together/rooms/list", method="OPTIONS", client_address=("127.0.0.1", 1))[0], 204)

    def test_invalid_or_expired_token(self):
        self.assertEqual(self.service.handle("/watch-together/rooms/list", headers={"X-ETLP-Watch-Token": "bad"}, client_address=("127.0.0.1", 1))[0], 401)
        service = WatchTogetherHttpService(self.coordinator, config=self.config, token_ttl=1)
        status, result = service.handle(
            "/watch-together/auth",
            {"server_url": "https://media.test", "user_id": "admin", "api_key": "browser-token"},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 200)
        service.clock = lambda: 10**10
        self.assertEqual(service.handle("/watch-together/rooms/list", headers={"X-ETLP-Watch-Token": result["token"]}, client_address=("127.0.0.1", 1))[0], 401)

    def test_disabled_config_does_not_start_coordinator(self):
        disabled = configparser.ConfigParser()
        disabled["watch_together"] = {
            "enable": "false", "admin_enable": "true",
            "server_url": "https://media.test", "admin_api_key": "admin-secret",
        }
        coordinator = WatchTogetherCoordinator(
            store=WatchTogetherStore(Path(self.temp.name) / "disabled.json"),
            api=FakeApi(), config=disabled,
        )
        service = WatchTogetherHttpService(coordinator, config=disabled)
        self.assertFalse(coordinator.start())
        status, body = service.handle(
            "/watch-together/auth",
            {"server_url": "https://media.test", "user_id": "admin", "api_key": "browser-token"},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "watch_together_unavailable")


if __name__ == "__main__":
    unittest.main()
