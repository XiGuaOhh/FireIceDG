from __future__ import annotations

import asyncio
import copy
import json
import math
import time
import uuid
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import websockets

from .config import MAX_STRENGTH, RELAY_PORT, Settings

PULSE_CHUNK_MS = 1000


def channel_limit(device, channel):
    state = (device or {}).get("slotState") or {}
    value = (state.get("channel" + channel) or {}).get("intensityMax")
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return max(0, min(MAX_STRENGTH, int(value)))


def validate_device(device):
    if not isinstance(device, dict) or not isinstance(device.get("slotId"), str):
        raise ValueError("无效设备")
    for key in ("slotState", "props"):
        if key in device and not isinstance(device[key], dict):
            raise ValueError("无效设备状态")
    for channel in ("A", "B"):
        state = device.get("slotState", {}).get("channel" + channel, {})
        if not isinstance(state, dict):
            raise ValueError("无效通道状态")
        if "intensityMax" in state and (type(state["intensityMax"]) not in (int, float)
                                         or not math.isfinite(state["intensityMax"])):
            raise ValueError("无效通道上限")
    return device


def device_list(data, key):
    values = data.get(key, [])
    if not isinstance(values, list):
        raise ValueError("无效设备列表")
    return [validate_device(item) for item in values]


def merge(current, patch):
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(current)
    for key, value in patch.items():
        result[key] = merge(current.get(key), value)
    return result


def pairing_link(url: str, controller: str) -> str:
    parts = urlsplit(url)
    query = (parts.query + "&" if parts.query else "") + "tid=" + quote(controller, safe="")
    socket_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return "https://dungeon-lab.cn/s/?v=1&action=socket&url=" + quote(socket_url, safe="")


def waveform(duration_ms: int) -> list[str]:
    if type(duration_ms) is not int or not 0 <= duration_ms <= PULSE_CHUNK_MS:
        raise ValueError("波形数据需按传输分段生成")
    # Each frame is 100 ms: four frequency bytes and four amplitude percentages.
    # These frames are generated here rather than copied from a waveform library.
    return ["2828282832323232"] * math.ceil(duration_ms / 100)


