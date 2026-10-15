using System;
using System.IO;
using System.Reflection;
using FireIceDG;

// Run the real monitor initialization and manual-play guard with a minimal
// reflection-compatible game API. No Unity runtime or physical device is used.
public static class Program
{
    public static int Main(string[] args)
    {
        string profile = Path.GetFullPath(args[0]);
        Directory.CreateDirectory(profile);
        File.WriteAllText(Path.Combine(profile, "bridge.key"), Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N"));
        Environment.SetEnvironmentVariable("FIREICEDG_DATA_DIR", profile);
        try
        {
            Check(typeof(AutoField), true, false);
            Check(typeof(AutoProperty), true, false);
            Check(typeof(MissingAuto), false, false);
            Console.WriteLine("PASS: field, property and missing-auto startup contracts.");
            return 0;
        }
        catch (Exception error)
        {
            Console.WriteLine("FAIL: " + error.Message);
            return 1;
        }
    }

    private static void Check(Type autoType, bool expectedReady, bool autoPlay)
    {
        HarmonyLib.AccessTools.AutoType = autoType;
        HarmonyLib.Harmony.PatchCalls = 0;
        MonitorCore.Enabled = true;
        bool loaded = MonitorCore.Start(message => Console.WriteLine(autoType.Name + ": " + message));
        try
        {
            if (loaded != expectedReady || MonitorCore.Ready != expectedReady)
                throw new Exception(autoType.Name + " did not become ready as expected.");
            if (!expectedReady) return;
            if (HarmonyLib.Harmony.PatchCalls != 2)
                throw new Exception("Failure and judgement hooks must both be installed.");
            MonitorCore.BeforeFail(GameController.instance, out bool manual);
            if (!manual) throw new Exception(autoType.Name + " did not accept manual gameplay.");
            autoType.GetMethod("SetAuto").Invoke(null, new object[] { true });
            MonitorCore.BeforeFail(GameController.instance, out bool automated);
            if (automated) throw new Exception(autoType.Name + " accepted automatic gameplay.");
            autoType.GetMethod("SetAuto").Invoke(null, new object[] { false });
        }
        finally { MonitorCore.Stop(); }
    }
}

public enum GameState { PlayerControl, Fail }
public enum HitMargin { Perfect, TooEarly, FailMiss }
public class GameController
{
    public static GameController instance { get; } = new GameController();
    public bool gameworld = true;
    public bool paused { get; set; } = false;
    public int currentSeqID = 5;
    public GameState currentState = GameState.PlayerControl;
    public GameState state => currentState;
    public void FailAction() { }
}
public class MarginTracker { public void AddHit(HitMargin margin) { } }
public static class AutoField
{
    public static bool auto = false;
    public static void SetAuto(bool value) { auto = value; }
}
public static class AutoProperty
{
    public static bool auto { get; set; } = false;
    public static void SetAuto(bool value) { auto = value; }
}
public static class MissingAuto { }

namespace HarmonyLib
{
    public static class AccessTools
    {
        public static Type AutoType;
        public static Type TypeByName(string name)
        {
            return name == "scrController" ? typeof(GameController)
                 : name == "scrMarginTracker" ? typeof(MarginTracker)
                 : name == "RDC" ? AutoType : null;
        }
        private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance;
        public static FieldInfo Field(Type type, string name) { return type.GetField(name, Flags); }
        public static PropertyInfo Property(Type type, string name) { return type.GetProperty(name, Flags); }
        public static MethodInfo Method(Type type, string name) { return type.GetMethod(name, Flags); }
    }
    public class HarmonyMethod
    {
        public HarmonyMethod(Type type, string name) { }
    }
    public class Harmony
    {
        public static int PatchCalls;
        public Harmony(string id) { }
        public MethodInfo Patch(MethodBase original, HarmonyMethod prefix, HarmonyMethod postfix, HarmonyMethod transpiler, HarmonyMethod finalizer)
        {
            PatchCalls++;
            return null;
        }
        public void UnpatchSelf() { }
    }
}
