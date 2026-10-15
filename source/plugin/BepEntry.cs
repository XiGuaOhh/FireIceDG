using BepInEx;

namespace FireIceDG
{
    [BepInPlugin("local.fireicedg.monitor", "FireIceDG Monitor", "1.0.1")]
    public sealed class Plugin : BaseUnityPlugin
    {
        private void Awake() { MonitorCore.Start(message => Logger.LogInfo(message)); }
        private void Update() { MonitorCore.Update(); }
        private void OnDestroy() { MonitorCore.Stop(); }
    }
}
