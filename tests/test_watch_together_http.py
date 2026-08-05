import configparser
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from utils.watch_together_coordinator import WatchTogetherCoordinator, WatchTogetherHttpService
from utils.watch_together_store import WatchTogetherStore
from utils.http_server import ThreadingHTTPServer, UserScriptRequestHandler


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

    def test_auth_response_context_and_store_exclude_credentials(self):
        status, result = self.service.handle(
            "/watch-together/auth",
            {"server_url": "https://media.test", "user_id": "admin", "api_key": "browser-token"},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"token", "expires_in"})
        self.assertNotEqual(result["token"], "browser-token")
        response_text = json.dumps(result)
        self.assertNotIn("admin-secret", response_text)
        self.assertNotIn("browser-token", response_text)

        context = self.service._tokens[result["token"]]
        self.assertEqual(set(context), {"user_id", "server_url", "expires_at"})
        context_text = json.dumps(context)
        self.assertNotIn("admin-secret", context_text)
        self.assertNotIn("browser-token", context_text)

        status, _ = self.service.handle(
            "/watch-together/rooms/create",
            {"name": "secret-check", "participant_user_ids": ["admin", "guest"],
             "primary_user_id": "admin"},
            headers={"X-ETLP-Watch-Token": result["token"]},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 200)
        persisted = self.coordinator.store.store_path.read_text(encoding="utf-8")
        self.assertNotIn("admin-secret", persisted)
        self.assertNotIn("browser-token", persisted)

    def test_participant_auth_returns_mode_without_credentials_or_store(self):
        participant = configparser.ConfigParser()
        participant["watch_together"] = {
            "enable": "true", "admin_enable": "false",
            "server_url": "https://media.test", "admin_api_key": "",
        }
        store_path = Path(self.temp.name) / "participant-rooms.json"
        coordinator = WatchTogetherCoordinator(
            api=FakeApi(), config=participant, store_path=store_path,
        )
        service = WatchTogetherHttpService(coordinator, config=participant)
        status, body = service.handle(
            "/watch-together/auth",
            {"server_url": "https://media.test", "user_id": "guest", "api_key": "browser-token"},
            client_address=("127.0.0.1", 1),
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "watch_together_participant_mode")
        response_text = json.dumps(body)
        self.assertNotIn("browser-token", response_text)
        self.assertNotIn("admin-secret", response_text)
        self.assertNotIn("token", body)
        self.assertFalse(store_path.exists())

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

    def test_disabled_corrupt_store_is_not_read_or_created(self):
        disabled = configparser.ConfigParser()
        disabled["watch_together"] = {
            "enable": "false", "admin_enable": "false",
            "server_url": "", "admin_api_key": "",
        }
        path = Path(self.temp.name) / "disabled-corrupt.json"
        path.write_text("{broken", encoding="utf-8")
        service = WatchTogetherHttpService(config=disabled, store_path=path)
        self.assertIsNone(service.coordinator.store)
        self.assertFalse(service.start())
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_enabled_corrupt_store_returns_503_and_keeps_file(self):
        enabled = configparser.ConfigParser()
        enabled["watch_together"] = {
            "enable": "true", "admin_enable": "true",
            "server_url": "https://media.test", "admin_api_key": "admin-secret",
        }
        path = Path(self.temp.name) / "enabled-corrupt.json"
        path.write_text('{"schema_version": 999, "rooms": []}', encoding="utf-8")
        service = WatchTogetherHttpService(config=enabled, store_path=path)
        status, body = service.handle(
            "/watch-together/auth", {}, client_address=("127.0.0.1", 1)
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "watch_together_unavailable")
        self.assertFalse(service.start())
        self.assertEqual(path.read_bytes(), b'{"schema_version": 999, "rooms": []}')

    def test_start_swallows_unexpected_coordinator_error(self):
        def fail_start():
            raise RuntimeError("unexpected start failure")

        original_start = self.coordinator.start
        self.coordinator.start = fail_start
        try:
            self.assertFalse(self.service.start())
        finally:
            self.coordinator.start = original_start

    def test_real_http_handler_cors_204_get_compatibility_and_single_call(self):
        class RecordingService:
            def __init__(self):
                self.calls = 0

            def handle(self, path, body, *, headers, client_address, method):
                self.calls += 1
                if method == "OPTIONS":
                    return 204, {}
                return 200, {"ok": True}

            def start(self):
                return False

            def stop(self, timeout=5):
                return None

        service = RecordingService()
        server = ThreadingHTTPServer(("127.0.0.1", 0), UserScriptRequestHandler)
        server.watch_together_service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(
                base + "/watch-together/rooms/list",
                data=b"{}", method="POST", headers={"Content-Type": "application/json"},
            )
            response = urllib.request.urlopen(request)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            self.assertEqual(service.calls, 1)

            options = urllib.request.Request(base + "/watch-together/rooms/list", method="OPTIONS")
            response = urllib.request.urlopen(options)
            self.assertEqual(response.status, 204)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers.get("Content-Length"), "0")
            self.assertIn("X-ETLP-Watch-Token", response.headers["Access-Control-Allow-Headers"])
            self.assertEqual(service.calls, 2)

            response = urllib.request.urlopen(base + "/")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"Server is running")
            self.assertEqual(service.calls, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_handler_internal_type_error_is_not_retried(self):
        class FailingService:
            calls = 0

            def handle(self, *args, **kwargs):
                self.calls += 1
                raise TypeError("intentional")

        service = FailingService()
        server = ThreadingHTTPServer(("127.0.0.1", 0), UserScriptRequestHandler)
        server.watch_together_service = service
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/watch-together/auth",
                data=b"{}", method="POST", headers={"Content-Type": "application/json"},
            )
            try:
                response = urllib.request.urlopen(request)
            except urllib.error.HTTPError as exc:
                response = exc
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())["error"]["code"], "watch_together_error")
            self.assertEqual(service.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
