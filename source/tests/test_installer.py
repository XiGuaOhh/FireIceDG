import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fireicedg import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "game"
        self.root.mkdir()
        executable = bytearray(1024)
        struct.pack_into("<I", executable, 0x3C, 128)
        executable[128:132] = b"PE\0\0"
        struct.pack_into("<H", executable, 132, 0x8664)
        (self.root / "A Dance of Fire and Ice.exe").write_bytes(executable)
        managed = self.root / "A Dance of Fire and Ice_Data/Managed"
        managed.mkdir(parents=True)
        (managed / "Assembly-CSharp.dll").write_bytes(b"original game data")
        self.original = (managed / "Assembly-CSharp.dll").read_bytes()
        self.profile = Path(self.temp.name) / "profile"
        self.profile.mkdir()
        self.process_check = patch.object(installer, "game_running", return_value=False)
        self.process_check.start()

    def tearDown(self):
        self.process_check.stop()
        self.temp.cleanup()

    def install(self):
        return installer.install(str(self.root), self.profile, "a" * 64)

    def test_install_update_and_uninstall_preserve_original_game(self):
        self.install()
        plugin = self.root / "BepInEx/plugins/FireIceDG/FireIceDG.Bep.dll"
        self.assertTrue(plugin.is_file())
        self.assertTrue((self.root / "winhttp.dll").is_file())
        self.install()
        installer.uninstall(str(self.root))
        self.assertFalse(plugin.exists())
        self.assertFalse((self.root / "winhttp.dll").exists())
        self.assertEqual((self.root / "A Dance of Fire and Ice_Data/Managed/Assembly-CSharp.dll").read_bytes(), self.original)

    def test_modified_file_is_retained(self):
        self.install()
        file = self.root / "doorstop_config.ini"
        file.write_text("modified by user")
        installer.uninstall(str(self.root))
        self.assertEqual(file.read_text(), "modified by user")

    def test_unknown_loader_is_never_overwritten(self):
        file = self.root / "winhttp.dll"
        file.write_bytes(b"other loader")
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(file.read_bytes(), b"other loader")
        self.assertFalse((self.root / "FireIceDG.install.json").exists())

    def test_shared_loader_retained_for_other_plugins(self):
        self.install()
        plugin = self.root / "BepInEx/plugins/OtherMod.dll"
        plugin.write_bytes(b"unrelated mod")
        result = installer.uninstall(str(self.root))
        self.assertIn("共享加载器", result)
        self.assertTrue((self.root / "winhttp.dll").is_file())
        self.assertTrue(plugin.is_file())
        self.assertFalse((self.root / "BepInEx/plugins/FireIceDG/FireIceDG.Bep.dll").exists())

    def test_running_game_blocks_installation(self):
        with patch.object(installer, "game_running", return_value=True):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertFalse((self.root / "winhttp.dll").exists())


if __name__ == "__main__":
    unittest.main()
