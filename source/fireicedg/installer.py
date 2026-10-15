from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path

from .config import resources


def game_running() -> bool:
    if os.name != "nt":
        return False
    import ctypes.wintypes as w
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("无法检查游戏是否运行；请先退出游戏。")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        exists = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while exists:
            if entry.szExeFile.lower() in {"a dance of fire and ice.exe", "adofai.exe"}:
                return True
            exists = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


def discover_game() -> str:
    candidates = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                root = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            candidates.append(root)
            library = root / "steamapps" / "libraryfolders.vdf"
            if library.exists():
                for path in re.findall(r'"path"\s+"([^"]+)"', library.read_text(encoding="utf-8")):
                    candidates.append(Path(path.replace("\\\\", "\\")))
        except OSError:
            pass
    for root in candidates:
        game = root / "steamapps" / "common" / "A Dance of Fire and Ice"
        if (game / "A Dance of Fire and Ice.exe").is_file():
            return str(game)
    return ""


def valid_game(path: Path) -> None:
    executable = path / "A Dance of Fire and Ice.exe"
    if not executable.is_file():
        raise ValueError("请选择包含 A Dance of Fire and Ice.exe 的游戏目录。")
    if not (path / "A Dance of Fire and Ice_Data" / "Managed" / "Assembly-CSharp.dll").is_file():
        raise ValueError("此版本不是受支持的 Windows Mono 版游戏。")
    with executable.open("rb") as stream:
        stream.seek(0x3C)
        position = struct.unpack("<I", stream.read(4))[0]
        stream.seek(position)
        if stream.read(4) != b"PE\0\0" or struct.unpack("<H", stream.read(2))[0] != 0x8664:
            raise ValueError("此安装包适用于 64 位 Windows 游戏。")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(path: str, profile: Path, token: str) -> str:
    root = Path(path).resolve()
    valid_game(root)
    if game_running():
        raise RuntimeError("游戏正在运行。请先退出游戏，再点击安装插件；软件不会替你关闭游戏。")
    asset = resources()
    manifest = root / "FireIceDG.install.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"created": []}
    managed = root / "A Dance of Fire and Ice_Data" / "Managed"
    has_umm = (managed / "UnityModManager" / "UnityModManager.dll").is_file()
    has_bep = (root / "BepInEx" / "core" / "BepInEx.dll").is_file()
    if has_umm and has_bep:
        raise RuntimeError("检测到两个加载器，请先只保留一个，避免插件重复加载。")
    if has_umm:
        loader = "Unity Mod Manager"
        copies = [(asset / "FireIceDG.Umm.dll", root / "Mods" / "FireIceDG" / "FireIceDG.Umm.dll"),
                  (asset / "Info.json", root / "Mods" / "FireIceDG" / "Info.json")]
    else:
        loader = "BepInEx"
        copies = [(asset / "FireIceDG.Bep.dll", root / "BepInEx" / "plugins" / "FireIceDG" / "FireIceDG.Bep.dll")]
    created = list(previous.get("created", []))
    saved = []
    def copy_new(source: Path, target: Path, allow_update=False):
        target = target.resolve()
        if not target.is_relative_to(root):
            raise ValueError("安装文件路径越界")
        if target.exists():
            if not allow_update:
                raise RuntimeError("安装文件已存在，未覆盖：" + target.name)
            relative = target.relative_to(root).as_posix()
            known = next((item for item in created if item["path"] == relative), None)
            if not known or digest(target) != known["sha256"]:
                raise RuntimeError("插件文件已存在且可能被修改，请先卸载旧插件。")
            saved.append((target, target.read_bytes()))
            created.remove(known)
        else:
            saved.append((target, None))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        created.append({"path": target.relative_to(root).as_posix(), "sha256": digest(target)})
    try:
        if not has_umm and not has_bep:
            if any((root / name).exists() for name in ("winhttp.dll", "doorstop_config.ini", "MelonLoader", "version.dll")):
                raise RuntimeError("检测到已有加载器文件，未覆盖。请使用现有 UMM 或 BepInEx 5，或先恢复游戏目录。")
            extracted = profile / "loader-package"
            extracted.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(asset / "BepInEx_win_x64_5.4.23.5.zip") as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    target = (root / member.filename).resolve()
                    if not target.is_relative_to(root):
                        raise ValueError("加载器文件路径越界")
                    scratch = (extracted / member.filename).resolve()
                    if not scratch.is_relative_to(extracted.resolve()):
                        raise ValueError("解压路径越界")
                    scratch.parent.mkdir(parents=True, exist_ok=True)
                    scratch.write_bytes(archive.read(member))
                    copy_new(scratch, target)
        for source, target in copies:
            copy_new(source, target, allow_update=True)
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "bridge.key").write_text(token + "\n", encoding="utf-8")
        manifest.write_text(json.dumps({"version": 1, "loader": loader, "created": created},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        for target, backup in reversed(saved):
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(backup)
        raise
    return "监听插件已安装（" + loader + "）。重新启动游戏后会自动连接。"


def uninstall(path: str) -> str:
    root = Path(path).resolve()
    valid_game(root)
    if game_running():
        raise RuntimeError("请先退出游戏再卸载插件。")
    manifest = root / "FireIceDG.install.json"
    if not manifest.is_file():
        raise RuntimeError("没有本软件的安装记录，未删除任何游戏文件。")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    remaining = []
    other_plugins = any(item.is_file() and item.name.lower().endswith(".dll")
                        and item.resolve() != (root / "BepInEx/plugins/FireIceDG/FireIceDG.Bep.dll").resolve()
                        for item in (root / "BepInEx/plugins").rglob("*")
                        ) if (root / "BepInEx/plugins").exists() else False
    for item in data["created"]:
        target = (root / item["path"]).resolve()
        if not target.is_relative_to(root):
            raise ValueError("卸载路径越界")
        if not target.exists():
            continue
        if other_plugins and not item["path"].startswith(("BepInEx/plugins/FireIceDG/", "Mods/FireIceDG/")):
            remaining.append(item)
            continue
        if digest(target) == item["sha256"]:
            target.unlink()
        else:
            remaining.append(item)
    if remaining:
        data["created"] = remaining
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ("监听插件已卸载；其他插件需要的共享加载器已保留。" if other_plugins else
                "已卸载未修改的文件；" + str(len(remaining)) + " 个有改动的文件已保留。")
    manifest.unlink()
    return "监听插件已卸载。原游戏文件未被替换。"