class DeviceLink:
    """DG-LAB 4 APP V4 transport, locally hosted or through a WSS relay."""

    def __init__(self, notify, changed):
        self.notify = notify
        self.changed = changed
        self.server = None
        self.ws = None
        self.receiver = None
        self.keepalive = None
        self.controller = ""
        self.client_id = ""
        self.devices = {}
        self.remote_clients = set()
        self.pending = {}
        self.mode = ""
        self.connected = False
        self.last_pong = time.monotonic()
        self.stopping = False
        self.rpc_timeout = 2.0

    def selected(self) -> dict | None:
        # Exactly one connected Coyote 3.0 is supported; do not silently pick
        # another device when the selected one disconnects.
        if self.mode == "remote" and len(self.remote_clients) != 1:
            return None
        devices = [d for d in self.devices.values()
                   if d.get("type") == "COYOTE_030" and isinstance(d.get("slotState"), dict)
                   and d["slotState"].get("hasDevice") is True and isinstance(d.get("props", {}), dict)]
        return devices[0] if len(devices) == 1 else None

    async def start_lan(self, host: str, port: int = RELAY_PORT, bind_host: str = "0.0.0.0"):
        await self.disconnect()
        self.mode = "lan"
        self.stopping = False
        self.controller = uuid.uuid4().hex
        self.server = await websockets.serve(self._app_connection, bind_host, port,
                                            ping_interval=2, ping_timeout=3, max_size=131072,
                                            max_queue=16, compression=None, close_timeout=1)
        actual_port = self.server.sockets[0].getsockname()[1]
        self.connected = True
        url = f"ws://{host}:{actual_port}/v4"
        self.notify("qr", pairing_link(url, self.controller))
        self.notify("connection", "等待手机扫码 · " + host)
        self.changed()
        return actual_port

    async def _app_connection(self, ws, path):
        token = parse_qs(urlsplit(path).query).get("tid", [""])[0]
        if token != self.controller or self.ws is not None:
            await ws.close(4001, "Invalid pairing or already paired")
            return
        self.ws = ws
        self.client_id = uuid.uuid4().hex
        try:
            await ws.send(json.dumps({"type": "hello", "clientId": self.client_id}))
            await ws.send(json.dumps({"type": "controller_attached", "clientId": self.controller}))
            self.notify("connection", "APP 已连接 · 等待蓝牙设备")
            self.changed()
            async for raw in ws:
                await self._incoming(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.client_id = ""
                self.devices.clear()
                self._reject_pending("手机连接已断开")
                self.notify("connection", "手机已断开 · 重新扫码连接")
                self.changed()

    async def start_remote(self, url: str):
        await self.disconnect()
        parts = urlsplit(url)
        if parts.scheme != "wss" or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("公网中继请填写不含查询参数的 wss:// 地址")
        self.mode = "remote"
        self.stopping = False
        self.notify("connection", "正在连接公网中继…")
        self.ws = await websockets.connect(url, open_timeout=8, ping_interval=2, ping_timeout=3,
                                           close_timeout=1, max_size=131072, max_queue=16,
                                           compression=None)
        try:
            hello = json.loads(await asyncio.wait_for(self.ws.recv(), 8))
            if (not isinstance(hello, dict) or hello.get("type") != "hello"
                    or not isinstance(hello.get("clientId"), str) or not hello["clientId"]):
                raise ValueError("中继不是 DG-LAB V4 协议")
        except BaseException:
            await self.disconnect()
            raise
        self.controller = hello["clientId"]
        self.connected = True
        self.last_pong = time.monotonic()
        self.notify("qr", pairing_link(url, self.controller))
        self.notify("connection", "中继已连接 · 等待手机扫码")
        self.receiver = asyncio.create_task(self._remote_receive())
        self.keepalive = asyncio.create_task(self._remote_ping())
        self.changed()

    async def _remote_receive(self):
        try:
            async for raw in self.ws:
                await self._incoming(raw)
        except (websockets.ConnectionClosed, AttributeError):
            pass
        finally:
            self.connected = False
            self.ws = None
            self.client_id = ""
            self.devices.clear()
            self.remote_clients.clear()
            self._reject_pending("中继连接已断开")
            self.notify("qr", "")
            self.notify("connection", "中继已断开 · 输出已关闭")
            self.changed()

    async def _remote_ping(self):
        try:
            while self.connected:
                await asyncio.sleep(2)
                if time.monotonic() - self.last_pong > 6:
                    await self.disconnect()
                    return
                if self.ws:
                    await self.ws.send('{"type":"ping"}')
        except (websockets.ConnectionClosed, AttributeError):
            pass

    async def _incoming(self, raw):
        try:
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                return
            kind = frame.get("type")
            if kind == "ping":
                await self.ws.send('{"type":"pong"}')
            elif kind == "pong":
                self.last_pong = time.monotonic()
            elif kind == "client_attached" and self.mode == "remote":
                if not isinstance(frame.get("clientId"), str) or not frame["clientId"]:
                    raise ValueError("无效手机 ID")
                self.remote_clients.add(frame.get("clientId"))
                if self.client_id and self.client_id != frame.get("clientId"):
                    self.notify("log", "检测到多个手机，已关闭输出；请只保留一个连接。")
                    self.devices.clear()
                    self.changed()
                    return
                self.client_id = frame.get("clientId", "")
                self.notify("connection", "APP 已连接 · 等待蓝牙设备")
            elif kind == "client_disconnected":
                self.remote_clients.discard(frame.get("clientId"))
                if frame.get("clientId") == self.client_id:
                    self.client_id = ""
                    self.devices.clear()
                    self._reject_pending("手机连接已断开")
                self.changed()
            elif kind == "error":
                self.notify("log", "中继错误：" + str(frame.get("message", frame.get("code"))))
                self.devices.clear()
                self._reject_pending("中继拒绝了指令")
                self.changed()
            elif kind == "message":
                if self.mode == "remote" and frame.get("clientId") != self.client_id:
                    return
                self._app_data(frame.get("data"))
        except (ValueError, TypeError, AttributeError):
            self.notify("log", "忽略了格式不正确的 APP 消息")

    def _app_data(self, data):
        if not isinstance(data, dict):
            return
        if data.get("t") == "resp":
            request_id = data.get("reqId") or data.get("requestId")
            if not isinstance(request_id, str):
                return
            future = self.pending.get(request_id)
            if future and not future.done():
                if "error" in data:
                    future.set_exception(RuntimeError(str(data["error"])))
                else:
                    future.set_result(data.get("result", {}))
            return
        if data.get("t") != "ev":
            return
        event = data.get("ev")
        updated = dict(self.devices)
        if event == "devices.snapshot":
            updated = {d["slotId"]: d for d in device_list(data, "devices")}
        elif event == "devices.patch":
            removed = data.get("removed", [])
            if not isinstance(removed, list) or any(not isinstance(slot, str) for slot in removed):
                raise ValueError("无效设备列表")
            for slot in removed:
                updated.pop(slot, None)
            for device in device_list(data, "added"):
                updated[device["slotId"]] = device
        elif event == "slots.patch":
            for slot in device_list(data, "slots"):
                slot_id = slot.get("slotId")
                if slot_id in updated:
                    updated[slot_id] = validate_device(merge(updated[slot_id], slot))
        elif event == "custom.action" and data.get("action") == 0:
            self.notify("remote_stop", None)
        self.devices = updated
        self.changed()

    async def rpc(self, method: str, data=None, timeout=None):
        if not self.ws or not self.client_id:
            raise RuntimeError("手机尚未连接")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        payload = {"t": "req", "reqId": request_id, "requestId": request_id, "m": method}
        if data is not None:
            payload["data"] = data
        frame = {"type": "message", "data": payload}
        if self.mode == "remote":
            frame["clientId"] = self.client_id
        try:
            await self.ws.send(json.dumps(frame, separators=(",", ":")))
            return await asyncio.wait_for(future, timeout if timeout is not None else self.rpc_timeout)
        finally:
            self.pending.pop(request_id, None)

    async def prepare(self, slot: str, channel: str):
        await self.rpc("ping")
        await self.rpc("device.op.clear", {"s": slot, "c": int(channel == "B")})
        await self.rpc("device.op", {"s": slot, "c": int(channel == "B"), "t": 7, "v": 0})

    async def pulse(self, slot: str, settings: Settings, generation_valid):
        settings = Settings(**settings.__dict__)
        if not 0 < settings.strength <= MAX_STRENGTH or settings.duration_ms == 0 or not generation_valid():
            return False
        channel = int(settings.channel == "B")
        # Long durations use bounded transmission chunks rather than allocating
        # one enormous frame array or passing huge timers to the phone APP.
        # The configured total duration has no software upper bound.
        deadline = time.monotonic_ns() + settings.duration_ms * 1_000_000
        while True:
            if not generation_valid():
                return False
            remaining_ms = (deadline - time.monotonic_ns() + 999_999) // 1_000_000
            if remaining_ms <= 0:
                return True
            duration_ms = min(PULSE_CHUNK_MS, remaining_ms)
            device = self.devices.get(slot, {})
            maximum = channel_limit(device, settings.channel)
            strength = settings.strength
            if maximum is not None:
                strength = min(strength, maximum)
            if strength == 0:
                raise RuntimeError("APP 的通道强度上限为 0")
            base = {"s": slot, "c": channel, "p": 1, "im": True, "d": duration_ms}
            # V4 responds after task completion. Intensity and waveform must be
            # submitted together; im=true replaces previous tasks, never stacks.
            jobs = [asyncio.create_task(self.rpc("device.op", {**base, "t": 4, "v": strength}, duration_ms / 1000 + 2)),
                    asyncio.create_task(self.rpc("device.op", {**base, "t": 0, "v": waveform(duration_ms), "ver": 3}, duration_ms / 1000 + 2))]
            try:
                results = await asyncio.gather(*jobs)
                if any(isinstance(r, dict) and r.get("reason") not in {None, "completed"} for r in results):
                    raise RuntimeError("APP 提前终止了脉冲，请检查 APP 后重新开启。")
            finally:
                for task in jobs:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def stop_device(self, slot: str):
        if not self.ws or not self.client_id:
            return False
        results = await asyncio.gather(
            self.rpc("device.op.clear", {"s": slot}, timeout=1.0),
            self.rpc("device.op", {"s": slot, "c": 0, "t": 7, "v": 0}, timeout=1.0),
            self.rpc("device.op", {"s": slot, "c": 1, "t": 7, "v": 0}, timeout=1.0),
            return_exceptions=True)
        return not any(isinstance(r, BaseException) for r in results)

    def _reject_pending(self, reason):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))

    async def disconnect(self):
        self.stopping = True
        self.connected = False
        self.notify("qr", "")
        current = asyncio.current_task()
        for task in (self.receiver, self.keepalive):
            if task and task is not current and not task.done():
                task.cancel()
        if self.ws:
            await self.ws.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await asyncio.gather(*(task for task in (self.receiver, self.keepalive)
                               if task and task is not current), return_exceptions=True)
        self.server = self.ws = self.receiver = self.keepalive = None
        self.devices.clear()
        self.remote_clients.clear()
        self.client_id = self.controller = ""
        self._reject_pending("设备连接已关闭")
        self.changed()
