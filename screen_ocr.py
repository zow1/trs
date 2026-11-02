#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
屏幕取词底层模块
================
* 用 GDI BitBlt 抓屏（纯 ctypes，不依赖 Pillow）
* 用 Windows 自带的 OCR 引擎识别（winsdk），离线、免安装、几十毫秒出结果
* 对识别结果做清洗：去掉中文字间空格、修正常见的拉丁字母误识别

对外接口
--------
    virtual_screen()            -> (x, y, w, h)   虚拟桌面（多屏）范围
    capture(x, y, w, h)         -> (bgra, w, h)   抓屏，BGRA 原始像素
    crop(bgra, sw, x, y, w, h)  -> bgra           从已抓好的大图里裁一块
    write_bmp32(path, bgra, w, h)                 写 32 位 BMP（给 OCR 用）
    write_ppm(path, bgra, w, h)                   写 PPM（给 tkinter 显示用）
    ocr_bgra(bgra, w, h)        -> (text, lines)  lines = [{"text","x","y","w","h"}]
    clean_text(text)            -> str            文本清洗
    cursor_pid(x, y)            -> int            鼠标下窗口所属进程 pid
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import tempfile
import threading
from ctypes import wintypes

# --------------------------------------------------------------------------- GDI

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0

H = ctypes.c_void_p


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateCompatibleDC.restype = H
gdi32.CreateCompatibleDC.argtypes = [H]
gdi32.CreateDIBSection.restype = H
gdi32.CreateDIBSection.argtypes = [H, ctypes.POINTER(BITMAPINFO), ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p), H, ctypes.c_uint]
gdi32.SelectObject.restype = H
gdi32.SelectObject.argtypes = [H, H]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         H, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.DeleteObject.argtypes = [H]
gdi32.DeleteDC.argtypes = [H]
user32.GetDC.restype = H
user32.GetDC.argtypes = [H]
user32.ReleaseDC.argtypes = [H, H]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


def virtual_screen():
    """整个虚拟桌面（含多显示器）的物理像素范围。"""
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        x, y = 0, 0
        w = user32.GetSystemMetrics(0) or 1920
        h = user32.GetSystemMetrics(1) or 1080
    return x, y, w, h


def capture(x=None, y=None, w=None, h=None):
    """抓一块屏幕，返回 (bgra_bytes, w, h)。"""
    vx, vy, vw, vh = virtual_screen()
    if x is None:
        x, y, w, h = vx, vy, vw, vh
    w = max(1, min(int(w), vw))
    h = max(1, min(int(h), vh))

    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h          # 负数 = 自上而下，省得再翻转
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = BI_RGB

    pbits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                  ctypes.byref(pbits), None, 0)
    old = gdi32.SelectObject(hdc, hbmp)
    try:
        gdi32.BitBlt(hdc, 0, 0, w, h, hdc_screen, int(x), int(y),
                     SRCCOPY | CAPTUREBLT)
        data = ctypes.string_at(pbits, w * h * 4)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, hdc_screen)
    return data, w, h


def crop(bgra, sw, x, y, w, h):
    """从一整屏的 bgra 里裁出一块（x/y 是相对大图左上角）。"""
    x = max(0, int(x))
    y = max(0, int(y))
    w = max(1, int(w))
    h = max(1, int(h))
    out = bytearray(w * h * 4)
    row = w * 4
    for r in range(h):
        src = ((y + r) * sw + x) * 4
        out[r * row:(r + 1) * row] = bgra[src:src + row]
    return bytes(out)


def pad_edge(bgra, w, h, nw, nh):
    """四周用边缘像素复制补到 nw×nh，原内容居中。

    比补纯黑/纯白更保险：不管背景是白的、黑的还是花的，
    补出来的边都和原图接得上，不会凭空多出一条对比强烈的色带。
    """
    if nw <= w and nh <= h:
        return bgra
    left = (nw - w) // 2
    top = (nh - h) // 2
    rb = w * 4
    nrb = nw * 4
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = min(max(y - top, 0), h - 1)
        base = y * nrb
        src = bgra[sy * rb:(sy + 1) * rb]
        out[base + left * 4:base + left * 4 + rb] = src
        if left:
            out[base:base + left * 4] = src[0:4] * left
        rn = nw - w - left
        if rn > 0:
            st = base + (left + w) * 4
            out[st:st + rn * 4] = src[-4:] * rn
    return bytes(out)


def bgra_to_rgb(bgra, w, h, level=1.0):
    """BGRA -> RGB，用扩展切片在 C 层完成，2M 像素也就几十毫秒。

    level < 1 时顺便压暗，一次遍历搞定。
    """
    n = w * h
    out = bytearray(n * 3)
    out[0::3] = bgra[2::4]
    out[1::3] = bgra[1::4]
    out[2::3] = bgra[0::4]
    if level < 1.0:
        table = bytes(max(0, min(255, int(i * level))) for i in range(256))
        out = out.translate(table)
    return bytes(out)


