from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
source = root / "source"
version = re.search(r'__version__ = "([^"]+)"', (source / "fireicedg/__init__.py").read_text(encoding="utf-8")).group(1)
dist = root / "dist"
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "FireIceDG", "--collect-all", "customtkinter", "--add-data", str(source / "assets") + ";assets",
    "--exclude-module", "numpy", "--exclude-module", "pytest", "--exclude-module", "scipy",
    "--exclude-module", "pandas", "--exclude-module", "matplotlib",
    "--distpath", str(dist), "--workpath", str(root / "build"), "--specpath", str(root / "build"),
    str(source / "main.py")], check=True)
archive_path = dist / f"FireIceDG-v{version}-Windows.zip"
with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    archive.write(dist / "FireIceDG.exe", "FireIceDG/FireIceDG.exe")
    for name in ("开始使用.md", "更新记录.md", "第三方组件.md"):
        archive.write(root / name, "FireIceDG/" + name)
    for file in (source / "assets/licenses").rglob("*"):
        if file.is_file():
            archive.write(file, "FireIceDG/licenses/" + file.name)
print(archive_path)
