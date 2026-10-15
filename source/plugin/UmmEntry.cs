using System;
using System.Linq;
using System.Reflection;

namespace FireIceDG
{
    public static class UmmEntry
    {
        // UMM supports a parameterless entry method. Resolve its existing types
        // at runtime so this build doesn't redistribute UnityModManager.dll.
        public static bool Load()
        {
            Type manager = AppDomain.CurrentDomain.GetAssemblies()
                .Select(a => a.GetType("UnityModManagerNet.UnityModManager", false))
                .FirstOrDefault(t => t != null);
            object entry = manager.GetMethod("FindMod").Invoke(null, new object[] { "FireIceDG" });
            Type type = entry.GetType();
            Bind(type, entry, "OnUpdate", nameof(Update));
            Bind(type, entry, "OnToggle", nameof(Toggle));
            Bind(type, entry, "OnUnload", nameof(Unload));
            return MonitorCore.Start(message => Console.WriteLine("[FireIceDG] " + message));
        }

        private static void Bind(Type type, object entry, string name, string method)
        {
            FieldInfo field = type.GetField(name);
            if (field == null) return;
            MethodInfo callback = typeof(UmmEntry).GetMethod(method);
            field.SetValue(entry, Delegate.CreateDelegate(field.FieldType, callback));
        }

        public static void Update(object entry, float dt) { MonitorCore.Update(); }
        public static bool Toggle(object entry, bool enabled)
        {
            if (!enabled) MonitorCore.Stop();
            else if (!MonitorCore.Enabled)
            {
                MonitorCore.Enabled = true;
                MonitorCore.Start(message => Console.WriteLine("[FireIceDG] " + message));
            }
            return true;
        }
        public static bool Unload(object entry) { MonitorCore.Stop(); return true; }
    }
}
