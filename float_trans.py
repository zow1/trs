#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FloatTrans · 悬浮翻译  v1.1

一个给看片用的桌面翻译小工具：
  * 无边框、圆角、永远置顶，可随手拖到屏幕任意角落
  * 全局热键 Ctrl+Alt+T 随时呼出（可收成一颗小圆球，不挡画面）
  * 复制任意文字自动翻译（看电影时选中字幕 Ctrl+C 即可）
  * 截图取词 Ctrl+Alt+S：框哪翻哪，硬字幕也能翻
  * 悬停取词 Ctrl+Alt+H：按住 Alt 停住鼠标，光标下的文字自动翻译
  * 单词给音标 + 词典释义，句子给整句翻译，还能点发音

界面与网络部分纯标准库；OCR 走 Windows 自带识别引擎（winsdk），离线免安装。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import font as tkfont

try:
    import screen_ocr as so
except Exception:                      # 取词功能不可用时，翻译本身照常工作
    so = None

# --------------------------------------------------------------------------- 常量

APP_NAME = "FloatTrans"
APP_TITLE = "悬浮翻译"
VERSION = "1.1.1"

CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LOG_PATH = os.path.join(CFG_DIR, "app.log")

# 配色
BG = "#12141b"
BG_SOFT = "#191c26"
BG_INPUT = "#1e2230"
BORDER = "#2b3042"
ACCENT = "#5b9dff"
ACCENT_HI = "#8fc0ff"
TEXT = "#e9edf6"
MUTED = "#8b93a8"
DIM = "#5f6779"
ERROR = "#ff7b72"

FAMILY = "Microsoft YaHei UI"
FAMILY_MONO = "Consolas"

DEFAULT_W = 450
MIN_H = 132
MAX_H = 660

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEFAULTS = {
    "x": None,
    "y": None,
    "alpha": 0.96,
    "font_size": 13,
    "clipboard": True,
    "tts": False,
    "autostart": False,
    "collapsed": False,
    "hover": False,          # 悬停取词开关
    "hover_mod": "alt",      # 悬停时要按住的修饰键
}

# --------------------------------------------------------------------------- 小工具


def ensure_dir():
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
    except Exception:
        pass


def log(msg: str):
    try:
        ensure_dir()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_cfg(cfg: dict):
    try:
        ensure_dir()
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception as e:
        log("save cfg failed: %r" % (e,))


def enable_dpi_awareness():
    """高分屏下让界面不发虚。macOS 无需此设置。"""
    if sys.platform == "darwin":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# --------------------------------------------------------------------------- 网络


def http_get(url: str, timeout: float = 7.0, data: bytes | None = None,
             referer: str | None = None) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")
TAG_RE = re.compile(r"<[^>]+>")
POS_RE = re.compile(r"^([a-z]+\.(?:\s*[&/,]\s*[a-z]+\.)*)\s+(.+)$")


