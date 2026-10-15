from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import asdict

from .config import GAME_PORT, Settings, ms_to_seconds
from .device import DeviceLink
from .events import EventFilter, qualifies


class GameReceiver(asyncio.DatagramProtocol):
    def __init__(self, engine):
        self.engine = engine

    def datagram_received(self, data, address):
        if address[0] != "127.0.0.1":
            return
        event = self.engine.events.parse(data)
        if event:
            self.engine.game_event(event)


class Engine:
    def __init__(self, token: str, settings: Settings, game_port: int = GAME_PORT):
        self.messages = queue.Queue()
        self.events = EventFilter(token)
        self.settings = settings
        self.game_port = game_port
        self.device = DeviceLink(self.notify, self.device_changed)
        self.loop = None
        self.thread = None
        self.startup = threading.Event()
        self.startup_error = None
        self.transition_lock = asyncio.Lock()
        self.connection_lock = asyncio.Lock()
        self.connecting = False
        self.transport = None
        self.watchdog = None
        self.armed = False
        self.preparing = False
        self.generation = 0
        self.owned_slot = ""
        self.owned_client = ""
        self.active_pulse = None
        self.pulse_sequence = 0
        self.last_trigger = None
        self.misses = 0
        self.outputs = 0
        self.game_online = False
        self.game_session = ""
        self.last_failure = ("", -2, -1000.0, "")
        self.closing = False

    def notify(self, kind, value):
        if kind == "remote_stop" and self.loop and self.loop.is_running():
            self.loop.create_task(self.disarm("APP 动作 0 紧急停止"))
            return
        self.messages.put((kind, value))

    def start(self):
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()
        if not self.startup.wait(5):
            raise RuntimeError("后台启动超时")
        if self.startup_error:
            raise RuntimeError("游戏监听启动失败：" + str(self.startup_error)) from self.startup_error

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._initialize())
            self.startup.set()
            self.loop.run_forever()
        except Exception as error:
            self.startup_error = error
            self.notify("error", str(error))
            self.startup.set()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()

    async def _initialize(self):
        self.transport, _ = await self.loop.create_datagram_endpoint(
            lambda: GameReceiver(self), local_addr=("127.0.0.1", self.game_port))
        self.watchdog = asyncio.create_task(self._watchdog())
        self.notify("log", "模拟模式已启动，等待游戏监听插件。")

    def submit(self, coroutine):
        if not self.loop or not self.loop.is_running() or self.closing:
            coroutine.close()
            return None
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        def done(result):
            try:
                result.result()
            except Exception as error:
                if not self.closing:
                    self.notify("error", str(error))
        future.add_done_callback(done)
        return future

    def game_event(self, event):
        if event.kind == "heartbeat":
            if self.game_session and self.game_session != event.session and (self.armed or self.preparing):
                asyncio.create_task(self.disarm("游戏进程已更换"))
            self.game_session = event.session
            self.game_online = self.events.connected()
            self.notify("game", {"online": self.game_online, "playing": event.playing,
                                 "version": event.version, "ready": event.ready})
            if not event.ready and (self.armed or self.preparing):
                asyncio.create_task(self.disarm("游戏接口未就绪"))
            return
        if event.kind == "stop":
            self.events.ready = False
            self.game_online = False
            self.notify("game", {"online": False})
            asyncio.create_task(self.disarm("游戏监听已停止"))
            return
        if not self.events.connected() or not event.playing or not qualifies(event, self.settings.mode):
            return
        now = time.monotonic()
        if event.kind == "death" or event.margin in {"FailMiss", "FailOverload"}:
            session, tile, timestamp, previous_kind = self.last_failure
            failure_kind = event.kind if event.kind == "death" else event.margin
            if session == event.session and tile == event.tile and now - timestamp < 0.35 and previous_kind != failure_kind:
                return
            self.last_failure = (event.session, event.tile, now, failure_kind)
        self.misses += 1
        label = "关卡失败" if event.kind == "death" else event.margin
        tile = f" · 第 {event.tile + 1} 块地板" if event.tile >= 0 else ""
        self.notify("counts", (self.misses, self.outputs))
        if not self.armed:
            self.notify("log", "[模拟] " + label + tile)
            return
        if self.settings.duration_ms == 0:
            self.notify("log", "[跳过] 持续时间为 0 · " + label + tile)
            return
        if self.last_trigger is not None and (now - self.last_trigger) * 1000 < self.settings.cooldown_ms:
            self.notify("log", "[冷却] " + label + tile)
            return
        device = self.device.selected()
        if not device or device["slotId"] != self.owned_slot or self.device.client_id != self.owned_client:
            asyncio.create_task(self.disarm("设备连接发生变化"))
            return
        self.last_trigger = now
        previous = self.active_pulse
        self.pulse_sequence += 1
        sequence = self.pulse_sequence
        if previous and not previous.done():
            previous.cancel()
        self.notify("log", "[输出] " + label + tile)
        self.active_pulse = asyncio.create_task(self._pulse(self.generation, sequence, previous))

    async def _pulse(self, generation, sequence, previous=None):
        if previous and not previous.done():
            await asyncio.gather(previous, return_exceptions=True)
        valid = lambda: (self.armed and self.generation == generation
                         and self.pulse_sequence == sequence and self.events.connected())
        if not valid():
            return
        slot = self.owned_slot
        settings = self.settings
        try:
            completed = await self.device.pulse(slot, settings, valid)
            if completed and valid():
                self.outputs += 1
                self.notify("counts", (self.misses, self.outputs))
                self.notify("log", "本次脉冲已完成，APP 临时强度归零。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.armed and self.generation == generation and self.pulse_sequence == sequence:
                await self.disarm("输出未能确认完成：" + str(error))

    def device_changed(self):
        selected = self.device.selected()
        self.notify("device", selected)
        if (self.armed or self.preparing) and (
                not selected or selected["slotId"] != self.owned_slot
                or self.device.client_id != self.owned_client):
            asyncio.create_task(self.disarm("手机或蓝牙设备已断开"))
        if self.armed and selected:
            props = selected.get("props") or {}
            intensity = props.get("intensity" + self.settings.channel)
            if isinstance(intensity, (int, float)) and intensity > self.settings.strength:
                asyncio.create_task(self.disarm("检测到通道强度超过本次设置"))

    async def arm(self):
        generation = self.generation
        async with self.transition_lock:
            if generation == self.generation:
                await self._arm()

    async def _arm(self):
        if self.connecting or self.closing:
            raise RuntimeError("正在更换连接或退出，请等待完成。")
        if self.armed or self.preparing:
            return
        if not self.events.connected():
            raise RuntimeError("先安装监听插件并启动游戏，等游戏状态显示已连接。")
        selected = self.device.selected()
        if not selected:
            raise RuntimeError("请在 DG-LAB 4 APP 中连接并开放一台郊狼 3.0。")
        if self.settings.strength <= 0:
            raise RuntimeError("强度为 0；请先设置强度。")
        self.generation += 1
        generation = self.generation
        self.owned_slot = selected["slotId"]
        self.owned_client = self.device.client_id
        self.preparing = True
        self.notify("armed", "preparing")
        try:
            await self.device.prepare(self.owned_slot, self.settings.channel)
            selected = self.device.selected()
            if generation != self.generation:
                return
            if (not self.events.connected() or not selected or selected["slotId"] != self.owned_slot
                    or self.device.client_id != self.owned_client):
                raise RuntimeError("开启过程中连接已变化，请重新开启。")
            self.armed = True
            self.last_trigger = None
            self.notify("armed", True)
            self.notify("log", "已开启真实输出；按 F8 可紧急停止。")
        except Exception:
            # This lock also covers prepare. A concurrent stop invalidates the
            # generation immediately and will perform its own cleanup next.
            if generation == self.generation:
                self.armed = self.preparing = False
                self.generation += 1
                slot, client = self.owned_slot, self.owned_client
                self.owned_slot = self.owned_client = ""
                if client == self.device.client_id:
                    await self.device.stop_device(slot)
                self.notify("armed", False)
            raise
        finally:
            if generation == self.generation:
                self.preparing = False
                if not self.armed:
                    self.notify("armed", False)

    async def disarm(self, reason="手动停止"):
        had_output = self.armed or self.preparing or bool(self.owned_slot)
        self.armed = self.preparing = False
        self.generation += 1
        slot, client = self.owned_slot, self.owned_client
        self.owned_slot = self.owned_client = ""
        task = self.active_pulse
        self.active_pulse = None
        self.notify("armed", False)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        async with self.transition_lock:
            if task and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            if had_output:
                confirmed = await self.device.stop_device(slot) if client == self.device.client_id else False
                self.notify("log", reason + (" · 已确认清空并归零" if confirmed
                                            else " · 停止指令未确认，请在 APP 或主机上停止"))
            else:
                self.notify("log", reason + " · 当前为模拟模式")

    async def connect_lan(self, host):
        await self._connect(lambda: self.device.start_lan(host))

    async def connect_remote(self, url):
        await self._connect(lambda: self.device.start_remote(url))

    async def _connect(self, start):
        async with self.connection_lock:
            if self.closing:
                return
            self.connecting = True
            self.notify("connecting", True)
            try:
                await self.disarm("更换连接")
                await start()
            except Exception:
                await self.device.disconnect()
                self.notify("connection", "连接失败 · 请检查地址或网络后重试")
                raise
            finally:
                self.connecting = False
                self.notify("connecting", False)

    async def disconnect(self):
        async with self.connection_lock:
            self.connecting = True
            try:
                await self.disarm("断开连接")
                await self.device.disconnect()
                self.notify("connection", "尚未连接")
            finally:
                self.connecting = False

    async def configure(self, settings: Settings):
        self.settings = Settings(**asdict(settings))
        await self.disarm("设置已修改")
        self.notify("settings", self.settings)
        self.notify("log", "触发设置已保存。")

    async def preview(self):
        self.notify("log", "[预览] 模拟一次失误 · " + self.settings.channel + " 通道 / 强度 "
                    + str(self.settings.strength) + " / " + ms_to_seconds(self.settings.duration_ms) + " 秒（仅预览）")

    async def _watchdog(self):
        while True:
            await asyncio.sleep(0.2)
            online = self.events.connected()
            if self.game_online and not online:
                self.game_online = False
                self.notify("game", {"online": False})
            if (self.armed or self.preparing) and not online:
                await self.disarm("游戏心跳丢失")

    async def _shutdown(self):
        self.closing = True
        await self.disarm("软件退出")
        await self.device.disconnect()
        if self.transport:
            self.transport.close()
        if self.watchdog:
            self.watchdog.cancel()

    def close(self):
        if not self.loop or not self.loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            future.result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1)
