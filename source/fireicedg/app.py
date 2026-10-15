from __future__ import annotations

import argparse
import csv
import ctypes
import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import socket
import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import qrcode

from . import __version__, installer
from .config import MAX_STRENGTH, Settings, data_directory, load_preferences, save_preferences, seconds_to_ms, ms_to_seconds
from .engine import Engine
from .device import channel_limit

BG = "#0D1119"
PANEL = "#151C28"
CARD = "#1C2636"
TEXT = "#E9EEF8"
MUTED = "#899BB7"
FIRE = "#FF8068"
ICE = "#79C9F5"
GREEN = "#7BDFC1"
RED = "#DC5363"
MODES = {"死亡 / 关卡失败": "death", "过早、过晚、漏按或多按": "mistake", "所有非纯完美判定": "nonperfect"}


def local_addresses():
    addresses = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("8.8.8.8", 80))
            addresses.append(route.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.extend(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return list(dict.fromkeys(ip for ip in addresses if not ip.startswith(("127.", "169.254.")))) or ["127.0.0.1"]


class EmergencyHotkey:
    def __init__(self, callback):
        self.callback = callback
        self.thread_id = None
        self.success = False
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.ready.wait(1)

    def run(self):
        if os.name != "nt":
            self.ready.set()
            return
        import ctypes.wintypes as w
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.success = bool(user.RegisterHotKey(None, 1, 0x4000, 0x77))
        self.ready.set()
        if not self.success:
            return
        message = w.MSG()
        try:
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    self.callback()
        finally:
            user.UnregisterHotKey(None, 1)

    def close(self):
        if self.success and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            self.thread.join(timeout=1)


class App(ctk.CTk):
    def __init__(self, selfcheck=False):
        super().__init__()
        self.title("FireIceDG · 冰与火之舞 × 郊狼")
        self.geometry("1120x920")
        self.minsize(1050, 880)
        self.configure(fg_color=BG)
        self.profile = data_directory()
        self.preference_path = self.profile / "preferences.json"
        self.preferences = load_preferences(self.preference_path)
        self.settings = Settings(**self.preferences["settings"])
        self.engine = Engine(self.preferences["game_token"], self.settings)
        self.ui_messages = queue.Queue()
        self.log_entries = []
        self.qr_link = ""
        self.qr_image = None
        self.device_info = None
        self.armed = False
        self.is_closing = False
        self.selfcheck = selfcheck
        self.logger = logging.getLogger("fireicedg")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = RotatingFileHandler(self.profile / "events.log", maxBytes=500000,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.logger.addHandler(handler)
        self._build()
        try:
            self.engine.start()
        except Exception:
            self.destroy()
            raise
        self.hotkey = EmergencyHotkey(lambda: self.engine.submit(self.engine.disarm("F8 紧急停止")))
        if not self.hotkey.success:
            self.hotkey_label.configure(text="F8 未注册成功 · 请使用停止按钮", text_color=FIRE)
        self.bind("<F8>", lambda event: self.stop())
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(80, self.poll)
        self.after(200, self._discover)

    def label(self, parent, text, size=14, color=TEXT, bold=False, **kwargs):
        kwargs.setdefault("height", max(20, size + 3))
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(family="Microsoft YaHei UI", size=size,
                                            weight="bold" if bold else "normal"), **kwargs)

    def button(self, parent, text, command, color=ICE, **kwargs):
        return ctk.CTkButton(parent, text=text, command=command, fg_color=color,
                            hover_color="#A0DCFC" if color == ICE else "#D76B58",
                            text_color=BG, corner_radius=9, height=36,
                            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"), **kwargs)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=26, pady=(22, 12), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        logo = tk.Canvas(header, width=70, height=64, bg=BG, highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        logo.create_oval(9, 6, 61, 58, outline="#33435C", width=2)
        logo.create_oval(7, 10, 31, 34, fill=FIRE, outline="")
        logo.create_oval(38, 35, 62, 59, fill=ICE, outline="")
        self.label(header, "冰与火之舞 × 郊狼", size=27, bold=True, anchor="w").grid(row=0, column=1, sticky="w")
        self.label(header, "FireIceDG  /  失误监听与脉冲联动", color=MUTED, anchor="w").grid(row=1, column=1, sticky="w")
        self.label(header, "v" + __version__, size=12, color=MUTED).grid(row=0, column=2, sticky="e")
        statuses = ctk.CTkFrame(self, fg_color="transparent")
        statuses.grid(row=1, column=0, padx=26, pady=(0, 16), sticky="ew")
        statuses.grid_columnconfigure((0, 1, 2), weight=1)
        self.game_status = self._status(statuses, 0, "01  游戏监听", "等待游戏", ICE)
        self.device_status = self._status(statuses, 1, "02  郊狼 3.0", "等待设备", GREEN)
        self.mode_status = self._status(statuses, 2, "03  输出状态", "模拟模式", FIRE)
        self.tabs = ctk.CTkTabview(self, fg_color=PANEL, corner_radius=14,
                                  segmented_button_fg_color=CARD, segmented_button_selected_color="#30476A",
                                  segmented_button_selected_hover_color="#3C577F",
                                  segmented_button_unselected_color=CARD,
                                  text_color=TEXT, height=660)
        self.tabs.grid(row=2, column=0, padx=26, pady=(0, 14), sticky="nsew")
        self.dashboard = self.tabs.add("控制台")
        self.install_tab = self.tabs.add("游戏插件")
        self.help_tab = self.tabs.add("使用说明")
        self._dashboard()
        self._install_page()
        self._help_page()
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, padx=28, pady=(0, 18), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.hotkey_label = self.label(footer, "F8  紧急停止（全局快捷键）", size=12, color=MUTED)
        self.hotkey_label.grid(row=0, column=0, sticky="w")
        self.label(footer, "Windows 64 位 · 郊狼 3.0 · DG-LAB 4 APP", size=12, color=MUTED).grid(row=0, column=1)

    def _status(self, parent, column, caption, value, accent):
        frame = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=12)
        frame.grid(row=0, column=column, padx=(0 if column == 0 else 8, 0 if column == 2 else 8), sticky="ew")
        self.label(frame, caption, size=12, color=MUTED).pack(anchor="w", padx=18, pady=(13, 4))
        widget = self.label(frame, value, size=21, color=accent, bold=True)
        widget.pack(anchor="w", padx=18, pady=(0, 13))
        return widget

    def _dashboard(self):
        page = self.dashboard
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, minsize=305)
        page.grid_rowconfigure(1, weight=1)
        controls = ctk.CTkFrame(page, fg_color="transparent")
        controls.grid(row=0, column=0, padx=(14, 18), pady=12, sticky="nsew")
        controls.grid_columnconfigure((0, 1), weight=1)
        self.label(controls, "触发设置", size=20, bold=True).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        self.label(controls, "何时触发", color=MUTED).grid(row=1, column=0, sticky="w")
        self.mode_menu = ctk.CTkOptionMenu(controls, values=list(MODES), fg_color=CARD,
                                          button_color="#30476A", text_color=TEXT, height=34, width=270)
        self.mode_menu.set(next(label for label, value in MODES.items() if value == self.settings.mode))
        self.mode_menu.grid(row=2, column=0, padx=(0, 12), sticky="ew", pady=(5, 12))
        self.label(controls, "输出通道", color=MUTED).grid(row=1, column=1, sticky="w")
        self.channel_menu = ctk.CTkSegmentedButton(controls, values=["A", "B"], height=34,
                                                 fg_color=CARD, selected_color="#30476A", text_color=TEXT)
        self.channel_menu.set(self.settings.channel)
        self.channel_menu.grid(row=2, column=1, sticky="ew", pady=(5, 12))
        self.label(controls, "脉冲强度", color=MUTED).grid(row=3, column=0, sticky="w")
        strength_row = ctk.CTkFrame(controls, fg_color="transparent")
        strength_row.grid(row=3, column=1, sticky="e")
        self.strength_entry = ctk.CTkEntry(strength_row, width=66, height=28, fg_color=CARD,
                                          border_color="#30405A", justify="center", text_color=FIRE)
        self.strength_entry.insert(0, str(self.settings.strength))
        self.strength_entry.pack(side="left", padx=(0, 6))
        self.strength_entry.bind("<KeyRelease>", self.strength_typed)
        self.label(strength_row, f"/ {MAX_STRENGTH}", color=MUTED).pack(side="left")
        self.strength_slider = ctk.CTkSlider(controls, from_=0, to=MAX_STRENGTH, number_of_steps=MAX_STRENGTH,
                                            progress_color=FIRE, button_color=FIRE,
                                            command=self.strength_slid)
        self.strength_slider.set(self.settings.strength)
        self.strength_slider.grid(row=4, column=0, columnspan=2, pady=(8, 13), sticky="ew")
        self.label(controls, "持续时间（秒）", color=MUTED).grid(row=5, column=0, sticky="w")
        self.label(controls, "触发冷却（秒）", color=MUTED).grid(row=5, column=1, sticky="w")
        self.duration = ctk.CTkEntry(controls, height=34, fg_color=CARD, border_color="#30405A")
        self.duration.insert(0, ms_to_seconds(self.settings.duration_ms))
        self.duration.grid(row=6, column=0, padx=(0, 12), sticky="ew", pady=(5, 13))
        self.cooldown = ctk.CTkEntry(controls, height=34, fg_color=CARD, border_color="#30405A")
        self.cooldown.insert(0, ms_to_seconds(self.settings.cooldown_ms))
        self.cooldown.grid(row=6, column=1, sticky="ew", pady=(5, 13))
        self.label(controls, "时长与冷却无上限；时长 0 不输出，冷却 0 不等待；再次触发重新计时。",
                   size=12, color=MUTED, anchor="w").grid(row=7, column=0, columnspan=2, sticky="w")
        actions = ctk.CTkFrame(controls, fg_color="transparent")
        actions.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(16, 10))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.save_button = self.button(actions, "保存设置", self.save_settings)
        self.save_button.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.button(actions, "模拟一次失误", lambda: self.engine.submit(self.engine.preview()), color="#A3B4CE").grid(row=0, column=1, sticky="ew")
        self.arm_button = self.button(actions, "开启真实输出", self.arm, color=FIRE)
        self.arm_button.grid(row=1, column=0, padx=(0, 10), pady=(10, 0), sticky="ew")
        self.stop_button = ctk.CTkButton(actions, text="紧急停止  ·  F8", command=self.stop, height=36,
                                        corner_radius=9, fg_color=RED, hover_color="#BF4353", text_color="white",
                                        font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"))
        self.stop_button.grid(row=1, column=1, pady=(10, 0), sticky="ew")
        self.label(controls, "按设备说明佩戴，由使用者自愿手动开启。预览按钮始终不输出。",
                   size=12, color=MUTED, anchor="w").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.count_label = self.label(controls, "检测失误  0     /     完成脉冲  0", size=15, color=ICE, bold=True)
        self.count_label.grid(row=10, column=0, columnspan=2, sticky="w", pady=(12, 0))
        pairing = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        pairing.grid(row=0, column=1, rowspan=2, padx=(0, 12), pady=12, sticky="nsew")
        self.label(pairing, "连接手机 APP", size=18, bold=True).pack(anchor="w", padx=16, pady=(14, 10))
        self.connection_menu = ctk.CTkOptionMenu(pairing, values=["局域网 · 同一 Wi-Fi", "公网中继 · 支持异地"],
                                                fg_color=PANEL, button_color="#30476A", width=272,
                                                command=self.connection_mode_changed)
        self.connection_menu.set("公网中继 · 支持异地" if self.preferences["relay_mode"] == "remote" else "局域网 · 同一 Wi-Fi")
        self.connection_menu.pack(padx=14, pady=(0, 7))
        self.address_menu = ctk.CTkComboBox(pairing, values=local_addresses(), width=272,
                                          fg_color=PANEL, border_color="#30405A", button_color="#30476A")
        self.address_menu.pack(padx=14, pady=(0, 7))
        self.remote_entry = ctk.CTkEntry(pairing, width=272, fg_color=PANEL, border_color="#30405A")
        self.remote_entry.insert(0, self.preferences["remote_url"])
        self.connection_mode_changed(self.connection_menu.get())
        row = ctk.CTkFrame(pairing, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 8))
        self.connect_button = self.button(row, "生成二维码", self.connect, width=172)
        self.connect_button.pack(side="left", padx=(0, 10))
        self.button(row, "断开", lambda: self.engine.submit(self.engine.disconnect()), color="#A3B4CE", width=90).pack(side="left")
        self.qr_widget = self.label(pairing, "点击生成二维码\n使用 DG-LAB 4 APP 扫码", color=MUTED,
                                     width=236, height=236, fg_color=PANEL, corner_radius=9)
        self.qr_widget.pack(padx=14, pady=5)
        self.connection_label = self.label(pairing, "尚未连接", size=12, color=MUTED,
                                           wraplength=260, height=34)
        self.connection_label.pack(padx=14, pady=(0, 2))
        self.button(pairing, "复制配对链接", self.copy_link, color="#A3B4CE", width=272).pack(padx=14, pady=(3, 8))
        self.label(pairing, "扫码后开放设备权限，\n并在 APP 中设置通道强度上限。", size=12,
                   color=MUTED).pack(padx=14, pady=(0, 12))
        self.limit_label = self.label(pairing, "连接设备后显示 APP 通道上限", size=12,
                                       color=ICE, wraplength=265)
        self.limit_label.pack(padx=14, pady=(0, 10))
        log = ctk.CTkFrame(page, fg_color="transparent")
        log.grid(row=1, column=0, sticky="nsew", padx=(14, 18), pady=(0, 10))
        log.grid_columnconfigure(0, weight=1)
        log.grid_rowconfigure(1, weight=1)
        self.label(log, "事件记录", size=16, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.button(log, "导出 CSV", self.export_log, color="#A3B4CE", width=100).grid(row=0, column=1, pady=(0, 7))
        self.log_box = ctk.CTkTextbox(log, fg_color=BG, text_color=MUTED, corner_radius=10,
                                     height=110, font=ctk.CTkFont(family="Microsoft YaHei UI", size=12))
        self.log_box.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.log_box.configure(state="disabled")

    def connection_mode_changed(self, value):
        if not hasattr(self, "remote_entry"):
            return
        self.address_menu.pack_forget()
        self.remote_entry.pack_forget()
        widget = self.remote_entry if "公网" in value else self.address_menu
        # Place the address between the connection choice and the action row.
        children = self.address_menu.master.winfo_children()
        action = next((item for item in children if isinstance(item, ctk.CTkFrame)), None)
        widget.pack(padx=14, pady=(0, 7), before=action) if action else widget.pack(padx=14, pady=(0, 7))

    def _install_page(self):
        page = self.install_tab
        page.grid_columnconfigure(0, weight=1)
        self.label(page, "让游戏直接报告失误", size=23, bold=True).grid(row=0, column=0, padx=24, pady=(24, 12), sticky="w")
        self.label(page, "安装一次监听插件，之后每次启动游戏都会连接。插件仅监听事件，不修改判定。",
                   color=MUTED).grid(row=1, column=0, padx=24, sticky="w")
        self.label(page, "游戏目录", color=MUTED).grid(row=2, column=0, padx=24, pady=(25, 8), sticky="w")
        row = ctk.CTkFrame(page, fg_color="transparent")
        row.grid(row=3, column=0, padx=24, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.game_path = ctk.CTkEntry(row, height=38, fg_color=CARD, border_color="#30405A")
        self.game_path.insert(0, self.preferences["game_path"])
        self.game_path.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.button(row, "选择目录", self.browse_game, width=112).grid(row=0, column=1)
        self.label(page, "请选择包含 A Dance of Fire and Ice.exe 的文件夹。", size=12,
                   color=MUTED).grid(row=4, column=0, padx=24, pady=10, sticky="w")
        row = ctk.CTkFrame(page, fg_color="transparent")
        row.grid(row=5, column=0, padx=24, pady=(10, 15), sticky="w")
        self.install_button = self.button(row, "安装监听插件", self.install_plugin, width=175)
        self.install_button.pack(side="left", padx=(0, 14))
        self.uninstall_button = self.button(row, "卸载本软件插件", self.uninstall_plugin, color="#A3B4CE", width=175)
        self.uninstall_button.pack(side="left")
        self.install_status = self.label(page, "先退出游戏，再点击安装。", color=ICE, wraplength=860, justify="left", anchor="w")
        self.install_status.grid(row=6, column=0, padx=24, pady=(10, 18), sticky="w")
        text = "安装方式\n\n已有 Unity Mod Manager：安装对应的 UMM 插件。\n已有 BepInEx 5：安装监听 DLL。\n没有加载器：一并安装附带的 BepInEx 5.4.23.5。\n\n安装记录会保存新增文件；卸载时只移除本软件创建且未被修改的文件。\n检测到其他加载器时会停止安装，并显示原因。"
        self.label(page, text, size=14, color=MUTED, justify="left", anchor="w").grid(row=7, column=0, padx=24, sticky="w")

    def _help_page(self):
        page = self.help_tab
        self.label(page, "从预览开始", size=23, bold=True).pack(anchor="w", padx=26, pady=(24, 16))
        text = ("1   在「游戏插件」里安装插件，然后重新启动游戏。\n\n"
                "2   手机用 DG-LAB 4 APP 连接郊狼 3.0；电脑选择局域网或公网中继，生成二维码。\n"
                "     APP 扫码后开放这台设备，等状态显示「设备已连接」。\n\n"
                "3   先在模拟模式里游玩，确认事件记录能准确显示你的失误。\n\n"
                "4   设置 A / B 通道、强度、持续时间与冷却，保存后手动点击「开启真实输出」。\n\n"
                "5   F8 或红色按钮可紧急停止；断开手机、蓝牙或游戏后会关闭真实输出。\n"
                "     重连不会自动恢复，需要再次手动开启。")
        self.label(page, text, size=14, color=TEXT, justify="left", anchor="w").pack(anchor="w", padx=26)
        notes = ("连接提示\n\n局域网：电脑和手机连接同一 Wi-Fi；系统提示时允许专用网络访问。\n"
                 "有 VPN 或多个网卡时，可在地址列表中选择手机能访问的电脑地址。\n"
                 "公网：使用 DG-LAB 官方 V4 中继，需联网；也可填自己的 wss:// V4 服务。\n\n"
                 "触发规则\n\n死亡模式仅在关卡失败时触发。失误模式包括过早、过晚、漏按和多按。\n"
                 "非纯完美模式还包括 EarlyPerfect / LatePerfect。自动演奏不触发。\n"
                 "同一次失败的重复事件会合并。冷却结束可再次触发，并替换当前脉冲重新计时。\n\n"
                 "强度范围 0–200；持续时间与冷却时间独立设置，没有软件上限。\n"
                 "时长 0 不输出，冷却 0 不等待。APP 和设备的通道上限继续生效。")
        self.label(page, notes, size=13, color=MUTED, justify="left", anchor="w").pack(anchor="w", padx=26, pady=(20, 10))

    def _discover(self):
        if not self.game_path.get():
            path = installer.discover_game()
            if path:
                self.game_path.insert(0, path)
                self.add_log("已找到本机游戏目录。")

    def browse_game(self):
        path = filedialog.askdirectory(title="选择冰与火之舞游戏目录", initialdir=self.game_path.get() or None)
        if path:
            self.game_path.delete(0, "end")
            self.game_path.insert(0, path)

    def strength_slid(self, value):
        self.strength_slider.set(value)
        self.strength_entry.delete(0, "end")
        self.strength_entry.insert(0, str(round(value)))
        self.strength_typed()

    def strength_typed(self, event=None):
        try:
            value = int(self.strength_entry.get())
            valid = 0 <= value <= MAX_STRENGTH
        except ValueError:
            valid = False
        self.strength_entry.configure(border_color="#30405A" if valid else RED)
        if valid:
            self.strength_slider.set(value)

    def update_limit(self):
        if not self.device_info:
            self.limit_label.configure(text="连接设备后显示 APP 通道上限")
            return
        channel = self.settings.channel
        maximum = channel_limit(self.device_info, channel)
        text = f"已保存：{channel} 通道 · 强度 {self.settings.strength}"
        text += ("\nAPP 未报告通道上限" if maximum is None else
                 f"\nAPP 上限 {maximum} · 实际最高 {min(maximum, self.settings.strength)}")
        self.limit_label.configure(text=text)

    def read_settings(self):
        try:
            duration = seconds_to_ms(self.duration.get())
            cooldown = seconds_to_ms(self.cooldown.get())
            return Settings(MODES[self.mode_menu.get()], self.channel_menu.get(),
                            int(self.strength_entry.get()), duration, cooldown)
        except (ValueError, OverflowError):
            raise ValueError("请输入有效数值：强度 0–200；持续时间和冷却时间为有限的非负秒数，两者无需比较。")

    def save_settings(self):
        try:
            settings = self.read_settings()
            previous = self.preferences["settings"]
            self.preferences["settings"] = asdict(settings)
            try:
                self._save_preferences()
            except OSError:
                self.preferences["settings"] = previous
                raise
            self.save_button.configure(text="正在保存…", state="disabled")
            self.engine.submit(self.engine.configure(settings))
        except (ValueError, OSError) as error:
            messagebox.showerror("设置未保存", str(error), parent=self)

    def _save_preferences(self):
        self.preferences["game_path"] = self.game_path.get().strip()
        self.preferences["remote_url"] = self.remote_entry.get().strip()
        self.preferences["relay_mode"] = "remote" if "公网" in self.connection_menu.get() else "lan"
        save_preferences(self.preference_path, self.preferences)

    def arm(self):
        if self.armed:
            self.stop()
            return
        try:
            settings = self.read_settings()
            if settings != self.settings:
                raise ValueError("设置有修改，请先点击「保存设置」。")
            if self.selfcheck:
                raise ValueError("检查模式不会开启真实输出。")
            self.engine.submit(self.engine.arm())
        except ValueError as error:
            messagebox.showerror("尚未开启", str(error), parent=self)

    def stop(self):
        self.engine.submit(self.engine.disarm("手动紧急停止"))

    def connect(self):
        try:
            self._save_preferences()
            if "公网" in self.connection_menu.get():
                self.engine.submit(self.engine.connect_remote(self.remote_entry.get().strip()))
            else:
                address = self.address_menu.get().strip()
                socket.inet_aton(address)
                if address.startswith("127."):
                    raise ValueError("没有找到局域网地址，请连接网络，或选择公网中继。")
                self.engine.submit(self.engine.connect_lan(address))
        except (ValueError, OSError) as error:
            messagebox.showerror("连接未启动", str(error), parent=self)

    def copy_link(self):
        if self.qr_link:
            self.clipboard_clear()
            self.clipboard_append(self.qr_link)
            self.add_log("配对链接已复制。")

    def _plugin_action(self, uninstall=False):
        path = self.game_path.get().strip()
        try:
            self._save_preferences()
        except OSError as error:
            messagebox.showerror("设置未保存", str(error), parent=self)
            return
        self.install_button.configure(state="disabled")
        self.uninstall_button.configure(state="disabled")
        self.install_status.configure(text="正在处理…")
        def run():
            try:
                result = installer.uninstall(path) if uninstall else installer.install(
                    path, self.profile, self.preferences["game_token"])
                self.ui_messages.put(("install_result", result))
            except Exception as error:
                self.ui_messages.put(("install_error", str(error)))
        threading.Thread(target=run, daemon=True).start()

    def install_plugin(self):
        self._plugin_action()

    def uninstall_plugin(self):
        self._plugin_action(uninstall=True)

    def add_log(self, text):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_entries.append((datetime.datetime.now().isoformat(timespec="seconds"), text))
        self.log_entries = self.log_entries[-1000:]
        self.logger.info(text)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{now}   {text}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 250:
            self.log_box.delete("1.0", "51.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def export_log(self):
        path = filedialog.asksaveasfilename(title="导出事件记录", defaultextension=".csv",
                                            initialfile="FireIceDG-events.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["时间", "事件"])
                    writer.writerows(self.log_entries)
            except OSError as error:
                messagebox.showerror("导出失败", str(error), parent=self)
                return
            self.add_log("事件记录已导出。")

    def poll(self):
        if self.is_closing:
            return
        for source in (self.engine.messages, self.ui_messages):
            for _ in range(80):
                try:
                    kind, value = source.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    self.add_log(value)
                elif kind == "error":
                    self.save_button.configure(text="保存设置", state="normal")
                    self.add_log("[提示] " + value)
                    if not self.selfcheck:
                        messagebox.showerror("FireIceDG", value, parent=self)
                elif kind == "connection":
                    self.connection_label.configure(text=value)
                elif kind == "connecting":
                    self.connect_button.configure(text="正在连接…" if value else "生成二维码",
                                                   state="disabled" if value else "normal")
                elif kind == "settings":
                    self.settings = value
                    self.save_button.configure(text="保存设置", state="normal")
                    self.update_limit()
                elif kind == "qr":
                    self.qr_link = value
                    if value:
                        qr = qrcode.make(value).convert("RGB")
                        self.qr_image = ctk.CTkImage(qr, qr, size=(236, 236))
                        self.qr_widget.configure(image=self.qr_image, text="")
                    else:
                        self.qr_image = None
                        self.qr_widget.configure(image=None, text="点击生成二维码\n使用 DG-LAB 4 APP 扫码")
                elif kind == "game":
                    online = value.get("online", False)
                    caption = "已连接 · 游玩中" if value.get("playing") else "已连接 · 待游玩"
                    if not online:
                        caption = "接口不兼容" if value.get("ready") is False else "等待游戏"
                    self.game_status.configure(text=caption, text_color=ICE if online else MUTED)
                elif kind == "device":
                    self.device_info = value
                    self.device_status.configure(text="设备已连接" if value else "等待设备", text_color=GREEN if value else MUTED)
                    self.update_limit()
                elif kind == "armed":
                    self.armed = value is True
                    preparing = value == "preparing"
                    self.mode_status.configure(text="正在开启…" if preparing else "真实输出已开启" if self.armed else "模拟模式")
                    self.arm_button.configure(text="关闭真实输出" if self.armed else "开启真实输出",
                                               state="disabled" if preparing else "normal")
                elif kind == "counts":
                    self.count_label.configure(text=f"检测失误  {value[0]}     /     完成脉冲  {value[1]}")
                elif kind == "remote_stop":
                    self.engine.submit(self.engine.disarm("APP 动作 0 紧急停止"))
                elif kind in {"install_result", "install_error"}:
                    self.install_button.configure(state="normal")
                    self.uninstall_button.configure(state="normal")
                    self.install_status.configure(text=value, text_color=FIRE if kind == "install_error" else GREEN)
                    self.add_log(value)
        self.after(80, self.poll)

    def close_app(self):
        if self.is_closing:
            return
        self.is_closing = True
        self.stop_button.configure(text="正在停止…")
        self.update_idletasks()
        self.hotkey.close()
        try:
            self.engine.close()
        finally:
            self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    mutex = None
    if os.name == "nt" and not args.selfcheck:
        import ctypes.wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = w.HANDLE
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
        mutex = kernel.CreateMutexW(None, False, "Local\\FireIceDG-v1")
        if ctypes.get_last_error() == 183:
            messagebox.showinfo("FireIceDG", "软件已经在运行。请查看已打开的窗口。")
            return
    try:
        try:
            app = App(selfcheck=args.selfcheck)
        except (OSError, RuntimeError) as error:
            if args.selfcheck:
                raise
            messagebox.showerror("FireIceDG 启动失败", str(error))
            return
        if args.selfcheck:
            app.after(1000, app.close_app)
        app.mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == "__main__":
    main()
