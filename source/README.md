# FireIceDG source

Python 3.12 desktop application + .NET Framework 4.7.2 event-only game plugins.

```powershell
python -m pip install -r requirements.txt
python main.py
python -m unittest discover -s tests -v
dotnet run --project plugin-tests/PluginContractTests.csproj -- ../plugin-test-profile
```

`assets/` contains compiled monitor plugins and the unmodified official BepInEx x64 release. No game DLLs are redistributed.

To rebuild plugins, install a .NET SDK and pass your ADOFAI installation and an extracted BepInEx distribution:

```powershell
dotnet build plugin/FireIceDG.csproj -c Release -p:Loader=Bep '-p:GamePath=C:/Games/A Dance of Fire and Ice' '-p:BepPath=C:/path/to/extracted/BepInEx' -o ../plugin-build-bep
dotnet build plugin/FireIceDG.csproj -c Release -p:Loader=Umm '-p:GamePath=C:/Games/A Dance of Fire and Ice' '-p:BepPath=C:/path/to/extracted/BepInEx' -o ../plugin-build-umm
```

Copy the resulting DLLs into `assets/`. Build the standalone Windows app with:

```powershell
python -m pip install pyinstaller==6.21.0
python -m PyInstaller --noconfirm --clean --onefile --windowed --name FireIceDG --collect-all customtkinter --add-data 'assets;assets' --exclude-module numpy --exclude-module pytest main.py
```

The GUI never sends device commands directly. All output passes through the background engine, immutable settings validation, a live game heartbeat, a manually enabled session, a stable device identity, generation checks and cooldown. Simulator events never call the physical output path. Device tasks are finite, sent concurrently, and cleared on stop. V4 device-op responses are delayed until task completion; sending intensity and then waiting before sending waveform would be incorrect.

Version 1.0.2 permits strength 0–200 and independent, nonnegative duration/cooldown values with no software upper bound. A zero duration emits no output; zero cooldown permits immediate retriggering. Retriggering replaces the previous pulse and starts its duration again. A pulse sequence guard prevents an old completion or error from affecting the replacement. Long durations use small transmission chunks and integer monotonic deadlines, keeping frame allocation and APP timers bounded without capping the total configured duration. APP intensity limits and emergency stop continue to apply.

The game bridge authenticates packets with an installation key, accepts only local packets, rejects stale or reordered events, and requires a heartbeat before accepting a new game session. The same key is read by the plugin from the user's application profile. Profile paths can be overridden by `FIREICEDG_DATA_DIR` for testing; this is used only for development.

Version 1.0.1 fixes initialization on games where `RDC.auto` is a static property rather than a field. The plugin contract test runs the real monitor initialization and manual-gameplay guard against both API shapes, and checks that a missing member still prevents arming.

Installer changes are reversible and recorded with file hashes. Existing loader files are never overwritten. Game files and third-party mods are preserved. Do not install while the game is running.

`main.py --selfcheck` starts the GUI briefly without permitting arming. Automated tests use a mock APP on loopback and never access a real device.

For use instructions and current validation limits, see `../开始使用.md`.

Version 1.0.3 serializes connection changes and output preparation/stop operations,
invalidates queued arming requests on stop, and clears failed pairing sessions.
Device patches and game session changes are validated before committing state.
Unexpected APP task termination disarms instead of silently staying enabled.
The GUI supports numeric strength entry and displays APP channel limits; failed
preference writes leave the applied settings intact. Damaged preferences recover
the installed bridge key when possible.

Engine tests inject packets into the real GameReceiver callback for deterministic
event-path coverage. SocketTests separately checks actual OS UDP delivery and
explicitly skips when the environment cannot deliver even a loopback probe.
Such a skip does NOT establish that communication with the installed game works.