# --------------------------------------------------------------------------- 落盘

_TMP_DIR = None
_tmp_lock = threading.Lock()
_tmp_seq = [0]


def _tmp_path(ext):
    global _TMP_DIR
    with _tmp_lock:
        if _TMP_DIR is None:
            _TMP_DIR = tempfile.mkdtemp(prefix="floattrans_ocr_")
        _tmp_seq[0] += 1
        return os.path.join(_TMP_DIR, "s%d.%s" % (_tmp_seq[0], ext))


tmp_path = _tmp_path


def write_bmp32(path, bgra, w, h):
    """32 位 BMP：Windows 解码器直接认，而且 BGRA 可以原样写入，零转换。"""
    off = 54
    size = w * h * 4
    fh = b"BM" + (off + size).to_bytes(4, "little") + b"\x00\x00\x00\x00" + \
        off.to_bytes(4, "little")
    ih = (40).to_bytes(4, "little") + \
        w.to_bytes(4, "little", signed=True) + \
        (-h).to_bytes(4, "little", signed=True) + \
        (1).to_bytes(2, "little") + (32).to_bytes(2, "little") + \
        (0).to_bytes(4, "little") + size.to_bytes(4, "little") + \
        b"\x00" * 16
    with open(path, "wb") as f:
        f.write(fh)
        f.write(ih)
        f.write(bgra)


def write_ppm(path, bgra, w, h, level=1.0):
    """PPM(P6) 是 tkinter 原生支持的格式，不用 Pillow。"""
    rgb = bgra_to_rgb(bgra, w, h, level)
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(rgb)


# --------------------------------------------------------------------------- OCR

_engine = None
_engine_lock = threading.Lock()
_ocr_broken = None          # 记录导入/初始化失败的原因


def available() -> str:
    """返回 '' 表示可用，否则返回不可用的原因。"""
    global _ocr_broken
    if _ocr_broken is not None:
        return _ocr_broken
    try:
        import winsdk.windows.media.ocr  # noqa: F401
    except Exception as e:
        _ocr_broken = "缺少 winsdk（pip install winsdk）: %r" % (e,)
        return _ocr_broken
    return ""


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        langs = list(OcrEngine.available_recognizer_languages)
        order = ["en-US", "en-GB", "zh-Hans-CN", "zh-Hant-TW"]
        pick = None
        for want in order:
            for lang in langs:
                if lang.language_tag.lower() == want.lower():
                    pick = lang
                    break
            if pick:
                break
        if pick is None and langs:
            pick = langs[0]
        if pick is None:
            raise RuntimeError("系统没有安装任何 OCR 语言包")
        eng = OcrEngine.try_create_from_language(Language(pick.language_tag))
        if eng is None:
            raise RuntimeError("OCR 引擎创建失败: %s" % pick.language_tag)
        _engine = eng
        return _engine


def _recognize_sync(path):
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def run():
        f = await StorageFile.get_file_from_path_async(path)
        s = await f.open_async(FileAccessMode.READ)
        dec = await BitmapDecoder.create_async(s)
        bmp = await dec.get_software_bitmap_async()
        return await _get_engine().recognize_async(bmp)

    return asyncio.run(run())


MIN_OCR = 40        # Windows OCR 要求图片至少 40×40，小于这个尺寸直接返回空
PAD_MARGIN = 6      # 补边时多留一点余量，免得文字贴着边


def ocr_bgra(bgra, w, h):
    """识别一块 BGRA 图像，返回 (整段文本, 行列表)。

    行列表里的坐标是相对原图左上角的（补边的偏移已经扣掉）。
    """
    why = available()
    if why:
        raise RuntimeError(why)

    pad_l = pad_t = 0
    if w < MIN_OCR or h < MIN_OCR:
        nw = w if w >= MIN_OCR else max(w + PAD_MARGIN, MIN_OCR)
        nh = h if h >= MIN_OCR else max(h + PAD_MARGIN, MIN_OCR)
        pad_l = (nw - w) // 2
        pad_t = (nh - h) // 2
        bgra = pad_edge(bgra, w, h, nw, nh)
        w, h = nw, nh

    path = _tmp_path("bmp")
    try:
        write_bmp32(path, bgra, w, h)
        res = _recognize_sync(path)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    lines = []
    for line in res.lines:
        words = list(line.words)
        if not words:
            continue
        rects = [word.bounding_rect for word in words]
        x0 = min(int(r.x) for r in rects)
        y0 = min(int(r.y) for r in rects)
        x1 = max(int(r.x + r.width) for r in rects)
        y1 = max(int(r.y + r.height) for r in rects)
        lines.append({"text": join_words(words), "x": x0 - pad_l,
                      "y": y0 - pad_t, "w": x1 - x0, "h": y1 - y0})

    text = "\n".join(L["text"] for L in lines)
    return text, lines


_NO_GLUE_TAIL = ",.;:!?)]}\"'，。、；：？！》】"
_NO_GLUE_HEAD = ",.;:!?)]}\"'，。、；：？！《【"