def trim_def(text: str, limit: int = 52) -> str:
    """释义太长时在分号/逗号处截断，避免最后一行只剩一个字。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("；", ";", "，", ","):
        i = head.rfind(sep)
        if i >= limit // 2:
            return head[:i + 1] + "…"
    return head + "…"


class TranslateError(Exception):
    pass


class Result:
    __slots__ = ("src", "text", "phonetic", "defs", "engine", "elapsed", "tts_url")

    def __init__(self, src: str):
        self.src = src
        self.text = ""
        self.phonetic = ""
        self.defs: list[str] = []
        self.engine = ""
        self.elapsed = 0.0
        self.tts_url = ""


class Translator:
    """有道为主，Google / MyMemory 兜底；单词额外查词典。"""

    def __init__(self):
        self.google_ok = None  # 探测结果：None 未探测
        self._probe_lock = threading.Lock()

    # ---------------------------------------------------------------- 探测
    def probe_google(self):
        with self._probe_lock:
            if self.google_ok is not None:
                return
            try:
                self._google("hello", "zh-CN")
                self.google_ok = True
            except Exception:
                self.google_ok = False
            log("google reachable: %s" % self.google_ok)

    # ---------------------------------------------------------------- 主入口
    def translate(self, text: str) -> Result:
        text = " ".join(text.split())
        if not text:
            raise TranslateError("没有可翻译的内容")
        if len(text) > 2000:
            text = text[:2000]

        res = Result(text)
        target = "en" if CJK_RE.search(text) else "zh-CN"

        # 1) 单个英文单词 -> 查词典（音标 / 释义 / 真人发音）
        if WORD_RE.match(text):
            try:
                info = self._youdao_dict(text)
                if info:
                    res.phonetic, res.defs = info
                    res.tts_url = ("https://dict.youdao.com/dictvoice?type=2&audio="
                                   + urllib.parse.quote(text))
            except Exception as e:
                log("dict fail %r" % (e,))

        # 2) 整句翻译（多引擎降级）
        engines = [("有道", self._youdao_ai)]
        if self.google_ok:
            engines.append(("Google", self._google))
        engines.append(("MyMemory", self._mymemory))

        last_err = None
        for name, fn in engines:
            try:
                out, tts = fn(text, target)
                if out:
                    res.text = out
                    res.engine = name
                    res.tts_url = res.tts_url or tts
                    return res
            except Exception as e:
                last_err = e
                log("engine %s fail: %r" % (name, e))

        if res.defs:  # 词典里拿到了释义也算成功
            res.text = "\n".join(res.defs[:2])
            res.engine = "有道词典"
            return res

        raise TranslateError("网络翻译失败，请检查网络后重试（%s）"
                             % (str(last_err)[:60] if last_err else "无可用引擎"))

    # ---------------------------------------------------------------- 有道
    def _youdao_ai(self, text: str, target: str):
        params = {"q": text, "from": "Auto", "to": "zh-CHS" if target == "zh-CN" else "en"}
        body = urllib.parse.urlencode(params).encode()
        url = "https://aidemo.youdao.com/trans"
        raw = http_get(url, timeout=8, data=body,
                       referer="https://ai.youdao.com/")
        data = json.loads(raw)
        if str(data.get("errorCode")) != "0":
            raise TranslateError("youdao error %s" % data.get("errorCode"))
        parts = data.get("translation") or []
        out = "".join(parts).strip()
        if not out:
            raise TranslateError("empty")
        tts = data.get("tSpeakUrl") or data.get("speakUrl") or ""
        return out, tts

    def _youdao_dict(self, word: str):
        url = ("https://dict.youdao.com/jsonapi?q="
               + urllib.parse.quote(word))
        data = json.loads(http_get(url, timeout=6))
        ec = (data.get("ec") or {}).get("word") or []
        if not ec:
            return None
        w = ec[0]
        phon = []
        if w.get("ukphone"):
            phon.append("英 /%s/" % w["ukphone"])
        if w.get("usphone"):
            phon.append("美 /%s/" % w["usphone"])
        defs = []
        for trs in w.get("trs", []):
            for tr in trs.get("tr", []):
                for line in tr.get("l", {}).get("i", []):
                    line = TAG_RE.sub("", line).strip()
                    if line:
                        defs.append(line)
        if not phon and not defs:
            return None
        return "   ".join(phon), defs

    # ---------------------------------------------------------------- Google
    def _google(self, text: str, target: str):
        tl = "zh-CN" if target == "zh-CN" else "en"
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=auto&tl=%s&dt=t&q=%s"
               % (tl, urllib.parse.quote(text)))
        data = json.loads(http_get(url, timeout=5))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        if not out.strip():
            raise TranslateError("empty")
        return out.strip(), ""

    # ---------------------------------------------------------------- MyMemory
    def _mymemory(self, text: str, target: str):
        pair = "en|zh-CN" if target == "zh-CN" else "zh-CN|en"
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=%s"
               % (urllib.parse.quote(text), urllib.parse.quote(pair)))
        data = json.loads(http_get(url, timeout=8))
        out = (data.get("responseData") or {}).get("translatedText") or ""
        out = TAG_RE.sub("", out).strip()
        if not out or "QUERY LENGTH LIMIT" in out.upper():
            raise TranslateError("mymemory empty")
        return out, ""


# --------------------------------------------------------------------------- 发音


_mci_lock = threading.Lock()
_IS_MAC = (sys.platform == "darwin")


def _mci(cmd: str):
    try:
        ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
    except Exception:
        pass


def _play_mp3_mac(path: str) -> bool:
    """macOS 用 afplay 播放 mp3，阻塞等待结束。"""
    try:
        r = subprocess.run(["afplay", path], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def play_url(url: str, fallback_text: str = ""):
    """下载有道发音 mp3 并播放；失败则退回系统朗读。"""
    def run():
        path = os.path.join(tempfile.gettempdir(), "floattrans_tts.mp3")
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": UA}),
                    timeout=8) as r:
                data = r.read()
            if len(data) < 800:
                raise ValueError("too small")
            with open(path, "wb") as f:
                f.write(data)
            if _IS_MAC:
                if _play_mp3_mac(path):
                    return
            else:
                with _mci_lock:
                    _mci("close ft_tts")
                with _mci_lock:
                    if _mci_ret('open "%s" type mpegvideo alias ft_tts' % path) == 0:
                        _mci("play ft_tts")
                        return
            raise ValueError("play failed")
        except Exception as e:
            log("tts fail %r" % (e,))
            if fallback_text and not CJK_RE.search(fallback_text):
                speak_fallback(fallback_text)

    threading.Thread(target=run, daemon=True).start()


def _mci_ret(cmd: str) -> int:
    try:
        return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
    except Exception:
        return -1


def speak_fallback(text: str):
    """没有网络发音时用系统自带语音朗读（仅英文有意义）。"""
    if not text:
        return
    if _IS_MAC:
        try:
            subprocess.Popen(["say", "-r", "170", text[:200]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log("say fail %r" % (e,))
        return
    text = text.replace("'", "''")[:200]
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "$s.Rate=0;$s.Speak('%s')" % text)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("sapi fail %r" % (e,))


# --------------------------------------------------------------------------- 全局热键


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

HOTKEY_SPECS = [
    ("show", [
        (MOD_CONTROL | MOD_ALT, ord("T"), "Ctrl+Alt+T"),
        (MOD_CONTROL | MOD_ALT, ord("Y"), "Ctrl+Alt+Y"),
        (MOD_CONTROL | MOD_SHIFT, 0x78, "Ctrl+Shift+F9"),
    ]),
    ("shot", [
        (MOD_CONTROL | MOD_ALT, ord("S"), "Ctrl+Alt+S"),
        (MOD_CONTROL | MOD_SHIFT, ord("S"), "Ctrl+Shift+S"),
    ]),
    ("hover", [
        (MOD_CONTROL | MOD_ALT, ord("H"), "Ctrl+Alt+H"),
        (MOD_CONTROL | MOD_SHIFT, ord("H"), "Ctrl+Shift+H"),
    ]),
]


_MAC_HOTKEY_ALT = [
    ("show", ["<ctrl>+<alt>+t", "<ctrl>+<alt>+y", "<ctrl>+<shift>+<f9>"],
     "Ctrl+Alt+T"),
    ("shot", ["<ctrl>+<alt>+s", "<ctrl>+<shift>+s"], "Ctrl+Alt+S"),
    ("hover", ["<ctrl>+<alt>+h", "<ctrl>+<shift>+h"], "Ctrl+Alt+H"),
]


class HotkeyListener(threading.Thread):
    """一个线程里注册多个全局热键，按下时点亮对应的 Event。

    Windows: RegisterHotKey + GetMessage 消息循环。
    macOS:   pynput.GlobalHotKeys（需要辅助功能权限）。
    """

    def __init__(self, events: dict):
        super().__init__(daemon=True)
        self.events = events
        self.labels: dict[str, str] = {}
        self.label = ""
        self._listener = None

    def run(self):
        if _IS_MAC:
            self._run_mac()
            return
        user32 = ctypes.windll.user32
        id_map = {}
        for i, (name, cands) in enumerate(HOTKEY_SPECS):
            hk_id = 0xB001 + i
            for mods, vk, label in cands:
                if user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                    id_map[hk_id] = name
                    self.labels[name] = label
                    break
            else:
                log("register hotkey failed: %s" % name)
        self.label = self.labels.get("show", "")
        if not id_map:
            return
        from ctypes import wintypes
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                name = id_map.get(msg.wParam)
                ev = self.events.get(name) if name else None
                if ev is not None:
                    ev.set()

    def _run_mac(self):
        try:
            from pynput import keyboard
        except Exception as e:
            log("pynput missing: %r" % (e,))
            return

        def make_cb(ev):
            def cb():
                ev.set()
            return cb

        mapping = {}
        for name, combos, label in _MAC_HOTKEY_ALT:
            ev = self.events.get(name)
            if ev is None:
                continue
            # 只取第一个候选组合注册，避免 pynput 冲突
            for combo in combos:
                if combo not in mapping:
                    mapping[combo] = make_cb(ev)
                    self.labels[name] = label
                    break
        if not mapping:
            return
        self.label = self.labels.get("show", "")
        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.start()
            self._listener.join()
        except Exception as e:
            log("mac hotkey fail: %r" % (e,))


# --------------------------------------------------------------------------- 自启动


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


_MAC_LAUNCH_LABEL = "com.zow1.FloatTrans"


def _mac_launch_plist_path() -> str:
    return os.path.expanduser(
        "~/Library/LaunchAgents/%s.plist" % _MAC_LAUNCH_LABEL)


def _set_autostart_mac(on: bool) -> bool:
    path = _mac_launch_plist_path()
    if not on:
        try:
            if os.path.exists(path):
                subprocess.run(["launchctl", "unload", path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
                os.remove(path)
            return True
        except Exception as e:
            log("autostart off fail %r" % (e,))
            return False
    try:
        if getattr(sys, "frozen", False):
            prog_args = [sys.executable]
        else:
            prog_args = [sys.executable, os.path.abspath(__file__)]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        args_xml = "".join("<string>%s</string>" % a for a in prog_args)
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>Label</key><string>%s</string>\n'
            '  <key>ProgramArguments</key><array>%s</array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '</dict></plist>\n' % (_MAC_LAUNCH_LABEL, args_xml))
        with open(path, "w", encoding="utf-8") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", path],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
        return True
    except Exception as e:
        log("autostart on fail %r" % (e,))
        return False


def set_autostart(on: bool) -> bool:
    if _IS_MAC:
        return _set_autostart_mac(on)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log("autostart fail %r" % (e,))
        return False


# --------------------------------------------------------------------------- 单实例


SINGLE_PORT = 47653
MUTEX_NAME = "Local\\FloatTrans_SingleInstance_v1"


def _mac_lock_path() -> str:
    base = os.path.expanduser("~/Library/Application Support/FloatTrans")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = tempfile.gettempdir()
    return os.path.join(base, ".lock")


def acquire_instance():
    """保证只跑一个实例；第二个实例让已运行的窗口弹出来。"""
    if _IS_MAC:
        import fcntl
        lock_path = _mac_lock_path()
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                c = socket.create_connection(("127.0.0.1", SINGLE_PORT), 1.5)
                c.sendall(b"show")
                c.close()
            except Exception:
                pass
            fh.close()
            return None
        # 保住 fh 引用，防止被回收导致锁释放
        acquire_instance._fh = fh
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", SINGLE_PORT))
            s.listen(2)
        except OSError as e:
            log("bind command port fail %r" % (e,))
        return s

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            try:
                c = socket.create_connection(("127.0.0.1", SINGLE_PORT), 1.5)
                c.sendall(b"show")
                c.close()
            except Exception:
                pass
            return None
    except Exception:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_PORT))
        s.listen(2)
    except OSError as e:
        log("bind command port fail %r" % (e,))
    return s


# --------------------------------------------------------------------------- 自绘滑块


class MiniSlider(tk.Canvas):
    """比 tk.Scale 好看很多的极简滑块。"""

    def __init__(self, master, lo, hi, value, on_change, height=24):
        super().__init__(master, height=height, bg=BG, bd=0,
                         highlightthickness=0, cursor="hand2")
        self.lo, self.hi = lo, hi
        self.value = value
        self.on_change = on_change
        self._drag = False
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 200
        h = self.winfo_height() or 24
        pad = 9
        y = h // 2
        self.create_line(pad, y, w - pad, y, fill="#2a3142", width=4, capstyle="round")
        ratio = (self.value - self.lo) / float(max(1, self.hi - self.lo))
        x = pad + (w - 2 * pad) * ratio
        self.create_line(pad, y, x, y, fill=ACCENT, width=4, capstyle="round")
        self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=ACCENT, outline=BG, width=2)

    def _press(self, e):
        self._drag = True
        self._set_from_x(e.x)

    def _move(self, e):
        if self._drag:
            self._set_from_x(e.x)

    def _release(self, _e):
        self._drag = False

    def _set_from_x(self, x):
        w = self.winfo_width() or 200
        pad = 9
        ratio = min(1.0, max(0.0, (x - pad) / float(max(1, w - 2 * pad))))
        v = int(round(self.lo + (self.hi - self.lo) * ratio))
        if v != self.value:
            self.value = v
            self._draw()
            self.on_change(v)


# --------------------------------------------------------------------------- 占位输入框


class PlaceholderEntry(tk.Entry):
    def __init__(self, master, placeholder: str, **kw):
        super().__init__(master, **kw)
        self.placeholder = placeholder
        self._ph = False
        self.bind("<FocusIn>", self._on_in, add="+")
        self.bind("<FocusOut>", self._on_out, add="+")
        self._show()

    def _show(self):
        if not super().get():
            self._ph = True
            self.configure(fg=DIM)
            super().insert(0, self.placeholder)

    def _hide(self):
        if self._ph:
            self._ph = False
            super().delete(0, "end")
            self.configure(fg=TEXT)

    def _on_in(self, _e):
        self._hide()

    def _on_out(self, _e):
        self._show()

    def get_text(self) -> str:
        return "" if self._ph else super().get()

    def set_text(self, text: str):
        self._hide()
        super().delete(0, "end")
        super().insert(0, text)


# --------------------------------------------------------------------------- 框选截图


class RegionSelector:
    """整屏蒙一层压暗的截图，拖动框选要识别的区域。

    压暗图和原图各准备一张：背景铺压暗图，选区把原图整张贴进去、
    由父 Frame 裁出可见部分，所以选区是清晰原色、四周平滑变暗。
    松手回调 on_done(x, y, w, h)，坐标相对这张整屏图。
    """

    def __init__(self, root, bgra, vx, vy, vw, vh, on_done, on_cancel):
        self.bgra = bgra
        self.vx, self.vy, self.vw, self.vh = vx, vy, vw, vh
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.done = False
        self.x0 = self.y0 = None
        self.dark_photo = None
        self.bright_photo = None

        win = tk.Toplevel(root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#0b0d13")
        win.geometry("%dx%d+%d+%d" % (vw, vh, vx, vy))

        cv = tk.Canvas(win, width=vw, height=vh, bg="#0b0d13", bd=0,
                       highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        self.cv = cv

        try:
            p1 = so.tmp_path("ppm")
            so.write_ppm(p1, bgra, vw, vh, level=0.4)
            self.dark_photo = tk.PhotoImage(file=p1)
            cv.create_image(0, 0, image=self.dark_photo, anchor="nw")

            p2 = so.tmp_path("ppm")
            so.write_ppm(p2, bgra, vw, vh)
            self.bright_photo = tk.PhotoImage(file=p2)
            for p in (p1, p2):
                try:
                    os.remove(p)
                except Exception:
                    pass
        except Exception as e:
            log("overlay background fail %r" % (e,))

        # 亮框：把原图整张贴进去，多出来的部分由父 Frame 裁掉，
        # 这样选区是清晰的，周围是平滑压暗的，没有点阵噪点。
        self.view = tk.Frame(win, bg="#000000", bd=0, highlightthickness=2,
                             highlightbackground=ACCENT)
        self.view_inner = tk.Label(self.view, bd=0, highlightthickness=0,
                                   bg="#000000")
        if self.bright_photo is not None:
            self.view_inner.configure(image=self.bright_photo)
        self.view_inner.place(x=0, y=0)

        # 十字准星
        self.cross_v = cv.create_line(0, 0, 0, vh, fill=ACCENT, width=1,
                                      dash=(4, 4))
        self.cross_h = cv.create_line(0, 0, vw, 0, fill=ACCENT, width=1,
                                      dash=(4, 4))

        # 尺寸提示
        self.size_lbl = tk.Label(win, text="", bg="#0b0d13", fg=ACCENT,
                                 font=(FAMILY, 9), padx=7, pady=2, bd=0,
                                 highlightthickness=1,
                                 highlightbackground=ACCENT)

        # 顶部说明
        self.hint = tk.Label(win, text="拖动框选要翻译的区域　·　Esc 取消",
                             bg="#0b0d13", fg=TEXT, font=(FAMILY, 9),
                             padx=16, pady=5, bd=0, highlightthickness=1,
                             highlightbackground=BORDER)
        self.hint.place(x=vw // 2, y=18, anchor="n")

        cv.bind("<Button-1>", self._press)
        cv.bind("<B1-Motion>", self._drag)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Motion>", self._move)
        cv.bind("<Button-3>", lambda e: self._cancel())
        win.bind("<Escape>", lambda e: self._cancel())

        win.update_idletasks()
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

    # ---------------------------------------------------------------- 事件
    def _press(self, e):
        self.x0, self.y0 = e.x, e.y
        self._drag(e)

    def _drag(self, e):
        if self.x0 is None:
            return
        x1 = max(0, min(e.x, self.vw))
        y1 = max(0, min(e.y, self.vh))
        l, t = min(self.x0, x1), min(self.y0, y1)
        r, b = max(self.x0, x1), max(self.y0, y1)
        w, h = r - l, b - t

        self.cv.coords(self.cross_v, e.x, 0, e.x, self.vh)
        self.cv.coords(self.cross_h, 0, e.y, self.vw, e.y)
        if w < 2 or h < 2:
            return

        self.view.place(x=l, y=t, width=w, height=h)
        self.view_inner.place(x=-l, y=-t)

        self.size_lbl.configure(text="%d × %d" % (w, h))
        ly = t - 28 if t > 32 else b + 6
        self.size_lbl.place(x=l, y=ly)

    def _move(self, e):
        # 有些情况下 <B1-Motion> 收不到，靠 <Motion> + 按键状态兜底
        if self.x0 is not None and (e.state & 0x0100):
            self._drag(e)
            return
        self.cv.coords(self.cross_v, e.x, 0, e.x, self.vh)
        self.cv.coords(self.cross_h, 0, e.y, self.vw, e.y)

    def _release(self, e):
        if self.x0 is None:
            return
        self._drag(e)
        l = min(self.x0, e.x)
        t = min(self.y0, e.y)
        w = abs(e.x - self.x0)
        h = abs(e.y - self.y0)
        if w < 8 or h < 8:
            self._cancel()
            return
        self._finish(l, t, w, h)

    def _finish(self, x, y, w, h):
        if self.done:
            return
        self.done = True
        try:
            self.win.destroy()
        except Exception:
            pass
        try:
            self.on_done(x, y, w, h)
        except Exception as e:
            log("region done fail %r" % (e,))

    def _cancel(self):
        if self.done:
            return
        self.done = True
        try:
            self.win.destroy()
        except Exception:
            pass
        try:
            self.on_cancel()
        except Exception:
            pass


# --------------------------------------------------------------------------- 悬停气泡


class HoverPopup:
    """鼠标旁边的小气泡：上面原文，下面译文。"""

    def __init__(self, app):
        self.app = app
        self.win = None
        self.job = None
        self.visible = False
        self.shown_at = 0.0
        self.last_trans = ""

    def show(self, cx, cy, src, trans, engine):
        self.hide()
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)

        wrap = tk.Frame(win, bg=BG, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)

        inner = tk.Frame(wrap, bg=BG)
        inner.pack(fill="both", expand=True, padx=12, pady=9)

        wl = 360
        if src:
            tk.Label(inner, text=src, bg=BG, fg=MUTED, font=(FAMILY, 9),
                     wraplength=wl, justify="left", anchor="w").pack(
                fill="x", anchor="w")
        tk.Label(inner, text=trans, bg=BG, fg=TEXT,
                 font=(FAMILY, int(self.app.cfg.get("font_size", 13))),
                 wraplength=wl, justify="left", anchor="w").pack(
            fill="x", anchor="w", pady=(4 if src else 0, 0))
        tk.Label(inner, text=engine, bg=BG, fg=DIM,
                 font=(FAMILY, 8), anchor="w").pack(fill="x", pady=(4, 0))

        win.update_idletasks()
        w = min(wl + 26, max(140, wrap.winfo_reqwidth()))
        h = wrap.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = cx + 20
        y = cy + 24
        if x + w > sw - 8:
            x = max(8, cx - w - 20)
        if y + h > sh - 8:
            y = max(8, cy - h - 16)
        win.geometry("%dx%d+%d+%d" % (w, h, int(x), int(y)))
        self.app._round_win(win, 12)

        self.last_trans = trans
        for widget in (wrap, inner):
            widget.bind("<Button-1>", lambda e: self._copy())
        win.bind("<Button-1>", lambda e: self._copy())

        self.visible = True
        self.shown_at = time.time()
        self.job = self.app.root.after(9000, self.hide)

    def _copy(self):
        try:
            self.app._ignore_clip = self.last_trans
            self.app._last_clip = self.last_trans
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(self.last_trans)
            self.app._set_status("译文已复制")
        except Exception:
            pass
        self.hide()

    def hide(self):
        if self.job:
            try:
                self.app.root.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        self.visible = False
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


# --------------------------------------------------------------------------- 主程序


class FloatTrans:
    def __init__(self, app_socket):
        self.cfg = load_cfg()
        self.translator = Translator()
        self.q: queue.Queue = queue.Queue()
        self.req_id = 0
        self.history: list[str] = []
        self.hist_idx = 0
        self.last_src = ""
        self.last_result = ""
        self.last_tts = ""
        self._last_clip = ""
        self._ignore_clip = ""
        self._drag = None
        self._moved = False
        self._sizing = False
        self._size_job = None
        self._save_job = None
        self._rounding = False
        self.settings_win = None
        self.show_event = threading.Event()
        self.shot_event = threading.Event()
        self.hover_event = threading.Event()
        self.hotkey = HotkeyListener({
            "show": self.show_event,
            "shot": self.shot_event,
            "hover": self.hover_event,
        })
        self.app_socket = app_socket

        # ---- 取词相关
        self._selector = None        # 正在框选的覆盖层
        self._shot = None            # 框选用的整屏图 (bgra, w, h)
        self._hover_anchor = None
        self._hover_busy = False
        self._hover_pos = None
        self.hq: queue.Queue = queue.Queue()
        self.popup = HoverPopup(self)

        self.W = DEFAULT_W
        self._build_root()
        self.root.report_callback_exception = self._on_tk_error
        self._build_ui()

        self._apply_position()
        self._start_threads()

        # 启动时不立刻翻译剪贴板里已有的内容（避免把上次复制的东西发出去）
        try:
            self._last_clip = (self.root.clipboard_get() or "").strip()
        except Exception:
            self._last_clip = ""

        self.root.after(40, self._drain_queue)
        self.root.after(700, self._watch_clipboard)
        self.root.after(200, lambda: self._round(16))
        self.root.after(60, self._autosize)
        self.root.after(400, self._hover_tick)
        # 启动时不抢焦点，免得打断正在播的电影


    def _on_tk_error(self, exc, val, tb):
        try:
            import traceback
            log("TK ERROR:\n" + "".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass

    def _schedule_save(self):
        """合并写盘，避免拖滑块时疯狂读写配置文件。"""
        if self._save_job:
            return
        self._save_job = self.root.after(500, self._do_save)

    def _do_save(self):
        self._save_job = None
        save_cfg(self.cfg)

    def _flush_save(self):
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        save_cfg(self.cfg)

    # ------------------------------------------------------------------ 窗口
    def _build_root(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.cfg["alpha"]))
        try:
            icon = resource_path("app.ico")
            if os.path.exists(icon):
                self.root.iconbitmap(icon)
        except Exception:
            pass
        self.root.geometry("%dx%d" % (self.W, 190))
        self.root.bind("<Escape>", lambda e: self.collapse())
        self.root.bind("<Control-MouseWheel>", self._zoom)

    def _apply_position(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if x is None or y is None or not (0 <= x < sw - 80) or not (0 <= y < sh - 80):
            x = sw - self.W - 70
            y = 100
        self.root.geometry("+%d+%d" % (int(x), int(y)))

    def _round(self, radius: int):
        """用 Windows 区域 API 做圆角，避免直角突兀。macOS 由系统处理圆角。"""
        if _IS_MAC:
            return
        if self._rounding:
            return
        self._rounding = True
        try:
            u = ctypes.windll.user32
            g = ctypes.windll.gdi32
            wid = self.root.winfo_id()
            parent = u.GetParent(wid)
            hwnd = wid
            if parent:
                buf = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(parent, buf, 64)
                if buf.value == "TkTopLevel":
                    hwnd = parent
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            rgn = g.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
            u.SetWindowRgn(hwnd, rgn, True)
        except Exception as e:
            log("round fail %r" % (e,))
        finally:
            self._rounding = False

    def _start_threads(self):
        self.hotkey.start()
        threading.Thread(target=self.translator.probe_google, daemon=True).start()
        threading.Thread(target=self._accept_commands, daemon=True).start()
        self.root.after(1500, self._show_hotkey_hint)

    def _show_hotkey_hint(self):
        if self.hotkey.label:
            hint = self.hotkey.label + " 呼出"
            shot = self.hotkey.labels.get("shot")
            if shot:
                hint += " · " + shot + " 截图"
            self.status_hint.configure(text=hint)
        else:
            self.status_hint.configure(text="热键未注册")

    def _accept_commands(self):
        while True:
            try:
                conn, _ = self.app_socket.accept()
                data = conn.recv(64)
                conn.close()
                if b"show" in data:
                    self.show_event.set()
            except Exception:
                time.sleep(0.5)

    # ------------------------------------------------------------------ 界面
    def _build_ui(self):
        self.container = tk.Frame(self.root, bg=BG,
                                  highlightbackground=BORDER, highlightthickness=1)
        self.container.pack(fill="both", expand=True)

        self.expanded = tk.Frame(self.container, bg=BG)
        self.expanded.pack(fill="both", expand=True)

        self._build_header()
        self._build_body()
        self._build_status()

        self.ball = tk.Canvas(self.container, bg=BG, highlightthickness=0, bd=0,
                              cursor="hand2")
        self._draw_ball()
        self.ball.bind("<Configure>", self._draw_ball)
        for w in (self.ball,):
            w.bind("<Button-1>", self._ball_press)
            w.bind("<B1-Motion>", self._ball_move)
            w.bind("<ButtonRelease-1>", self._ball_release)

    def _draw_ball(self, _e=None):
        w = self.ball.winfo_width() or 54
        h = self.ball.winfo_height() or 54
        self.ball.delete("all")
        self.ball.create_oval(2, 2, w - 2, h - 2, fill="#1b2030",
                              outline=ACCENT, width=2)
        self.ball.create_text(w / 2, h / 2, text="译", fill=ACCENT,
                              font=(FAMILY, 16, "bold"))

    def _build_header(self):
        head = tk.Frame(self.expanded, bg=BG, height=32)
        head.pack(fill="x", padx=12, pady=(10, 2))
        head.pack_propagate(False)

        logo = tk.Label(head, text="译", bg=ACCENT, fg="#ffffff",
                        font=(FAMILY, 9, "bold"), padx=5, pady=1)
        logo.pack(side="left", padx=(0, 8))
        title = tk.Label(head, text=APP_TITLE, bg=BG, fg=MUTED,
                         font=(FAMILY, 9))
        title.pack(side="left")

        self.btn_shot = self._btn(head, "截图", self.start_region_shot)
        self.btn_shot.pack(side="left", padx=(12, 0))
        self.btn_hover = tk.Label(head, text="悬停", bg=BG, fg=MUTED,
                                  font=(FAMILY, 9), cursor="hand2", padx=4)
        self.btn_hover.pack(side="left", padx=(4, 0))
        self.btn_hover.bind("<Button-1>", lambda e: self.toggle_hover())
        self.btn_hover.bind("<Enter>",
                            lambda e: self.btn_hover.configure(fg=ACCENT_HI))
        self.btn_hover.bind("<Leave>", lambda e: self._refresh_hover_btn())
        self._refresh_hover_btn()

        btn_close = self._btn(head, "✕", self.quit, hover=ERROR)
        btn_close.pack(side="right")
        btn_min = self._btn(head, "─", self.collapse)
        btn_min.pack(side="right", padx=(0, 2))
        self.btn_pin = self._btn(head, "⚙", self.open_settings)
        self.btn_pin.pack(side="right", padx=(0, 2))

        for w in (head, logo, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.configure(cursor="fleur")

    def _build_body(self):
        body = tk.Frame(self.expanded, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 0))

        row = tk.Frame(body, bg=BG_INPUT)
        row.pack(fill="x")

        self.entry = PlaceholderEntry(
            row, "输入 / 粘贴 单词或句子…",
            bg=BG_INPUT, fg=TEXT, insertbackground=ACCENT,
            relief="flat", bd=0, font=(FAMILY, 11),
            selectbackground="#33507f", selectforeground="#ffffff")
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(10, 4))
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Up>", lambda e: self._history(-1))
        self.entry.bind("<Down>", lambda e: self._history(1))
        self.entry.bind("<Control-v>", lambda e: self.root.after(60, self._translate_from_entry))

        go = tk.Label(row, text="译", bg=ACCENT, fg="#ffffff",
                      font=(FAMILY, 11, "bold"), padx=14, cursor="hand2")
        go.pack(side="right", fill="y")
        go.bind("<Button-1>", lambda e: self._translate_from_entry())
        go.bind("<Enter>", lambda e: go.configure(bg=ACCENT_HI))
        go.bind("<Leave>", lambda e: go.configure(bg=ACCENT))

        self.lbl_src = tk.Label(body, text="", bg=BG, fg=DIM,
                                font=(FAMILY, 9), anchor="w", justify="left",
                                wraplength=self.W - 32)
        self.lbl_src.pack(fill="x", pady=(8, 0))
        self.lbl_src.pack_forget()

        size0 = int(self.cfg["font_size"])
        self.result_font = tkfont.Font(family=FAMILY, size=size0)
        self.fonts = {
            "trans": self.result_font,
            "phon": tkfont.Font(family=FAMILY, size=max(9, size0 - 4)),
            "pos": tkfont.Font(family=FAMILY, size=max(8, size0 - 4)),
            "def": tkfont.Font(family=FAMILY, size=max(9, size0 - 2)),
        }
        self.txt_holder = tk.Frame(body, bg=BG, width=self.W - 24, height=26)
        self.txt_holder.pack(fill="x", pady=(4, 0))
        self.txt_holder.pack_propagate(False)

        self.txt = tk.Text(self.txt_holder, height=1, width=1, wrap="word",
                           bg=BG, fg=TEXT, relief="flat", bd=0,
                           highlightthickness=0, padx=0, pady=0,
                           spacing1=1, spacing3=2, cursor="arrow",
                           font=self.result_font,
                           selectbackground="#33507f", selectforeground="#ffffff")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("trans", foreground=TEXT, font=self.fonts["trans"])
        self.txt.tag_configure("phon", foreground=MUTED, font=self.fonts["phon"])
        self.txt.tag_configure("pos", foreground=ACCENT, font=self.fonts["pos"])
        self.txt.tag_configure("def", foreground="#b9c2d6", font=self.fonts["def"])
        self.txt.tag_configure("hint", foreground=DIM,
                               font=(FAMILY, max(9, int(self.cfg["font_size"]) - 3)))
        self.txt.tag_configure("err", foreground=ERROR)
        self.txt.configure(state="disabled")
        self.blocks = [("在任意软件里选中文字 Ctrl+C，这里会自动翻译", "hint")]
        self.txt.configure(state="normal")
        self.txt.insert("1.0", self.blocks[0][0], "hint")
        self.txt.configure(state="disabled")
        self.txt.bind("<Double-Button-1>", lambda e: self.copy_result())
        self.txt.bind("<MouseWheel>", self._on_wheel)

        self.body = body

    def _build_status(self):
        bar = tk.Frame(self.expanded, bg=BG)
        bar.pack(fill="x", padx=12, pady=(0, 8))

        self.status_hint = tk.Label(bar, text="", bg=BG, fg=DIM,
                                    font=(FAMILY, 8), anchor="w")
        self.status_hint.pack(side="left")
        self.status_left = tk.Label(bar, text="就绪", bg=BG, fg=DIM,
                                    font=(FAMILY, 8), anchor="w")
        self.status_left.pack(side="left", padx=(8, 0))

        self.btn_copy = self._btn(bar, "复制", self.copy_result, size=8)
        self.btn_copy.pack(side="right")
        self.btn_speak = self._btn(bar, "发音", self.speak, size=8)
        self.btn_speak.pack(side="right", padx=(0, 6))
        self.btn_clip = self._btn(bar, "自动", self.toggle_clipboard, size=8)
        self.btn_clip.pack(side="right", padx=(0, 10))
        self._refresh_clip_btn()

    def _btn(self, parent, text, cmd, size=9, hover=ACCENT_HI, color=None):
        color = color or MUTED
        lbl = tk.Label(parent, text=text, bg=BG, fg=color,
                       font=(FAMILY, size), cursor="hand2", padx=4)
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.configure(fg=hover))
        lbl.bind("<Leave>", lambda e: lbl.configure(fg=color))
        return lbl

    # ------------------------------------------------------------------ 拖拽
    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self._moved = False

    def _drag_move(self, e):
        if not self._drag:
            return
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.root.geometry("+%d+%d" % (x, y))
        self._moved = True

    def _drag_end(self, _e):
        if self._drag and self._moved:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            self._schedule_save()
        self._drag = None

    def _ball_press(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self._moved = False

    def _ball_move(self, e):
        self._drag_move(e)

    def _ball_release(self, e):
        moved = self._moved
        self._drag_end(e)
        if not moved:
            self.expand()

    # ------------------------------------------------------------------ 收起 / 展开
    def collapse(self):
        if not self.expanded.winfo_ismapped():
            return
        self.cfg["x"] = self.root.winfo_x()
        self.cfg["y"] = self.root.winfo_y()
        self.cfg["collapsed"] = True
        self._schedule_save()
        self.expanded.pack_forget()
        self.ball.pack(fill="both", expand=True)
        x, y = self._clamp(self.root.winfo_x(), self.root.winfo_y(), 54, 54)
        self.root.geometry("54x54+%d+%d" % (x, y))
        self.root.after(30, lambda: self._round(27))

    def expand(self):
        self.cfg["collapsed"] = False
        self._schedule_save()
        self.ball.pack_forget()
        self.expanded.pack(fill="both", expand=True)
        x, y = self._clamp(self.root.winfo_x(), self.root.winfo_y(), self.W, 190)
        self.root.geometry("%dx%d+%d+%d" % (self.W, 190, x, y))
        self.root.after(30, lambda: (self._round(16), self._autosize()))
        self.root.lift()
        self.root.focus_force()
        self.entry.focus_set()

    def _clamp(self, x, y, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, min(int(x), sw - w - 4))
        y = max(0, min(int(y), sh - h - 60))
        return x, y

    # ------------------------------------------------------------------ 翻译
    def _on_enter(self, _e=None):
        self._translate_from_entry()
        return "break"

    def _translate_from_entry(self):
        text = self.entry.get_text().strip()
        if text:
            self.translate(text)

    def translate(self, text: str):
        text = text.strip()
        if not text:
            return
        self.req_id += 1
        rid = self.req_id
        self.last_src = text
        self._set_status("翻译中…")
        if not self.history or self.history[0] != text:
            self.history.insert(0, text)
            del self.history[40:]
        self.hist_idx = 0
        threading.Thread(target=self._work, args=(rid, text), daemon=True).start()

    def _work(self, rid, text):
        t0 = time.time()
        try:
            res = self.translator.translate(text)
            res.elapsed = time.time() - t0
            self.q.put(("ok", rid, res))
        except Exception as e:
            log("translate fail %r" % (e,))
            self.q.put(("err", rid, str(e)))

    def _drain_queue(self):
        try:
            while True:
                kind, rid, payload = self.q.get_nowait()
                if kind == "ocr":
                    self._after_ocr(payload)
                    continue
                if rid != self.req_id:
                    continue
                if kind == "ok":
                    self._render(payload)
                else:
                    self._render_error(payload)
        except queue.Empty:
            pass

        try:
            while True:
                kind, pos, payload = self.hq.get_nowait()
                self._hover_busy = False
                if kind == "ok" and pos:
                    src, res = payload
                    self._show_hover(pos[0], pos[1], src, res)
        except queue.Empty:
            pass

        if self.show_event.is_set():
            self.show_event.clear()
            self.show_window()
        if self.shot_event.is_set():
            self.shot_event.clear()
            self.start_region_shot()
        if self.hover_event.is_set():
            self.hover_event.clear()
            self.toggle_hover()
        self.root.after(40, self._drain_queue)

    def _render(self, res: Result):
        self.last_result = res.text
        self.last_tts = res.tts_url
        blocks = [(res.text, "trans")]
        if res.phonetic:
            blocks.append((res.phonetic, "phon"))
        for d in res.defs[:2]:
            d = trim_def(d)
            m = POS_RE.match(d)
            if m:
                blocks.append((m.group(1), "pos"))
                blocks.append((m.group(2), "def"))
            else:
                blocks.append((d, "def"))
        self.blocks = blocks

        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for i, (text, tag) in enumerate(blocks):
            if i:
                self.txt.insert("end", "\n")
            self.txt.insert("end", text, tag)
        self.txt.configure(state="disabled")

        src = res.src if len(res.src) > 26 else ""
        self.lbl_src.configure(text=src)
        if src:
            self.lbl_src.pack(fill="x", pady=(8, 0), before=self.txt_holder)
        else:
            self.lbl_src.pack_forget()

        self._set_status("%s · %.2fs" % (res.engine, res.elapsed))
        self._autosize()

        if self.cfg.get("tts") and res.tts_url:
            play_url(res.tts_url, res.src)

    def _render_error(self, msg: str):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", msg, "err")
        self.txt.configure(state="disabled")
        self.blocks = [(msg, "trans")]
        self.lbl_src.configure(text="")
        self.lbl_src.pack_forget()
        self._set_status("翻译失败")
        self._autosize()

    def _autosize(self):
        if self._size_job:
            try:
                self.root.after_cancel(self._size_job)
            except Exception:
                pass
        self._size_job = self.root.after_idle(self._do_autosize)

    def _wrap_count(self, text: str, font, width: int) -> int:
        """按像素宽度模拟 Tk 的 word 换行，算出这段文字占几行。"""
        total = 0
        for para in text.split("\n"):
            if not para:
                total += 1
                continue
            cur = 0
            lines = 1
            for tok in re.findall(r"\S+\s*|\s+", para):
                w = font.measure(tok)
                if cur + w <= width:
                    cur += w
                    continue
                if cur > 0:
                    lines += 1
                    cur = 0
                if w <= width:
                    cur = w
                    continue
                for ch in tok:
                    cw = font.measure(ch)
                    if cur + cw > width and cur > 0:
                        lines += 1
                        cur = 0
                    cur += cw
            total += lines
        return max(1, total)

    def _do_autosize(self):
        self._size_job = None
        if self._sizing or not self.expanded.winfo_ismapped():
            return
        self._sizing = True
        try:
            width = max(80, self.W - 28)
            # Tk 的 Text 每一行都按控件默认字体的行高排版，与 tag 字体无关
            line_h = int(self.result_font.metrics("linespace")) + 3
            total_lines = 0
            for text, tag in getattr(self, "blocks", [("", "trans")]):
                font = self.fonts.get(tag, self.result_font)
                total_lines += self._wrap_count(text, font, width)
            self.txt_holder.configure(height=max(line_h, total_lines * line_h + 2))
            self.root.update_idletasks()
            h = max(MIN_H, min(self.expanded.winfo_reqheight(), MAX_H))
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self.root.geometry("%dx%d+%d+%d" % (self.W, h, x, y))
        except Exception as e:
            log("autosize fail %r" % (e,))
        finally:
            self._sizing = False
        self.root.after(20, lambda: self._round(16))

    def _set_status(self, text: str):
        self.status_left.configure(text=text)

    def _on_wheel(self, e):
        self.txt.yview_scroll(-1 * (e.delta // 120), "units")

    # ------------------------------------------------------------------ 其它动作
    def copy_result(self):
        if not self.last_result:
            return
        self._ignore_clip = self.last_result
        self._last_clip = self.last_result
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_result)
        self._set_status("已复制")
        self.root.after(1400, lambda: self._set_status("就绪"))

    def speak(self):
        if self.last_tts:
            play_url(self.last_tts, self.last_src)
        elif self.last_src:
            url = ("https://dict.youdao.com/dictvoice?type=2&audio="
                   + urllib.parse.quote(self.last_src[:120]))
            play_url(url, self.last_src)

    def toggle_clipboard(self):
        self.cfg["clipboard"] = not self.cfg["clipboard"]
        self._schedule_save()
        self._refresh_clip_btn()

    def _refresh_clip_btn(self):
        on = bool(self.cfg.get("clipboard"))
        color = ACCENT if on else DIM
        self.btn_clip.configure(fg=color)
        self.btn_clip.configure(text="自动" if on else "手动")
        self.btn_clip.bind("<Leave>", lambda e: self.btn_clip.configure(fg=color))

    def _history(self, step: int):
        if not self.history:
            return "break"
        self.hist_idx = max(0, min(len(self.history) - 1, self.hist_idx + step))
        self.entry.set_text(self.history[self.hist_idx])
        return "break"

    def _zoom(self, e):
        size = int(self.cfg["font_size"]) + (1 if e.delta > 0 else -1)
        size = max(10, min(22, size))
        self.cfg["font_size"] = size
        self._schedule_save()
        self._apply_font_size()

    def _apply_font_size(self):
        size = int(self.cfg["font_size"])
        self.fonts["trans"].configure(size=size)
        self.fonts["phon"].configure(size=max(9, size - 4))
        self.fonts["pos"].configure(size=max(8, size - 4))
        self.fonts["def"].configure(size=max(9, size - 2))
        self._autosize()

    # ------------------------------------------------------------------ 剪贴板
    def _watch_clipboard(self):
        self.root.after(650, self._watch_clipboard)
        if not self.cfg.get("clipboard"):
            return
        if not self.expanded.winfo_ismapped():
            return
        try:
            txt = self.root.clipboard_get()
        except Exception:
            return
        if not isinstance(txt, str):
            return
        txt = txt.strip()
        if not txt or txt == self._last_clip or txt == self._ignore_clip:
            return
        self._last_clip = txt
        if len(txt) > 400 or txt.count("\n") > 6:
            return
        if txt == self.last_src:
            return
        self.entry.set_text(txt)
        self.translate(txt)

    # ------------------------------------------------------------------ 显示
    def show_window(self):
        if not self.expanded.winfo_ismapped():
            self.expand()
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def quit(self):
        self._flush_save()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------ 设置
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return

        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.overrideredirect(True)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.geometry("300x330")
        x = self.root.winfo_x() + self.W + 10
        y = self.root.winfo_y()
        sw = win.winfo_screenwidth()
        if x + 300 > sw:
            x = max(20, self.root.winfo_x() - 310)
        win.geometry("+%d+%d" % (x, y))

        wrap = tk.Frame(win, bg=BG, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True)

        head = tk.Frame(wrap, bg=BG, height=34)
        head.pack(fill="x", padx=14, pady=(10, 0))
        head.pack_propagate(False)
        tk.Label(head, text="设置", bg=BG, fg=TEXT, font=(FAMILY, 10, "bold")).pack(side="left")
        close = self._btn(head, "✕", win.destroy, hover=ERROR)
        close.pack(side="right")
        for w in (head,):
            w.bind("<Button-1>", lambda e: self._win_drag(win, e, "s"))
            w.bind("<B1-Motion>", lambda e: self._win_drag(win, e, "m"))

        def section(title, value_text=""):
            f = tk.Frame(wrap, bg=BG)
            f.pack(fill="x", padx=16, pady=(10, 0))
            tk.Label(f, text=title, bg=BG, fg=MUTED, font=(FAMILY, 9)).pack(side="left")
            v = tk.Label(f, text=value_text, bg=BG, fg=ACCENT, font=(FAMILY, 9))
            v.pack(side="right")
            return v

        # 透明度
        v_alpha = section("不透明度", "%d%%" % int(float(self.cfg["alpha"]) * 100))
        MiniSlider(wrap, 40, 100, int(float(self.cfg["alpha"]) * 100),
                   lambda v: self._set_alpha(v, v_alpha)).pack(
            fill="x", padx=14, pady=(4, 2))

        # 字号
        v_font = section("字号", str(self.cfg["font_size"]))
        MiniSlider(wrap, 10, 22, int(self.cfg["font_size"]),
                   lambda v: self._set_font(v, v_font)).pack(
            fill="x", padx=14, pady=(4, 2))

        self._check(wrap, "复制文字后自动翻译", "clipboard")
        self._check(wrap, "查词自动发音", "tts")
        self._check(wrap, "Alt 悬停取词", "hover",
                    lambda on: self._refresh_hover_btn())
        self._check(wrap, "开机自动启动", "autostart", self._toggle_autostart)

        tip = tk.Frame(wrap, bg=BG)
        tip.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(tip, text="呼出热键", bg=BG, fg=MUTED, font=(FAMILY, 9)).pack(side="left")
        tk.Label(tip, text=self.hotkey.label or "未注册",
                 bg=BG, fg=ACCENT, font=(FAMILY_MONO, 9)).pack(side="right")

        tip2 = tk.Frame(wrap, bg=BG)
        tip2.pack(fill="x", padx=16, pady=(5, 0))
        tk.Label(tip2, text="截图热键", bg=BG, fg=MUTED,
                 font=(FAMILY, 9)).pack(side="left")
        tk.Label(tip2, text=self.hotkey.labels.get("shot") or "未注册",
                 bg=BG, fg=ACCENT, font=(FAMILY_MONO, 9)).pack(side="right")

        foot = tk.Frame(wrap, bg=BG)
        foot.pack(fill="x", padx=16, pady=(14, 0))
        reset = tk.Label(foot, text="重置位置", bg=BG_SOFT, fg=MUTED,
                         font=(FAMILY, 9), padx=10, pady=5, cursor="hand2")
        reset.pack(side="left")
        reset.bind("<Button-1>", lambda e: self._reset_pos())
        tk.Label(foot, text="v" + VERSION, bg=BG, fg=DIM,
                 font=(FAMILY, 8)).pack(side="right")

        win.update_idletasks()
        win.geometry("300x%d" % max(300, wrap.winfo_reqheight()))
        win.after(30, lambda: self._round_win(win, 14))
        win.bind("<Escape>", lambda e: win.destroy())

    def _win_drag(self, win, e, mode):
        if mode == "s":
            win._d = (e.x_root - win.winfo_x(), e.y_root - win.winfo_y())
        elif getattr(win, "_d", None):
            win.geometry("+%d+%d" % (e.x_root - win._d[0], e.y_root - win._d[1]))

    def _round_win(self, win, radius):
        if _IS_MAC:
            return
        try:
            win.update_idletasks()
            u = ctypes.windll.user32
            wid = win.winfo_id()
            parent = u.GetParent(wid)
            hwnd = wid
            if parent:
                buf = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(parent, buf, 64)
                if buf.value == "TkTopLevel":
                    hwnd = parent
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, win.winfo_width() + 1, win.winfo_height() + 1,
                radius * 2, radius * 2)
            u.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    def _check(self, parent, text, key, cmd=None):
        on = bool(self.cfg.get(key))
        row = tk.Frame(parent, bg=BG, cursor="hand2")
        row.pack(fill="x", padx=14, pady=(9, 0))
        box = tk.Label(row, text="☑" if on else "☐", bg=BG,
                       fg=ACCENT if on else DIM, font=(FAMILY, 12), cursor="hand2")
        box.pack(side="left")
        lbl = tk.Label(row, text=text, bg=BG, fg=TEXT, font=(FAMILY, 9),
                       cursor="hand2")
        lbl.pack(side="left", padx=(8, 0))

        def toggle(_e=None):
            val = not bool(self.cfg.get(key))
            self.cfg[key] = val
            box.configure(text="☑" if val else "☐", fg=ACCENT if val else DIM)
            self._schedule_save()
            if cmd:
                cmd(val)
            if key == "clipboard":
                self._refresh_clip_btn()

        for w in (row, box, lbl):
            w.bind("<Button-1>", toggle)
        return row

    def _toggle_autostart(self, on):
        if not set_autostart(on):
            self._set_status("设置开机启动失败")

    def _set_alpha(self, value, label):
        label.configure(text="%d%%" % value)
        self.cfg["alpha"] = value / 100.0
        self.root.attributes("-alpha", value / 100.0)
        self._schedule_save()

    def _set_font(self, value, label):
        label.configure(text=str(value))
        self.cfg["font_size"] = value
        self._schedule_save()
        self._apply_font_size()

    def _reset_pos(self):
        sw = self.root.winfo_screenwidth()
        self.cfg["x"] = sw - self.W - 70
        self.cfg["y"] = 100
        self._schedule_save()
        self.root.geometry("+%d+%d" % (self.cfg["x"], self.cfg["y"]))

    # ------------------------------------------------------------------ 截图取词
    def start_region_shot(self):
        if so is None:
            self._set_status("取词模块缺失")
            return
        why = so.available()
        if why:
            self._set_status("OCR 不可用：" + why)
            return
        if self._selector is not None:
            return
        self.hide_hover()
        try:
            bgra, vw, vh = so.capture()
        except Exception as e:
            log("capture fail %r" % (e,))
            self._set_status("抓屏失败")
            return
        vx, vy, _, _ = so.virtual_screen()
        self._shot = (bgra, vw, vh)
        try:
            self._selector = RegionSelector(
                self.root, bgra, vx, vy, vw, vh,
                self._on_region, self._on_region_cancel)
        except Exception as e:
            log("selector fail %r" % (e,))
            self._selector = None
            self._shot = None
            self._set_status("框选窗口打开失败")
            return
        self._set_status("拖动框选要翻译的区域…")

    def _on_region_cancel(self):
        self._selector = None
        self._shot = None
        self._set_status("已取消")

    def _on_region(self, x, y, w, h):
        self._selector = None
        shot = self._shot
        self._shot = None
        if not shot:
            return
        bgra, vw, vh = shot
        self._set_status("识别中…")

        def work():
            try:
                sub = so.crop(bgra, vw, x, y, w, h)
                txt, _lines = so.ocr_bgra(sub, w, h)
                text = so.clean_text(txt)
                self.q.put(("ocr", 0, (text, None, (w, h))))
            except Exception as e:
                log("ocr fail %r" % (e,))
                self.q.put(("ocr", 0, ("", str(e), (w, h))))

        threading.Thread(target=work, daemon=True).start()

    def _after_ocr(self, payload):
        text, err, size = payload
        w, h = size
        if err:
            self._set_status("识别失败：" + err)
            return
        if not text:
            if h < 18 or w < 18:
                self._set_status("框太小了，把整行文字都框进去")
            else:
                self._set_status("没认出文字，框松一点再试")
            return
        flat = " ".join(text.split())
        show = flat if len(flat) <= 60 else flat[:60] + "…"
        self.entry.set_text(show)
        self.show_window()
        self.translate(text)
        self._set_status("截图识别 %d 字" % len(text))

    # ------------------------------------------------------------------ 悬停取词
    def toggle_hover(self):
        if so is None or so.available():
            self._set_status("取词不可用")
            return
        on = not bool(self.cfg.get("hover"))
        self.cfg["hover"] = on
        self._schedule_save()
        self._refresh_hover_btn()
        if on:
            self._set_status("悬停取词已开：按住 Alt 停在文字上")
        else:
            self.hide_hover()
            self._set_status("悬停取词已关")

    def _refresh_hover_btn(self):
        if not hasattr(self, "btn_hover"):
            return
        self.btn_hover.configure(
            fg=ACCENT if self.cfg.get("hover") else MUTED)

    def _hover_tick(self):
        self.root.after(140, self._hover_tick)
        if so is None or not self.cfg.get("hover"):
            self._hover_anchor = None
            return
        if self._selector is not None or self._hover_busy:
            return
        pos = so.cursor_pos()
        if not pos:
            return
        x, y = pos
        down = so.modifier_down(self.cfg.get("hover_mod") or "alt")

        if self.popup.visible:
            # 松开修饰键就收起气泡，留一点余量免得一闪而过
            if not down and time.time() - self.popup.shown_at > 0.4:
                self.hide_hover()
            # 鼠标移开原位也收起，方便接着扫下一行
            elif self._hover_pos and (
                    abs(x - self._hover_pos[0]) > 90
                    or abs(y - self._hover_pos[1]) > 60):
                self.hide_hover()
            return

        if not down:
            self._hover_anchor = None
            return
        if so.cursor_pid(x, y) == os.getpid():
            self._hover_anchor = None
            return
        if self._hover_anchor is None:
            self._hover_anchor = (x, y, time.time())
            return
        ax, ay, at = self._hover_anchor
        if abs(x - ax) > 6 or abs(y - ay) > 6:
            self._hover_anchor = (x, y, time.time())
            return
        if time.time() - at < 0.45:
            return
        if self._hover_pos and abs(x - self._hover_pos[0]) < 24 \
                and abs(y - self._hover_pos[1]) < 18:
            return
        self._hover_pos = (x, y)
        self._hover_anchor = None
        self._hover_busy = True
        threading.Thread(target=self._hover_worker, args=(x, y),
                         daemon=True).start()

    @staticmethod
    def _hover_pick_line(lines, rel):
        """在候选行里挑出鼠标底下那一行，返回 (文本, 行框) 或 (None, None)。

        只按“行中心离鼠标多近”挑会被噪点带偏：截图里常混进侧栏图标、
        竖线之类的小碎块，它们又小又贴着鼠标，距离比正文还近。所以这里
        先剔掉小碎块和认不出字的东西，再让宽度参与打分——距离差几像素时
        优先选更宽的那行（多半是正文），距离差得多时仍然是最近的赢。
        """
        best = None
        best_score = None
        for L in lines:
            text = so.clean_text(L.get("text") or "")
            if not text:
                continue
            lw = max(1, int(L["w"]))
            lh = max(1, int(L["h"]))
            d = abs((L["y"] + lh / 2.0) - rel)
            # 小碎块基本都是图标 / 边框 / 侧栏残留，除非鼠标正压在上面
            if (lw < 16 or lh < 8) and d > 6:
                continue
            # 孤零零一个字也常是噪点
            if len(text.strip()) < 2 and d > 4:
                continue
            score = d - min(lw, 500) * 0.02
            if best_score is None or score < best_score:
                best_score = score
                best = (text, L)
        return best if best else (None, None)

    def _hover_worker(self, cx, cy):
        try:
            vx, vy, vw, vh = so.virtual_screen()
            bh = 80
            pick = ""
            for band in (1000, vw):
                bw = min(band, vw)
                x = max(vx, min(cx - bw // 2, vx + vw - bw))
                y = max(vy, min(cy - bh // 2, vy + vh - bh))
                bgra, w, h = so.capture(x, y, bw, bh)
                txt, lines = so.ocr_bgra(bgra, w, h)
                text = so.clean_text(txt)
                if not text:
                    continue
                pick = text
                edge = False
                if lines:
                    cand, near = self._hover_pick_line(lines, cy - y)
                    if cand:
                        pick = cand
                        # 这一行被截断了，换整屏宽度再来一次
                        edge = near["x"] <= 2 or near["x"] + near["w"] >= w - 2
                if not edge or bw >= vw:
                    break
            if not pick:
                self.hq.put(("none", None, None))
                return
            res = self.translator.translate(pick)
            self.hq.put(("ok", (cx, cy), (pick, res)))
        except Exception as e:
            log("hover fail %r" % (e,))
            self.hq.put(("none", None, None))

    def _show_hover(self, cx, cy, src, res):
        try:
            self.popup.show(cx, cy, src, res.text, res.engine)
        except Exception as e:
            log("popup fail %r" % (e,))

    def hide_hover(self):
        try:
            self.popup.hide()
        except Exception:
            pass


# --------------------------------------------------------------------------- 入口


def _mac_check_permissions():
    """macOS 首次运行：检测并引导授予屏幕录制 / 辅助功能权限。"""
    try:
        import screen_ocr as so
    except Exception:
        return
    try:
        ok = True
        check = getattr(so, "screen_permission_ok", None)
        if check is not None and not check():
            ok = False
            req = getattr(so, "request_screen_permission", None)
            if req is not None:
                try:
                    req()
                except Exception:
                    pass
        # 辅助功能权限（pynput 热键 / 修饰键监听用）
        try:
            from ApplicationServices import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                ok = False
        except Exception:
            pass
        if not ok:
            import tkinter.messagebox as mb
            mb.showinfo(
                "FloatTrans 需要系统权限",
                "首次运行请在「系统设置 → 隐私与安全性」中为 FloatTrans 开启：\n\n"
                "1. 屏幕录制（截图取词）\n"
                "2. 辅助功能（全局热键、悬停取词）\n\n"
                "授权后请重新启动 FloatTrans。")
    except Exception as e:
        log("perm check fail %r" % (e,))


def main():
    enable_dpi_awareness()
    if sys.platform == "darwin":
        _mac_check_permissions()

    def hook(exc_type, exc, tb):
        import traceback
        log("UNCAUGHT:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook

    sock = acquire_instance()
    if sock is None:
        return
    log("---- start v%s ----" % VERSION)
    app = FloatTrans(sock)
    try:
        app.root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
