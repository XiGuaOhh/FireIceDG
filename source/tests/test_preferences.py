import json
import socket
import tempfile
import unittest
from pathlib import Path

from fireicedg.config import Settings, load_preferences
from fireicedg.engine import Engine


class RecoveryTests(unittest.TestCase):
    def test_damaged_preferences_keep_installed_game_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            path.write_text("{broken", encoding="utf-8")
            (path.parent / "bridge.key").write_text("b" * 64 + "\n", encoding="utf-8")
            result = load_preferences(path)
            self.assertEqual(result["game_token"], "b" * 64)
            self.assertEqual(result["settings"]["strength"], 5)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)

    def test_invalid_hex_token_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            path.write_text(json.dumps({"game_token": "z" * 64}), encoding="utf-8")
            token = load_preferences(path)["game_token"]
            self.assertEqual(len(bytes.fromhex(token)), 32)

    def test_occupied_game_port_reports_start_failure(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            engine = Engine("a" * 64, Settings(), game_port=occupied.getsockname()[1])
            with self.assertRaisesRegex(RuntimeError, "游戏监听启动失败"):
                engine.start()
            engine.thread.join(timeout=2)
            self.assertFalse(engine.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
