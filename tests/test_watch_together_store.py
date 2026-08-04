import json
import tempfile
import unittest
from pathlib import Path

from utils.watch_together_store import WatchTogetherStore, WatchTogetherStoreError


class WatchTogetherStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "watch_together_rooms.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_round_trip_and_delete(self):
        store = WatchTogetherStore(self.path)
        room = store.create_room(
            server_id="server-1", server_url="https://MEDIA.example/emby",
            name="Movie", participant_user_ids=["u1", "u2"], primary_user_id="u1",
        )
        self.assertEqual(room["server_url"], "https://media.example")
        self.assertEqual(WatchTogetherStore(self.path).list_rooms(), [room])
        self.assertIn('"schema_version": 1', self.path.read_text(encoding="utf-8"))
        self.assertTrue(store.delete_room(room["id"]))
        self.assertEqual(store.list_rooms(), [])
        self.assertFalse(store.delete_room(room["id"]))

    def test_member_and_primary_validation(self):
        store = WatchTogetherStore(self.path)
        with self.assertRaises(WatchTogetherStoreError):
            store.create_room(server_id="s", server_url="https://x.test", name="x",
                              participant_user_ids=["u1", "u1"], primary_user_id="u1")
        with self.assertRaises(WatchTogetherStoreError):
            store.create_room(server_id="s", server_url="https://x.test", name="x",
                              participant_user_ids=["u1", "u2"], primary_user_id="u3")

    def test_corrupt_or_unknown_schema_is_not_overwritten(self):
        self.path.write_text('{"schema_version": 999, "rooms": []}', encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(WatchTogetherStoreError):
            WatchTogetherStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("not-json", encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(WatchTogetherStoreError):
            WatchTogetherStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_secrets_and_unknown_fields_rejected(self):
        payload = {
            "schema_version": 1,
            "rooms": [{
                "id": "00000000-0000-0000-0000-000000000001",
                "server_id": "s", "server_url": "https://x.test", "name": "x",
                "participant_user_ids": ["u1", "u2"], "primary_user_id": "u1",
                "created_at": "2026-01-01T00:00:00Z", "api_key": "secret",
            }],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(WatchTogetherStoreError):
            WatchTogetherStore(self.path)

    def test_member_cannot_belong_to_two_rooms_on_same_server(self):
        store = WatchTogetherStore(self.path)
        store.create_room("s1", "https://one.test", "one", ["u1", "u2"], "u1")
        with self.assertRaises(WatchTogetherStoreError):
            store.create_room("s1", "https://one.test", "two", ["u1", "u3"], "u1")
        # A user may participate in a room on a different Emby server.
        store.create_room("s2", "https://two.test", "two", ["u1", "u3"], "u1")

    def test_load_rejects_same_server_member_overlap(self):
        payload = {
            "schema_version": 1,
            "rooms": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "server_id": "s", "server_url": "https://x.test", "name": "one",
                    "participant_user_ids": ["u1", "u2"], "primary_user_id": "u1",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "server_id": "s", "server_url": "https://x.test", "name": "two",
                    "participant_user_ids": ["u1", "u3"], "primary_user_id": "u1",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(WatchTogetherStoreError):
            WatchTogetherStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_create_and_delete_roll_back_memory_when_write_fails(self):
        store = WatchTogetherStore(self.path)
        original_write = store._write
        store._write = lambda: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(Exception):
            store.create_room("s", "https://x.test", "x", ["u1", "u2"], "u1")
        self.assertEqual(store.list_rooms(), [])
        store._write = original_write
        room = store.create_room("s", "https://x.test", "x", ["u1", "u2"], "u1")
        store._write = lambda: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(Exception):
            store.delete_room(room["id"])
        self.assertEqual(store.list_rooms(), [room])


if __name__ == "__main__":
    unittest.main()
