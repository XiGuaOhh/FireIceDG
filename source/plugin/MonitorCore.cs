using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using HarmonyLib;

namespace FireIceDG
{
    public static class MonitorCore
    {
        private static UdpClient socket;
        private static string token;
        private static readonly string session = Guid.NewGuid().ToString("N");
        private static long sequence;
        private static int ticks;
        private static Harmony harmony;
        private static Type controllerType;
        private static Type rdcType;
        private static PropertyInfo instance;
        private static FieldInfo gameworld;
        private static FieldInfo tile;
        private static FieldInfo auto;
        private static PropertyInfo autoProperty;
        private static PropertyInfo paused;
        private static FieldInfo currentState;
        private static PropertyInfo state;
        private static Action<string> logger;
        private static DateTime lastHeartbeat = DateTime.MinValue;
        public static bool Enabled = true;
        public static bool Ready { get; private set; }

        public static bool Start(Action<string> log)
        {
            logger = log;
            try
            {
                string overridePath = Environment.GetEnvironmentVariable("FIREICEDG_DATA_DIR");
                string directory = string.IsNullOrEmpty(overridePath)
                    ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FireIceDG")
                    : overridePath;
                string[] configuration = File.ReadAllLines(Path.Combine(directory, "bridge.key"));
                token = configuration[0].Trim();
                if (token.Length != 64 || token.Any(c => !Uri.IsHexDigit(c)))
                    throw new InvalidDataException("Invalid bridge key; install from the FireIceDG app.");
                socket = new UdpClient();
                socket.Connect(IPAddress.Loopback, 19795);
                controllerType = AccessTools.TypeByName("scrController");
                rdcType = AccessTools.TypeByName("RDC");
                Type marginType = AccessTools.TypeByName("scrMarginTracker");
                if (controllerType == null || marginType == null)
                    throw new MissingMemberException("ADOFAI types were not found.");
                instance = AccessTools.Property(controllerType, "instance");
                gameworld = AccessTools.Field(controllerType, "gameworld");
                tile = AccessTools.Field(controllerType, "currentSeqID");
                paused = AccessTools.Property(controllerType, "paused");
                currentState = AccessTools.Field(controllerType, "currentState");
                state = AccessTools.Property(controllerType, "state");
                auto = rdcType?.GetField("auto", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                autoProperty = rdcType?.GetProperty("auto", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                if (auto != null && (auto.FieldType != typeof(bool) || !auto.IsStatic)) auto = null;
                if (autoProperty != null && (autoProperty.PropertyType != typeof(bool)
                    || autoProperty.GetGetMethod(true) == null || !autoProperty.GetGetMethod(true).IsStatic)) autoProperty = null;
                MethodInfo fail = AccessTools.Method(controllerType, "FailAction");
                MethodInfo addHit = AccessTools.Method(marginType, "AddHit");
                if (fail == null || addHit == null || addHit.GetParameters().Length != 1
                    || !addHit.GetParameters()[0].ParameterType.IsEnum || instance == null
                    || gameworld == null || paused == null || tile == null || (auto == null && autoProperty == null))
                    throw new MissingMethodException("The game API changed; output is disabled.");
                harmony = new Harmony("local.fireicedg.monitor");
                InstallPatch(fail, new HarmonyMethod(typeof(MonitorCore), nameof(BeforeFail)),
                                  new HarmonyMethod(typeof(MonitorCore), nameof(AfterFail)));
                InstallPatch(addHit, null, new HarmonyMethod(typeof(MonitorCore), nameof(AfterHit)));
                Ready = true;
                Send("heartbeat", "", false);
                logger("FireIceDG connected: failure and judgement hooks are ready. No game state is changed.");
                return true;
            }
            catch (Exception error)
            {
                Ready = false;
                logger("FireIceDG unavailable: " + error.Message);
                Send("heartbeat", "", false);
                return false;
            }
        }

        private static object Controller()
        {
            try { return instance == null ? null : instance.GetValue(null, null); }
            catch { return null; }
        }

        private static void InstallPatch(MethodBase original, HarmonyMethod prefix, HarmonyMethod postfix)
        {
            // UMM uses Harmony, while BepInEx uses HarmonyX. Resolve the shared
            // five-argument API at runtime; HarmonyX marks it compile-time obsolete.
            MethodInfo patch = typeof(Harmony).GetMethod("Patch", new[] {
                typeof(MethodBase), typeof(HarmonyMethod), typeof(HarmonyMethod),
                typeof(HarmonyMethod), typeof(HarmonyMethod) });
            if (patch == null) throw new MissingMethodException("Compatible Harmony patch API not found.");
            patch.Invoke(harmony, new object[] { original, prefix, postfix, null, null });
        }

        private static bool InManualLevel(object controller)
        {
            try
            {
                return Enabled && Ready && controller != null
                    && (bool)gameworld.GetValue(controller)
                    && !(bool)paused.GetValue(controller, null)
                    && !(bool)(auto != null ? auto.GetValue(null) : autoProperty.GetValue(null, null));
            }
            catch { return false; }
        }

        private static bool Playing(object controller)
        {
            if (!InManualLevel(controller)) return false;
            try
            {
                string name = state != null ? state.GetValue(controller, null)?.ToString()
                                           : currentState.GetValue(controller)?.ToString();
                return name == "PlayerControl" || name == "Checkpoint";
            }
            catch { return false; }
        }

        public static void BeforeFail(object __instance, out bool __state)
        {
            __state = Playing(__instance);
        }

        public static void AfterFail(bool __state)
        {
            if (__state && Enabled && Ready) Send("death", "", true);
        }

        public static void AfterHit(object[] __args)
        {
            if (__args == null || __args.Length != 1 || !InManualLevel(Controller())) return;
            string margin = __args[0]?.ToString() ?? "";
            if (margin == "Auto" || margin == "Perfect") return;
            // Failure judgements may be registered after FailAction changes the
            // controller state. Capture those as well; the desktop deduplicates.
            Send("hit", margin, true);
        }

        public static void Update()
        {
            if (!Enabled) return;
            if ((DateTime.UtcNow - lastHeartbeat).TotalMilliseconds < 500) return;
            lastHeartbeat = DateTime.UtcNow;
            Send("heartbeat", "", Playing(Controller()));
            ticks++;
        }

        private static string Escape(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "").Replace("\n", "");
        }

        private static void Send(string kind, string margin, bool playing)
        {
            if (socket == null || string.IsNullOrEmpty(token)) return;
            try
            {
                object controller = Controller();
                int floor = controller == null || tile == null ? -1 : (int)tile.GetValue(controller);
                long now = new DateTimeOffset(DateTime.UtcNow).ToUnixTimeMilliseconds();
                string version = controllerType?.Assembly.GetName().Version?.ToString() ?? "unknown";
                string json = "{\"protocol\":\"fireicedg/1\",\"token\":\"" + token
                    + "\",\"session\":\"" + session + "\",\"seq\":" + (++sequence)
                    + ",\"time\":" + now + ",\"kind\":\"" + kind + "\",\"margin\":\"" + Escape(margin)
                    + "\",\"tile\":" + floor + ",\"playing\":" + (playing ? "true" : "false")
                    + ",\"ready\":" + (Ready ? "true" : "false") + ",\"version\":\"" + Escape(version) + "\"}";
                byte[] data = Encoding.UTF8.GetBytes(json);
                socket.Send(data, data.Length);
            }
            catch (Exception error)
            {
                // A stopped desktop receiver must never interrupt game input.
                if (ticks % 120 == 0) logger?.Invoke("FireIceDG bridge: " + error.Message);
            }
        }

        public static void Stop()
        {
            Send("stop", "", false);
            Enabled = false;
            Ready = false;
            if (harmony != null)
            {
                MethodInfo unpatch = typeof(Harmony).GetMethod("UnpatchSelf", Type.EmptyTypes);
                if (unpatch != null) unpatch.Invoke(harmony, null);
                else typeof(Harmony).GetMethod("UnpatchAll", new[] { typeof(string) })
                    ?.Invoke(harmony, new object[] { "local.fireicedg.monitor" });
            }
            socket?.Close();
            socket = null;
        }
    }
}
