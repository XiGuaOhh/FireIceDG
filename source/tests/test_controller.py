import asyncio
import json
import socket
import time
import unittest
from unittest.mock import patch, AsyncMock

import websockets

from fireicedg.config import Settings, seconds_to_ms, ms_to_seconds
from fireicedg.device import DeviceLink, waveform
from fireicedg.engine import Engine, GameReceiver
from fireicedg.events import EventFilter, GameEvent, qualifies

TOKEN = "a" * 64
DEVICE = {"slotId": "test-coyote", "name": "Mock Coyote", "type": "COYOTE_030",
          "props": {"intensityA": 0, "intensityB": 0},
          "slotState": {"hasDevice": True, "channelA": {"intensityMax": 10}}}


async def eventually(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Expected state did not arrive before timeout")
        await asyncio.sleep(0.005)


def packet(sequence, kind="heartbeat", **extra):
    data = {"protocol": "fireicedg/1", "token": TOKEN, "session": "game-1", "seq": sequence,
            "time": int(time.time() * 1000), "kind": kind, "tile": 23, "playing": True, "ready": True}
    data.update(extra)
    return json.dumps(data).encode()


class EventTests(unittest.TestCase):
    def test_invalid_new_session_does_not_retire_valid_game(self):
        events = EventFilter(TOKEN)
        events.parse(packet(1))
        self.assertIsNone(events.parse(packet(1, session="bad-session", tile=[])))
        self.assertIsNotNone(events.parse(packet(2, "death")))
        self.assertEqual(events.session, "game-1")

    def test_stop_invalidates_heartbeat_immediately(self):
        events = EventFilter(TOKEN)
        events.parse(packet(1))
        events.parse(packet(2, "stop"))
        self.assertFalse(events.connected())

    def test_authentication_freshness_and_replay(self):
        events = EventFilter(TOKEN)
        self.assertIsNone(events.parse(packet(1, token="b" * 64)))
        self.assertIsNone(events.parse(packet(1, "death")))
        self.assertIsNotNone(events.parse(packet(1)))
        self.assertIsNone(events.parse(packet(1)))
        self.assertIsNone(events.parse(packet(2, "death", time=int(time.time() * 1000) - 3000)))
        self.assertIsNotNone(events.parse(packet(2, "death")))
        self.assertIsNone(events.parse(packet(3, "hit", margin="Unknown")))
        self.assertIsNone(events.parse(b"[]"))
        self.assertIsNone(events.parse(b"\xff"))
        self.assertIsNone(events.parse(packet(True)))

    def test_new_session_requires_heartbeat_and_retires_old_one(self):
        events = EventFilter(TOKEN)
        events.parse(packet(1))
        self.assertIsNone(events.parse(packet(2, "death", session="new")))
        self.assertIsNotNone(events.parse(packet(1, session="new")))
        self.assertIsNone(events.parse(packet(3)))

    def test_trigger_modes(self):
        event = lambda margin: GameEvent("hit", "g", 1, 0, margin, 23, True, True)
        self.assertFalse(qualifies(event("TooEarly"), "death"))
        self.assertTrue(qualifies(event("TooEarly"), "mistake"))
        self.assertFalse(qualifies(event("EarlyPerfect"), "mistake"))
        self.assertTrue(qualifies(event("EarlyPerfect"), "nonperfect"))
        self.assertFalse(qualifies(event("Perfect"), "nonperfect"))
        self.assertFalse(qualifies(event("Auto"), "nonperfect"))
        self.assertTrue(qualifies(GameEvent("death", "g", 1, 0), "death"))

    def test_limits_reject_invalid_values(self):
        for params in [{"strength": 201}, {"strength": -1}, {"duration_ms": -1},
                       {"cooldown_ms": -1}, {"strength": True}, {"duration_ms": float("inf")},
                       {"cooldown_ms": True}]:
            with self.assertRaises(ValueError):
                Settings(**params)
        self.assertEqual(len(waveform(500)), 5)
        self.assertEqual(len(bytes.fromhex(waveform(500)[0])), 8)

    def test_full_strength_and_independent_unbounded_times(self):
        Settings(strength=200, duration_ms=60000, cooldown_ms=0)
        Settings(duration_ms=1, cooldown_ms=10**1000)
        Settings(duration_ms=10**1000, cooldown_ms=1)
        Settings(duration_ms=0, cooldown_ms=0)

    def test_seconds_parser_supports_long_times_and_zero(self):
        self.assertEqual(seconds_to_ms("60000"), 60000000)
        self.assertEqual(seconds_to_ms("0"), 0)
        self.assertEqual(seconds_to_ms("0.0001"), 1)
        self.assertEqual(seconds_to_ms("1e100"), 10**103)
        for text in ["nan", "inf", "-inf", "-1", "", "hello"]:
            with self.assertRaises(ValueError):
                seconds_to_ms(text)

    def test_huge_time_formats_and_roundtrips_without_float_overflow(self):
        value = 10**1000 + 123
        self.assertEqual(seconds_to_ms(ms_to_seconds(value)), value)
        self.assertEqual(ms_to_seconds(500), "0.5")
        self.assertEqual(ms_to_seconds(0), "0")


class MockApp:
    """Speaks the documented APP protocol, including delayed task responses."""
    def __init__(self):
        self.commands = []
        self.pending = {}
        self.requests = {}
        self.ws = None
        self.reader = None

    async def connect(self, link, port):
        self.ws = await websockets.connect(f"ws://127.0.0.1:{port}/v4?tid={link.controller}")
        self.assert_hello = json.loads(await self.ws.recv())
        await self.ws.recv()
        await self.send({"t": "ev", "ev": "devices.snapshot", "devices": [DEVICE]})
        self.reader = asyncio.create_task(self.receive())
        await asyncio.sleep(0.03)

    async def send(self, data):
        await self.ws.send(json.dumps({"type": "message", "data": data}))

    async def respond(self, request, result):
        await self.send({"t": "resp", "reqId": request["reqId"], "result": result})

    async def finish(self, request):
        await asyncio.sleep(request["data"]["d"] / 1000)
        await self.respond(request, {"reason": "completed", "type": request["data"]["t"]})

    async def receive(self):
        try:
            async for raw in self.ws:
                frame = json.loads(raw)
                if frame.get("type") != "message":
                    continue
                request = frame["data"]
                self.commands.append((time.monotonic(), request))
                method = request["m"]
                if method == "device.op" and request["data"]["t"] in (0, 4):
                    if request["data"].get("im"):
                        for key, old in list(self.requests.items()):
                            if not self.pending[key].done() and all(old["data"].get(field) == request["data"].get(field)
                                                                    for field in ("s", "c", "t", "p")):
                                self.pending[key].cancel()
                                await self.respond(old, {"reason": "replaced", "type": old["data"]["t"]})
                    self.requests[request["reqId"]] = request
                    self.pending[request["reqId"]] = asyncio.create_task(self.finish(request))
                elif method == "device.op.clear":
                    for key, task in self.pending.items():
                        if not task.done():
                            task.cancel()
                            await self.respond(self.requests[key], {"reason": "cleared", "type": self.requests[key]["data"]["t"]})
                    await self.respond(request, {})
                else:
                    await self.respond(request, {})
        except websockets.ConnectionClosed:
            pass

    async def close(self):
        for task in self.pending.values():
            task.cancel()
        if self.ws:
            await self.ws.close()
        if self.reader:
            await self.reader
        await asyncio.gather(*self.pending.values(), return_exceptions=True)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_remote_handshake_closes_socket_and_clears_qr(self):
        peer = AsyncMock()
        peer.recv.return_value = "[]"
        with patch("fireicedg.device.websockets.connect", AsyncMock(return_value=peer)):
            with self.assertRaises(ValueError):
                await self.link.start_remote("wss://example.invalid/v4")
        peer.close.assert_awaited()
        self.assertIsNone(self.link.ws)
        self.assertFalse(self.link.connected)
        self.assertIn(("qr", ""), self.messages)

    async def test_malformed_updates_preserve_device_and_receive_next_message(self):
        for data in [
            {"t": "ev", "ev": "devices.snapshot", "devices": None},
            {"t": "ev", "ev": "devices.patch", "removed": ["test-coyote"], "added": None},
            {"t": "ev", "ev": "slots.patch", "slots": [5]},
            {"t": "resp", "reqId": [], "requestId": [1]},
        ]:
            await self.link._incoming(json.dumps({"type": "message", "data": data}))
            self.assertIsNotNone(self.link.selected())
        await self.app.send({"t": "ev", "ev": "slots.patch", "slots": [
            {"slotId": "test-coyote", "props": {"intensityA": 3}}]})
        await eventually(lambda: self.link.selected()["props"]["intensityA"] == 3)

    async def test_unexpected_task_end_is_not_silently_accepted(self):
        with patch.object(self.link, "rpc", AsyncMock(return_value={"reason": "cleared"})):
            with self.assertRaises(RuntimeError):
                await self.link.pulse("test-coyote", Settings(), lambda: True)

    async def asyncSetUp(self):
        self.messages = []
        self.link = DeviceLink(lambda k, v: self.messages.append((k, v)), lambda: None)
        self.port = await self.link.start_lan("127.0.0.1", port=0, bind_host="127.0.0.1")
        self.app = MockApp()
        await self.app.connect(self.link, self.port)

    async def asyncTearDown(self):
        await self.app.close()
        await self.link.disconnect()

    async def test_pairing_is_read_only_and_rejects_wrong_peer(self):
        self.assertEqual(self.app.commands, [])
        self.assertEqual(self.link.selected()["slotId"], "test-coyote")
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/v4?tid=wrong") as ws:
            with self.assertRaises(websockets.ConnectionClosed):
                await ws.recv()

    async def test_intensity_and_wave_start_together_and_are_bounded(self):
        await self.link.prepare("test-coyote", "A")
        result = await self.link.pulse("test-coyote", Settings(strength=20, duration_ms=300), lambda: True)
        self.assertTrue(result)
        operations = [(when, req["data"]) for when, req in self.app.commands
                      if req["m"] == "device.op" and req["data"]["t"] in (0, 4)]
        self.assertEqual(len(operations), 2)
        self.assertLess(abs(operations[0][0] - operations[1][0]), 0.05)
        intensity = next(data for _, data in operations if data["t"] == 4)
        pulse = next(data for _, data in operations if data["t"] == 0)
        self.assertEqual(intensity["v"], 10)  # APP's smaller limit wins.
        self.assertEqual(intensity["d"], 300)
        self.assertEqual(len(pulse["v"]), 3)
        self.assertTrue(pulse["im"])
        self.assertFalse(self.link.pending)

    async def test_generation_guard_prevents_output_after_stop(self):
        await self.link.pulse("test-coyote", Settings(), lambda: False)
        self.assertEqual(self.app.commands, [])

    async def test_zero_duration_does_not_send_device_commands(self):
        result = await self.link.pulse("test-coyote", Settings(duration_ms=0), lambda: True)
        self.assertFalse(result)
        self.assertEqual(self.app.commands, [])

    async def test_long_duration_streams_small_transmission_chunks(self):
        with patch("fireicedg.device.PULSE_CHUNK_MS", 100):
            result = await self.link.pulse("test-coyote", Settings(duration_ms=350, cooldown_ms=0), lambda: True)
        self.assertTrue(result)
        waves = [req["data"] for _, req in self.app.commands if req["m"] == "device.op" and req["data"]["t"] == 0]
        self.assertGreaterEqual(len(waves), 3)
        self.assertTrue(all(0 < wave["d"] <= 100 and len(wave["v"]) == 1 for wave in waves))
        self.assertFalse(self.link.pending)

    async def test_huge_duration_starts_without_a_huge_packet_and_can_be_stopped(self):
        pulse = asyncio.create_task(self.link.pulse("test-coyote", Settings(duration_ms=10**1000, cooldown_ms=0), lambda: True))
        await eventually(lambda: len(self.app.commands) >= 2)
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        self.assertTrue(await self.link.stop_device("test-coyote"))
        wave = next(req["data"] for _, req in self.app.commands if req["m"] == "device.op" and req["data"]["t"] == 0)
        self.assertLessEqual(len(wave["v"]), 10)
        self.assertLessEqual(wave["d"], 1000)
        self.assertFalse(self.link.pending)

    async def test_strength_200_is_transmitted_when_app_limit_allows_it(self):
        await self.app.send({"t": "ev", "ev": "slots.patch", "slots": [
            {"slotId": "test-coyote", "slotState": {"channelA": {"intensityMax": 200}}}]})
        await eventually(lambda: self.link.devices["test-coyote"]["slotState"]["channelA"]["intensityMax"] == 200)
        self.assertTrue(await self.link.pulse("test-coyote", Settings(strength=200, duration_ms=100), lambda: True))
        intensity = next(req["data"] for _, req in self.app.commands if req["m"] == "device.op" and req["data"]["t"] == 4)
        self.assertEqual(intensity["v"], 200)

    async def test_device_removal_and_disconnect(self):
        await self.app.send({"t": "ev", "ev": "slots.patch",
                             "slots": [{"slotId": "test-coyote", "slotState": {"hasDevice": False}}]})
        await eventually(lambda: self.link.selected() is None)
        self.assertIsNone(self.link.selected())
        await self.app.ws.close()
        await eventually(lambda: not self.link.devices)
        self.assertEqual(self.link.devices, {})


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_connection_clears_busy_state_and_old_qr(self):
        with patch.object(self.engine.device, "start_remote", AsyncMock(side_effect=OSError("offline"))):
            with self.assertRaises(OSError):
                await self.engine.connect_remote("wss://example.invalid/v4")
        self.assertFalse(self.engine.connecting)
        self.assertIsNone(self.engine.device.ws)
        messages = []
        while not self.engine.messages.empty():
            messages.append(self.engine.messages.get_nowait())
        self.assertIn(("qr", ""), messages)
        self.assertIn(("connecting", False), messages)

    async def test_stop_during_prepare_does_not_arm_later(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.device.prepare
        async def delayed_prepare(slot, channel):
            entered.set()
            await release.wait()
            await original(slot, channel)
        with patch.object(self.engine.device, "prepare", delayed_prepare):
            arm = asyncio.create_task(self.engine.arm())
            await entered.wait()
            stop = asyncio.create_task(self.engine.disarm())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(arm, stop)
        self.assertFalse(self.engine.armed)
        self.assertFalse(self.engine.preparing)
        await self.emit("death")
        self.assertFalse(self.outputs())

    async def test_rearm_waits_until_previous_stop_finishes(self):
        await self.arm()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.device.stop_device
        async def delayed_stop(slot):
            entered.set()
            await release.wait()
            return await original(slot)
        with patch.object(self.engine.device, "stop_device", delayed_stop):
            stop = asyncio.create_task(self.engine.disarm())
            await entered.wait()
            rearm = asyncio.create_task(self.engine.arm())
            await asyncio.sleep(0.05)
            try:
                self.assertFalse(self.engine.armed)
                self.assertFalse(rearm.done())
            finally:
                release.set()
                await asyncio.gather(stop, rearm)
        self.assertTrue(self.engine.armed)

    async def test_connection_changes_do_not_overlap(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def connect(url):
            calls.append(url)
            entered.set()
            await release.wait()
        with patch.object(self.engine.device, "start_remote", connect):
            first = asyncio.create_task(self.engine.connect_remote("first"))
            await entered.wait()
            second = asyncio.create_task(self.engine.connect_remote("second"))
            await asyncio.sleep(0.03)
            try:
                self.assertEqual(calls, ["first"])
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertEqual(calls, ["first", "second"])

    async def asyncSetUp(self):
        self.engine = Engine(TOKEN, Settings(mode="mistake", duration_ms=300, cooldown_ms=1000), game_port=0)
        self.engine.loop = asyncio.get_running_loop()
        await self.engine._initialize()
        port = await self.engine.device.start_lan("127.0.0.1", port=0, bind_host="127.0.0.1")
        self.app = MockApp()
        await self.app.connect(self.engine.device, port)
        self.receiver = GameReceiver(self.engine)
        self.sequence = 0
        await self.emit()

    async def asyncTearDown(self):
        await self.engine._shutdown()
        await self.app.close()

    async def emit(self, kind="heartbeat", **extra):
        self.sequence += 1
        # Exercise the real datagram handler deterministically. OS loopback
        # delivery is checked separately by SocketTests, without hiding failure.
        self.receiver.datagram_received(packet(self.sequence, kind, **extra), ("127.0.0.1", 19999))
        await eventually(lambda: self.engine.events.last_sequence == self.sequence)
        await asyncio.sleep(0.01)

    async def arm(self):
        await self.engine.arm()
        self.engine.last_trigger = -1000

    def outputs(self):
        return [req for _, req in self.app.commands if req["m"] == "device.op" and req["data"]["t"] == 4]

    async def test_default_simulation_and_preview_never_send_output(self):
        await self.emit("death")
        await self.emit("hit", margin="TooEarly")
        await self.engine.preview()
        self.assertEqual(self.engine.misses, 2)
        self.assertEqual(self.outputs(), [])
        self.assertEqual(self.app.commands, [])

    async def test_duplicate_failure_and_cooldown_do_not_stack(self):
        await self.arm()
        await self.emit("death")
        await self.emit("hit", margin="FailMiss")
        await self.emit("hit", margin="TooEarly")
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.outputs()), 1)
        self.assertEqual(self.engine.outputs, 1)
        self.assertEqual(self.engine.misses, 2)

    async def test_emergency_stop_cancels_jobs_and_resets_both_channels(self):
        await self.arm()
        await self.emit("death")
        await eventually(lambda: len(self.outputs()) == 1)
        await self.engine.disarm("test emergency")
        await self.emit("hit", margin="TooLate")
        self.assertFalse(self.engine.armed)
        self.assertEqual(len(self.outputs()), 1)
        resets = [req["data"]["c"] for _, req in self.app.commands
                  if req["m"] == "device.op" and req["data"]["t"] == 7]
        self.assertIn(0, resets)
        self.assertIn(1, resets)
        self.assertFalse(self.engine.device.pending)

    async def test_heartbeat_loss_disarms(self):
        await self.arm()
        self.engine.events.last_heartbeat = time.monotonic() - 5
        await asyncio.sleep(0.3)
        self.assertFalse(self.engine.armed)

    async def test_device_loss_disarms_and_reconnect_never_rearms(self):
        await self.arm()
        await self.app.send({"t": "ev", "ev": "devices.snapshot", "devices": []})
        await asyncio.sleep(0.06)
        self.assertFalse(self.engine.armed)
        await self.app.send({"t": "ev", "ev": "devices.snapshot", "devices": [DEVICE]})
        await asyncio.sleep(0.03)
        await self.emit("death")
        self.assertEqual(self.outputs(), [])

    async def test_unknown_game_interface_refuses_arming(self):
        self.engine.events.ready = False
        with self.assertRaises(RuntimeError):
            await self.engine.arm()
        self.assertEqual(self.outputs(), [])

    async def test_game_restart_requires_manual_rearm(self):
        await self.arm()
        await self.emit(session="game-2")
        await eventually(lambda: not self.engine.armed)
        self.assertEqual(self.outputs(), [])

    async def test_app_emergency_does_not_depend_on_gui_polling(self):
        await self.arm()
        await self.app.send({"t": "ev", "ev": "custom.action", "action": 0})
        await eventually(lambda: not self.engine.armed)
        self.assertEqual(self.outputs(), [])

    async def test_no_output_when_game_is_not_playing(self):
        await self.arm()
        await self.emit("hit", margin="TooEarly", playing=False)
        self.assertEqual(self.outputs(), [])

    async def test_shorter_cooldown_retriggers_before_previous_duration_ends(self):
        self.engine.settings = Settings(mode="mistake", duration_ms=300, cooldown_ms=30)
        await self.arm()
        await self.emit("hit", margin="TooEarly")
        await eventually(lambda: len(self.outputs()) == 1)
        await asyncio.sleep(0.05)
        await self.emit("hit", margin="TooLate")
        await eventually(lambda: len(self.outputs()) == 2)
        await asyncio.sleep(0.12)
        self.assertTrue(self.engine.armed)
        self.assertEqual(self.engine.outputs, 0)
        await eventually(lambda: self.engine.outputs == 1)
        self.assertFalse(self.engine.device.pending)

    async def test_zero_cooldown_allows_immediate_retrigger_then_stop(self):
        self.engine.settings = Settings(mode="mistake", duration_ms=300, cooldown_ms=0)
        await self.arm()
        await self.emit("hit", margin="TooEarly")
        await eventually(lambda: len(self.outputs()) == 1)
        await self.emit("hit", margin="TooLate")
        await eventually(lambda: len(self.outputs()) == 2)
        await self.engine.disarm("emergency after replacement")
        self.assertFalse(self.engine.armed)
        self.assertFalse(self.engine.device.pending)

    async def test_zero_duration_suppresses_real_output(self):
        self.engine.settings = Settings(mode="mistake", duration_ms=0, cooldown_ms=0)
        await self.arm()
        await self.emit("hit", margin="TooEarly")
        self.assertEqual(self.outputs(), [])

    async def test_large_cooldown_does_not_delay_first_trigger(self):
        self.engine.settings = Settings(mode="mistake", duration_ms=100, cooldown_ms=10**1000)
        await self.engine.arm()
        await self.emit("hit", margin="TooEarly")
        await eventually(lambda: len(self.outputs()) == 1)
        await self.emit("hit", margin="TooLate")
        self.assertEqual(len(self.outputs()), 1)

    async def test_zero_cooldown_does_not_drop_distinct_deaths_on_same_tile(self):
        self.engine.settings = Settings(mode="death", duration_ms=300, cooldown_ms=0)
        await self.arm()
        await self.emit("death")
        await eventually(lambda: len(self.outputs()) == 1)
        await self.emit("death")
        await eventually(lambda: len(self.outputs()) == 2)

    async def test_huge_duration_preview_does_not_overflow_or_send_commands(self):
        self.engine.settings = Settings(duration_ms=10**1000, cooldown_ms=0)
        await self.engine.preview()
        self.assertEqual(self.app.commands, [])


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_os_udp_loopback_delivery(self):
        received = asyncio.Event()
        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                if data == b"fireicedg-loopback-check":
                    received.set()
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Receiver, local_addr=("127.0.0.1", 0))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"fireicedg-loopback-check", transport.get_extra_info("sockname"))
            try:
                await asyncio.wait_for(received.wait(), 0.5)
            except asyncio.TimeoutError:
                self.skipTest("OS UDP loopback unavailable; game network delivery is NOT verified")
        finally:
            sender.close()
            transport.close()


if __name__ == "__main__":
    unittest.main()