def join_words(words):
    """把一行里的词拼回去。

    Windows OCR 经常把单词拆开（everyone -> "eve ryo n e"），
    但拆开的词之间几乎没有间隙，真正的空格间隙明显更大，
    所以用「中位间隙」当阈值，小于阈值的直接粘起来。
    """
    items = []
    for w in words:
        b = w.bounding_rect
        items.append((float(b.x), float(b.x + b.width), w.text))
    items.sort(key=lambda t: t[0])
    if len(items) == 1:
        return items[0][2]

    gaps = sorted(g for g in (items[i + 1][0] - items[i][1]
                              for i in range(len(items) - 1)) if g >= 0)
    thr = gaps[len(gaps) // 2] * 0.45 if gaps else 0.0

    out = [items[0][2]]
    for i in range(1, len(items)):
        gap = items[i][0] - items[i - 1][1]
        a, b = items[i - 1][2], items[i][2]
        glue = (gap < thr and a and b
                and a[-1] not in _NO_GLUE_TAIL
                and b[0] not in _NO_GLUE_HEAD)
        out.append(b if glue else " " + b)
    return "".join(out)


# --------------------------------------------------------------------------- 文本清洗

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_RE = re.compile("[%s]" % CJK)
CJK_PUNCT = "，。、；：？！“”‘’（）《》【】…—·"
LATIN_JUNK = "丨|¦︱│"

_LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
        "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
_PUNCT_MAP = {"，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
              "！": "!", "？": "?", "（": "(", "）": ")", "《": '"',
              "》": '"', "【": "[", "】": "]", "“": '"', "”": '"',
              "‘": "'", "’": "'", "—": "-", "…": "...", "·": "-"}


def _is_latin(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text))
    cjk = len(CJK_RE.findall(text))
    return latin >= cjk


def _fix_latin(s: str) -> str:
    for k, v in _LIG.items():
        s = s.replace(k, v)
    # OCR 常把 I / l 认成中文的竖线
    s = re.sub(r"(?<![A-Za-z])[丨|¦︱│](?![A-Za-z])", "I", s)
    s = re.sub(r"(?<=[A-Za-z])[丨|¦︱│]", "l", s)
    # 字母中间的 0 基本都该是 o
    s = re.sub(r"(?<=[A-Za-z])0(?=[A-Za-z])", "o", s)
    s = re.sub(r"(?<=[A-Za-z])0(?=\b)", "o", s)
    s = re.sub(r"\b0(?=[A-Za-z])", "O", s)
    # 小写词里混进来的大写 O 基本是 o
    s = re.sub(r"(?<=[a-z])O(?=[a-z])", "o", s)
    s = re.sub(r"(?<=[a-z])O(?=\b)", "o", s)
    s = re.sub(r"\b([A-Z])O(?=[a-z])", r"\1o", s)
    # 全角标点换成半角
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    s = s.replace("`", "'").replace("´", "'")
    # 英文句子里孤零零的汉字都是误识别，直接丢掉
    s = re.sub(r"(?<![%s])[%s](?![%s])" % (CJK, CJK, CJK), " ", s)
    # 去掉行首行尾的杂散符号
    s = re.sub(r"^[%s\s\-–—_,.;:'\"()\[\]]+" % CJK_PUNCT, "", s)
    s = re.sub(r"[%s]+$" % CJK_PUNCT, "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _fix_cjk(s: str) -> str:
    # 中文字之间被插了空格，全去掉；中文和英文之间保留一个空格
    s = re.sub(r"(?<=[%s])\s+(?=[%s])" % (CJK, CJK), "", s)
    s = re.sub(r"(?<=[%s])\s+(?=[，。、；：？！“”‘’（）《》【】…—·])" % CJK, "", s)
    s = re.sub(r"(?<=[，。、；：？！“”‘’（）《》【】…—·])\s+(?=[%s])" % CJK, "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def clean_text(text: str) -> str:
    """把 OCR 的原始结果收拾干净。"""
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = s.replace("\u3000", " ")
        if _is_latin(s):
            s = _fix_latin(s)
        else:
            s = _fix_cjk(s)
        if not s:
            continue
        # 只剩标点 / 单个杂符号的行丢掉
        if not re.search(r"[A-Za-z0-9%s]" % CJK, s):
            continue
        out.append(s)
    return "\n".join(out)


# --------------------------------------------------------------------------- 鼠标下是谁的窗口

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL

GA_ROOT = 2


def cursor_pid(x, y):
    """鼠标坐标下最顶层窗口属于哪个进程；拿不到返回 0。"""
    try:
        hwnd = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return 0
        root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def cursor_pos():
    pt = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


VK = {"alt": 0x12, "ctrl": 0x11, "shift": 0x10}


def modifier_down(name: str) -> bool:
    vk = VK.get(name or "alt")
    if not vk:
        return True
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def key_down(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
