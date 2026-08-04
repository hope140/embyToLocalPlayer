import tempfile
import unittest
from pathlib import Path

from utils.watch_together_coordinator import (
    TICKS_PER_SECOND,
    EmbyAdminApi,
    WatchTogetherCoordinator,
)
from utils.watch_together_store import WatchTogetherStore


class FakeAdminApi:
    server_url = "https://media.test"
    server_id = "server-1"

    def __init__(self, *, update=True):
        self.update = update
        self.commands = []
        self.sessions = [self.session("u1", "s1", 10), self.session("u2", "s2", 10)]

    @staticmethod
    def session(user_id, session_id, position, *, item="item", paused=False, device="watch-together"):
        return {
            "Id": session_id, "UserId": user_id, "Client": "embyToLocalPlayer",
            "DeviceName": device, "DeviceId": "etlp-wt-" + user_id,
            "LastActivityDate": "2026-01-01T00:00:00Z",
            "NowPlayingItem": {"Id": item, "RunTimeTicks": 120 * TICKS_PER_SECOND},
            "PlayState": {"PositionTicks": int(position * TICKS_PER_SECOND),
                           "RunTimeTicks": 120 * TICKS_PER_SECOND, "IsPaused": paused,
                           "PlaybackRate": 1.0},
        }

    def get_sessions(self):
        return self.sessions

    def get_users_for_ui(self):
        return [{"id": "u1", "name": "One"}, {"id": "u2", "name": "Two"}]

    def get_system_info(self):
        return {"Id": self.server_id}

    def send_command(self, session_id, command, *, position_ticks=None):
        self.commands.append((session_id, command, position_ticks))
        if not self.update:
            return
        for session in self.sessions:
            if session["Id"] != session_id:
                continue
            state = session["PlayState"]
            if command == "Pause":
                state["IsPaused"] = True
            elif command == "Unpause":
                state["IsPaused"] = False
            elif command == "Seek":
                state["PositionTicks"] = int(position_ticks)


class WatchTogetherCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WatchTogetherStore(Path(self.temp.name) / "rooms.json")
        self.api = FakeAdminApi()
        self.coordinator = WatchTogetherCoordinator(
            store=self.store, api=self.api, enabled=True,
            server_url=self.api.server_url, admin_api_key="admin-secret",
        )
        self.room = self.coordinator.create_room(
            name="test", participant_user_ids=["u1", "u2"], primary_user_id="u1"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_session_filter_requires_control_device_and_picks_latest(self):
        old = self.api.session("u1", "old", 1)
        old["LastActivityDate"] = "2025-01-01T00:00:00Z"
        bad = self.api.session("u1", "bad", 20, device="browser")
        bad["DeviceId"] = "browser-device"
        selected = self.coordinator.select_control_sessions(
            [old, bad, self.api.sessions[0]], ["u1", "u2"]
        )
        self.assertEqual(selected["u1"]["Id"], "s1")

    def test_barrier_runs_pause_seek_restore_in_separate_rounds(self):
        rid = self.room["id"]
        self.coordinator.poll_once(now=0)
        self.assertEqual(self.coordinator.runtime[rid]["state"], "barrier")
        self.assertEqual([command for _, command, _ in self.api.commands], ["Pause", "Pause"])
        self.coordinator.poll_once(now=1)
        self.assertEqual(self.coordinator.runtime[rid]["barrier"]["stage"], "seek")
        self.coordinator.poll_once(now=2)
        self.assertEqual(self.api.commands[-1][1], "Seek")
        self.coordinator.poll_once(now=3)
        self.assertEqual(self.coordinator.runtime[rid]["barrier"]["stage"], "restore")
        self.coordinator.poll_once(now=4)
        self.assertEqual(self.api.commands[-1][1], "Unpause")
        self.coordinator.poll_once(now=5)
        self.assertEqual(self.coordinator.runtime[rid]["state"], "watching")

    def test_item_duration_and_rate_mismatch_waits_and_pauses_other(self):
        self.coordinator.poll_once(now=0)
        self.coordinator.poll_once(now=1)
        self.api.sessions[1]["NowPlayingItem"]["Id"] = "different"
        self.api.sessions[1]["PlayState"]["IsPaused"] = False
        self.coordinator.poll_once(now=2)
        self.assertEqual(self.coordinator.runtime[self.room["id"]]["state"], "waiting")
        self.assertTrue(any(command == "Pause" for _, command, _ in self.api.commands))
        self.api.sessions[1]["NowPlayingItem"]["Id"] = "item"
        self.api.sessions[1]["PlayState"]["PlaybackRate"] = 1.2
        self.coordinator.poll_once(now=3)
        self.assertEqual(self.coordinator.runtime[self.room["id"]]["state"], "waiting")

    def test_pause_propagates_and_primary_wins_same_round(self):
        rid = self.room["id"]
        for now in range(6):
            self.coordinator.poll_once(now=now)
        self.assertEqual(self.coordinator.runtime[rid]["state"], "watching")
        self.api.sessions[0]["PlayState"]["IsPaused"] = True
        self.api.sessions[1]["PlayState"]["IsPaused"] = False
        self.coordinator.poll_once(now=6)
        self.assertEqual(self.api.commands[-1][1], "Pause")
        self.assertEqual(self.api.sessions[1]["PlayState"]["IsPaused"], True)

    def test_unacknowledged_command_retries_once_then_waits_without_storm(self):
        rid = self.room["id"]
        self.api.update = False
        self.coordinator.poll_once(now=0)
        initial_count = len(self.api.commands)
        self.coordinator.poll_once(now=1)
        self.coordinator.poll_once(now=3)
        retry_count = len(self.api.commands)
        self.coordinator.poll_once(now=6)
        self.assertEqual(len(self.api.commands), retry_count)
        self.assertGreaterEqual(retry_count, initial_count)
        self.assertEqual(self.coordinator.runtime[rid]["state"], "waiting")

    def test_acknowledged_action_echo_is_suppressed(self):
        for now in range(6):
            self.coordinator.poll_once(now=now)
        before = len(self.api.commands)
        self.coordinator.action(self.room["id"], "pause")
        self.coordinator.poll_once(now=7)
        # The snapshots acknowledge both action commands; they must not be
        # interpreted as a fresh user pause and broadcast a second round.
        self.assertEqual(len(self.api.commands), before + 2)


class EmbyAdminApiTests(unittest.TestCase):
    def test_paths_headers_and_verify_use_temporary_token(self):
        calls = []

        def request(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("System/Info"):
                return {"Id": "sid"}
            if "/Users/u1" in url:
                return {"Id": "u1", "Policy": {"IsAdministrator": True}}
            return {"Items": []}

        api = EmbyAdminApi("https://media.test/emby", "admin-secret", request_func=request)
        self.assertTrue(api.verify_admin_user("u1", "browser-token"))
        api.send_command("session", "Seek", position_ticks=123)
        self.assertEqual(calls[-1][0], "https://media.test/emby/Sessions/session/Playing/Seek")
        self.assertEqual(calls[-1][1]["params"]["SeekPositionTicks"], 123)
        self.assertEqual(calls[-1][1]["headers"]["X-Emby-Token"], "admin-secret")
        self.assertNotIn("browser-token", api.admin_api_key)


if __name__ == "__main__":
    unittest.main()
