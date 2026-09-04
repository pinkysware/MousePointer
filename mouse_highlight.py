# -*- coding: utf-8 -*-
"""鼠标指示工具 (Windows) — 白手套定位器

依赖: tkinter + Pillow + pywin32。

行为:
  1. 极简控制面板(可拖动): 标题栏 + 热键设置 + 退出按钮。
  2. 平时屏幕上没有任何覆盖层, 不影响正常操作。
  3. 按下全局热键(默认 F10): 在鼠标指针位置弹出一个米老鼠风格白手套,
     食指精确指向指针。
  4. 移动鼠标后手套自动消失; 也可再按一次热键手动隐藏。

技术要点(紫色边框的最终解决方案):
  手套窗口不再用 Tk Toplevel + Canvas, 改用 Win32 原生分层窗口
  (WS_EX_LAYERED) + UpdateLayeredWindow(ULW_ALPHA) 直接合成 PNG 的
  per-pixel alpha。完全绕开 Tk 渲染管线和颜色关键色匹配, 不存在
  "magenta 被合成成紫色" 的问题。

热键配置: 已内置到控制面板, 保存后即时生效。
"""
import tkinter as tk
from tkinter import ttk
import ctypes
from ctypes import wintypes
import json
import os
import sys
import subprocess
import threading
import time
import datetime
import locale

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import win32gui, win32ui, win32con, win32api
    HAS_PYWIN32 = True
except Exception:
    HAS_PYWIN32 = False

# ====== 配置 (按需修改) ======
GLOVE_H = 130          # 手套显示高度(px)
GLOVE_OFFSET_X = 20    # 指尖对齐微调(px): 正值手套右移 (指尖在指针右下角)
GLOVE_OFFSET_Y = 20    # 指尖放在指针正下方 20px (指尖 <-> 鼠标 不重合)
PANEL_BG = '#1e1e24'
# =============================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

root = None
panel_ui = {}

# GDI 手套窗口
glove_hwnd = None       # 分层窗口句柄
glove_bgra = None       # 预乘 alpha 的 BGRA 字节流 (bottom-up)
glove_pw = glove_ph = 0 # 像素尺寸
_gdi = {}               # screen_dc / mem_dc / bmp 句柄缓存

# 指尖在缩放后手套图中的相对位置 (0-1), 运行 load_glove 时自动算出
GLOVE_FINGER_X = 0.5
GLOVE_FINGER_Y = 0.0

glove_visible = False
anchor_x = anchor_y = 0
glove_show_time = 0
show_coord = False
stop = False
last_toggle_ts = 0  # 热键去抖时间戳

# ---- 路径解析 (兼容 PyInstaller 单文件打包) ----
# 资源文件(只读: 手套图/托盘图) 打包后解压到 _MEIPASS; 配置/日志(需读写) 放 exe 同目录。
def _app_dir():
    """可写数据目录: 打包后为 exe 所在目录, 源码运行时为脚本所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """只读资源目录: 打包后为 PyInstaller 的 _MEIPASS 临时目录。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', _app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """返回资源文件(只读)的绝对路径。"""
    return os.path.join(_resource_dir(), rel)


# ---- 热键配置 (单一按键: 显示/隐藏手套) ----
CONFIG_PATH = os.path.join(_app_dir(), 'hotkeys.json')
LOG_PATH = os.path.join(_app_dir(), 'mouse_highlight.log')

# 可选的单键列表 (label -> 虚拟键码)
KEY_OPTIONS = {}
for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    KEY_OPTIONS[c] = ord(c)
for i in range(10):
    KEY_OPTIONS[str(i)] = ord('0') + i
for i in range(1, 13):
    KEY_OPTIONS['F%d' % i] = 0x70 + (i - 1)
KEY_OPTIONS['Space'] = 0x20
KEY_OPTIONS['Esc'] = 0x1B
KEY_OPTIONS['Tab'] = 0x09
KEY_OPTIONS['Enter'] = 0x0D
KEY_OPTIONS['Up'] = 0x26
KEY_OPTIONS['Down'] = 0x28
KEY_OPTIONS['Left'] = 0x25
KEY_OPTIONS['Right'] = 0x27
KEY_OPTIONS['Menu'] = 0x5D   # 键盘右侧「菜单键」(VK_APPS)
# 标点/符号键 (108键键盘常用, VK 码固定, 与 Shift 无关)
KEY_OPTIONS['['] = 0xDB       # VK_OEM_4
KEY_OPTIONS[']'] = 0xDD       # VK_OEM_6
KEY_OPTIONS['\\'] = 0xDC      # VK_OEM_5 反斜杠
KEY_OPTIONS[';'] = 0xBA       # VK_OEM_1 分号
KEY_OPTIONS["'"] = 0xDE       # VK_OEM_7 单引号
KEY_OPTIONS[','] = 0xBC       # VK_OEM_COMMA
KEY_OPTIONS['.'] = 0xBE       # VK_OEM_PERIOD
KEY_OPTIONS['/'] = 0xBF       # VK_OEM_2
KEY_OPTIONS['`'] = 0xC0       # VK_OEM_3 反引号
KEY_OPTIONS['-'] = 0xBD       # VK_OEM_MINUS
KEY_OPTIONS['='] = 0xBB       # VK_OEM_PLUS
# 编辑区键 (Insert/Delete/Home/End/PageUp/PageDown, VK 码固定)
KEY_OPTIONS['Insert'] = 0x2D  # VK_INSERT
KEY_OPTIONS['Delete'] = 0x2E  # VK_DELETE
KEY_OPTIONS['Home'] = 0x24    # VK_HOME
KEY_OPTIONS['End'] = 0x23     # VK_END
KEY_OPTIONS['PageUp'] = 0x21  # VK_PRIOR
KEY_OPTIONS['PageDown'] = 0x22  # VK_NEXT
# 小键盘运算键 (加 Num 前缀, 与主键盘 / - + 区分)
KEY_OPTIONS['Num/'] = 0x6F    # VK_DIVIDE
KEY_OPTIONS['Num*'] = 0x6A    # VK_MULTIPLY
KEY_OPTIONS['Num-'] = 0x6D    # VK_SUBTRACT
KEY_OPTIONS['Num+'] = 0x6B    # VK_ADD

DEFAULT_HOTKEY = 'F10'
hotkey = DEFAULT_HOTKEY
hotkey_pending = None      # 主线程写入待切换热键, 热键线程轮询读取
hotkey_suspended = False   # True=临时停用热键(捕获按键期间)

# 单一动作: 显示/隐藏手套
HOTKEY_ACTION_ID = 1
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

# Tk keysym -> 我们 KEY_OPTIONS 的键名 映射
# Tk 的 keysym 是英文短名, 我们的 KEY_OPTIONS 用统一命名, 这里建立互转表
TK_KEYSYM_TO_NAME = {
    'Up': 'Up', 'Down': 'Down', 'Left': 'Left', 'Right': 'Right',
    'Menu': 'Menu', 'Apps': 'Menu',  # Tk 不同平台 keysym 不一样, 都映射到 Menu
    'space': 'Space', 'Escape': 'Esc', 'Tab': 'Tab', 'Return': 'Enter',
    'F1': 'F1', 'F2': 'F2', 'F3': 'F3', 'F4': 'F4', 'F5': 'F5',
    'F6': 'F6', 'F7': 'F7', 'F8': 'F8', 'F9': 'F9', 'F10': 'F10',
    'F11': 'F11', 'F12': 'F12',
    # 编辑区键 (Tk keysym)
    'Insert': 'Insert', 'Delete': 'Delete', 'Home': 'Home', 'End': 'End',
    'Prior': 'PageUp', 'Next': 'PageDown',
    'Page_Up': 'PageUp', 'Page_Down': 'PageDown',
    # 小键盘 (Tk keysym 用 KP_ 前缀)
    'KP_Divide': 'Num/', 'KP_Multiply': 'Num*',
    'KP_Subtract': 'Num-', 'KP_Add': 'Num+',
}
for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    TK_KEYSYM_TO_NAME[c] = c
for i in range(10):
    TK_KEYSYM_TO_NAME[str(i)] = str(i)

# VK 码 -> 键名 反向映射 (用于 e.keycode 捕获: Windows 上 Tk 的 e.keycode == VK 码)
VK_TO_NAME = {}
for name, vk in KEY_OPTIONS.items():
    VK_TO_NAME[vk] = name

# 显式声明参数类型
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_bool
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = ctypes.c_bool


def log(msg):
    line = '[%s] %s\n' % (datetime.datetime.now().strftime('%H:%M:%S'), msg)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


# ================= 热键 =================

def install_hotkey(key):
    vk = KEY_OPTIONS.get(key)
    if vk is None:
        log('无效热键: %s' % key)
        return False
    ok = user32.RegisterHotKey(None, HOTKEY_ACTION_ID, MOD_NOREPEAT, vk)
    log('热键注册 %s(vk=%d): %s' % (key, vk, '成功' if ok else '失败'))
    return bool(ok)


def uninstall_hotkey():
    try:
        user32.UnregisterHotKey(None, HOTKEY_ACTION_ID)
    except Exception:
        pass


class MSG(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('message', wintypes.UINT),
                ('wParam', wintypes.WPARAM), ('lParam', wintypes.LPARAM),
                ('time', wintypes.DWORD), ('pt', wintypes.POINT)]


user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_uint]
user32.PeekMessageW.restype = ctypes.c_bool
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def hotkey_loop(key):
    """热键线程: 必须在本线程内 RegisterHotKey, WM_HOTKEY 才会投递到
    本线程队列。注册挪到主线程会导致热键全部失灵。

    热键切换: 主线程把新键写入 hotkey_pending, 本线程在消息循环里轮询到
    变更后, 在本线程内注销旧键、注册新键(保证线程归属正确)。
    """
    global hotkey_pending, hotkey_suspended
    current_key = key
    installed = False
    install_hotkey(current_key)
    installed = True
    msg = MSG()
    while not stop:
        # 检查是否处于"挂起"状态(捕获按键期间临时停用热键)
        if hotkey_suspended and installed:
            uninstall_hotkey()
            installed = False
        elif not hotkey_suspended and not installed:
            install_hotkey(current_key)
            installed = True
        # 检查是否有待切换的热键
        if hotkey_pending is not None and hotkey_pending != current_key:
            if installed:
                uninstall_hotkey()
                installed = False
            new_key = hotkey_pending
            hotkey_pending = None
            if not hotkey_suspended:
                install_hotkey(new_key)
                installed = True
            current_key = new_key
        # 用 PeekMessageW 轮询(非阻塞), 既收消息又能及时响应热键切换
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE=1
            if msg.message == 0x0012:  # WM_QUIT
                break
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_ACTION_ID:
                    log('收到热键: 显示/隐藏手套')
                    root.after(0, toggle_glove)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01)
    if installed:
        uninstall_hotkey()


# ================= 手套图处理 =================

def _find_fingertip(im):
    """食指尖端检测 (纯 PIL/Python, 无 numpy 依赖)。

    手套图中食指朝正上方(顶部那根竖直手指), 指尖 = 顶部手指区域的最上端中心。
    算法: 找每一列的最上不透明像素; 全局最小 y 即指尖高度; 在顶部 40px
    薄带内取不透明像素的 x 中点作为指尖 x。
    im: PIL Image (RGBA)。返回归一化 (x, y)。
    """
    w, h = im.size
    px = im.load()
    # 每一列最上的不透明像素 y
    topmost_y = [h] * w
    for x in range(w):
        for y in range(h):
            if px[x, y][3] > 100:
                topmost_y[x] = y
                break
    global_min_y = min(topmost_y)
    # 顶部薄带(指尖附近 40px)
    band = min(global_min_y + 40, h)
    tip_xs = []
    tip_y = h
    for y in range(band):
        for x in range(w):
            if px[x, y][3] > 100:
                if y < tip_y:
                    tip_y = y
                    tip_xs = [x]
                elif y == tip_y:
                    tip_xs.append(x)
    if not tip_xs:
        for y in range(h):
            for x in range(w):
                if px[x, y][3] > 100:
                    tip_xs.append(x)
                    break
            if tip_xs:
                break
    if not tip_xs:
        return 0.5, 0.0
    tip_x = sum(tip_xs) / len(tip_xs)
    return float(tip_x) / w, float(tip_y) / h


def load_glove():
    """加载手套图: 旋转 -> 指尖定位 -> 缩放 -> 预乘 alpha BGRA 数据。"""
    global glove_bgra, glove_pw, glove_ph, GLOVE_FINGER_X, GLOVE_FINGER_Y
    if not (HAS_PIL and HAS_PYWIN32):
        log('错误: 缺少依赖 Pillow/pywin32, 无法加载手套')
        return False
    path = resource_path('glove.png')
    if not os.path.exists(path):
        log('错误: 找不到 glove.png')
        return False
    try:
        try:
            resample_rsz = Image.Resampling.LANCZOS
        except AttributeError:
            resample_rsz = Image.BICUBIC

        # 注意: glove.png 已经是处理好的透明图(黑边+白填充, 背景透明,
        # 食指朝正上方)。无需再旋转 —— 旋转会破坏方向。
        im = Image.open(path).convert('RGBA')

        # 指尖定位(缩放前, 归一化坐标与尺寸无关)
        GLOVE_FINGER_X, GLOVE_FINGER_Y = _find_fingertip(im)
        log('指尖自动定位: x=%.3f, y=%.3f' % (GLOVE_FINGER_X, GLOVE_FINGER_Y))

        # 缩放
        w, h = im.size
        ratio = GLOVE_H / h
        im = im.resize((max(1, int(w * ratio)), GLOVE_H), resample_rsz)
        glove_pw, glove_ph = im.size

        # 预乘 alpha + RGBA->BGRA + 上下翻转(bottom-up DIB)
        w, h = im.size
        src = im.tobytes()
        out = bytearray(w * h * 4)
        for i in range(w * h):
            o = i * 4
            r, g, b, a = src[o], src[o + 1], src[o + 2], src[o + 3]
            out[o]     = r * a // 255
            out[o + 1] = g * a // 255
            out[o + 2] = b * a // 255
            out[o + 3] = a
        row = w * 4
        flipped = bytearray()
        for y in range(h - 1, -1, -1):
            flipped += out[y * row:(y + 1) * row]
        glove_bgra = bytes(flipped)
        log('手套图就绪: %dx%d, BGRA %d bytes' % (glove_pw, glove_ph, len(glove_bgra)))
        return True
    except Exception as e:
        log('加载手套图失败: %s' % e)
        return False


# ================= GDI 分层窗口 =================

# UpdateLayeredWindow 所需结构
class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [('cx', ctypes.c_long), ('cy', ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', ctypes.c_ulong), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
        ('biPlanes', ctypes.c_ushort), ('biBitCount', ctypes.c_ushort),
        ('biCompression', ctypes.c_ulong), ('biSizeImage', ctypes.c_ulong),
        ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long),
        ('biClrUsed', ctypes.c_ulong), ('biClrImportant', ctypes.c_ulong),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', ctypes.c_uint * 3)]


ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0

user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
    wintypes.HDC, ctypes.POINTER(_POINT), ctypes.c_uint,
    ctypes.POINTER(_BLENDFUNCTION), ctypes.c_uint
]
user32.UpdateLayeredWindow.restype = ctypes.c_bool
gdi32.SetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint,
                            ctypes.c_uint, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint]
gdi32.SetDIBits.restype = ctypes.c_int

GLOVE_CLASS = 'MouseHighlightGloveWnd'


def make_glove_window():
    """创建 Win32 原生分层窗口(不显示), 并准备 GDI 资源与位图。"""
    global glove_hwnd
    if not HAS_PYWIN32:
        log('错误: 缺少 pywin32, 手套窗口不可用')
        return
    hinst = win32api.GetModuleHandle()
    try:
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpfnWndProc = lambda *a: 0
        wc.lpszClassName = GLOVE_CLASS
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass  # 已注册

    ex_style = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
                win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST)
    glove_hwnd = win32gui.CreateWindowEx(
        ex_style, GLOVE_CLASS, 'glove', win32con.WS_POPUP,
        0, 0, glove_pw or 10, glove_ph or 10,
        0, 0, hinst, None)
    if not glove_hwnd:
        log('错误: CreateWindowEx 失败')
        return

    # GDI 资源: 内存 DC + 兼容位图
    screen_dc = win32gui.GetDC(0)
    src_dc = win32ui.CreateDCFromHandle(screen_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src_dc, glove_pw, glove_ph)
    mem_dc.SelectObject(bmp)
    _gdi.update({'screen_dc': screen_dc, 'src_dc': src_dc,
                 'mem_dc': mem_dc, 'bmp': bmp})

    # 写入预乘 alpha 像素
    if glove_bgra:
        _write_bitmap()

    ex = user32.GetWindowLongW(glove_hwnd, -20)
    log('GDI 手套窗口就绪 hwnd=%s ex=%s LAYERED=%s TRANSPARENT=%s' %
        (glove_hwnd, hex(ex), bool(ex & 0x80000), bool(ex & 0x20)))


def _write_bitmap():
    """把手套 BGRA 数据写入内存位图。返回写入的扫描线数(失败为0)。"""
    bi = _BITMAPINFO()
    bi.bmiHeader.biSize = 40
    bi.bmiHeader.biWidth = glove_pw
    bi.bmiHeader.biHeight = glove_ph
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    lines = gdi32.SetDIBits(_gdi['mem_dc'].GetSafeHdc(), _gdi['bmp'].GetHandle(),
                            0, glove_ph, glove_bgra, ctypes.byref(bi), DIB_RGB_COLORS)
    if lines != glove_ph:
        log('警告: SetDIBits 只写入 %d/%d 行' % (lines, glove_ph))
    return lines


def show_glove():
    global glove_visible, anchor_x, anchor_y, glove_show_time
    if not glove_hwnd or not glove_bgra:
        log('无法显示手套: 窗口或图片未就绪')
        return
    anchor_x = root.winfo_pointerx()
    anchor_y = root.winfo_pointery()
    # 指尖精确对齐鼠标位置(含微调偏移)
    wx = anchor_x - int(glove_pw * GLOVE_FINGER_X) + GLOVE_OFFSET_X
    wy = anchor_y - int(glove_ph * GLOVE_FINGER_Y) + GLOVE_OFFSET_Y

    dst = _POINT(wx, wy)
    sz = _SIZE(glove_pw, glove_ph)
    src = _POINT(0, 0)
    bf = _BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
    ok = user32.UpdateLayeredWindow(
        glove_hwnd, _gdi['screen_dc'],
        ctypes.byref(dst), ctypes.byref(sz),
        _gdi['mem_dc'].GetSafeHdc(), ctypes.byref(src),
        0, ctypes.byref(bf), ULW_ALPHA)
    # 关键: UpdateLayeredWindow 只合成内容, 窗口创建时是隐藏的,
    # 必须 ShowWindow 才会真正显示在屏幕上
    if ok:
        win32gui.ShowWindow(glove_hwnd, win32con.SW_SHOW)
        # HWND_TOPMOST 重设置顶, 确保 ShowWindow 后仍在最前
        win32gui.SetWindowPos(glove_hwnd, win32con.HWND_TOPMOST,
                              wx, wy, glove_pw, glove_ph,
                              win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE)
    glove_visible = True
    glove_show_time = int(datetime.datetime.now().timestamp() * 1000)
    vis = win32gui.IsWindowVisible(glove_hwnd) if ok else False
    log('显示手套 at (%d,%d) 窗口=(%d,%d) ULW=%s 可见=%s' %
        (anchor_x, anchor_y, wx, wy, 'OK' if ok else 'FAIL', vis))


def hide_glove():
    global glove_visible
    if glove_visible and glove_hwnd:
        glove_visible = False
        win32gui.ShowWindow(glove_hwnd, win32con.SW_HIDE)
        log('隐藏手套')


def destroy_glove_window():
    """退出时清理 GDI 资源与窗口。"""
    global glove_hwnd
    try:
        if glove_hwnd:
            win32gui.DestroyWindow(glove_hwnd)
            glove_hwnd = None
    except Exception:
        pass
    for k in ('mem_dc', 'bmp', 'src_dc'):
        try:
            obj = _gdi.pop(k, None)
            if obj is not None:
                if k == 'bmp':
                    win32gui.DeleteObject(obj.GetHandle())
                else:
                    obj.DeleteDC()
        except Exception:
            pass
    try:
        if 'screen_dc' in _gdi:
            win32gui.ReleaseDC(0, _gdi.pop('screen_dc'))
    except Exception:
        pass


def toggle_glove():
    global last_toggle_ts
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    if now_ms - last_toggle_ts < 250:
        return
    last_toggle_ts = now_ms
    if glove_visible:
        hide_glove()
    else:
        show_glove()


# ================= 系统托盘 =================

# Shell_NotifyIcon 结构
class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('hWnd', wintypes.HWND),
        ('uID', wintypes.UINT),
        ('uFlags', wintypes.UINT),
        ('uCallbackMessage', wintypes.UINT),
        ('hIcon', wintypes.HICON),
        ('szTip', wintypes.WCHAR * 128),
        ('dwState', wintypes.DWORD),
        ('dwStateMask', wintypes.DWORD),
        ('szInfo', wintypes.WCHAR * 256),
        ('uVersion', wintypes.UINT),
        ('szInfoTitle', wintypes.WCHAR * 64),
        ('dwInfoFlags', wintypes.DWORD),
        ('guidItem', wintypes.BYTE * 16),
        ('hBalloonIcon', wintypes.HICON),
    ]

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

WM_TRAY = 0x0400 + 100  # 自定义托盘回调消息
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

shell32 = ctypes.windll.shell32
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = ctypes.c_bool

# 子类化 root 窗口用的 SetWindowLongPtrW (64位) / SetWindowLongW (32位)
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]

tray_hwnd = None      # 托盘消息接收窗口句柄(用 Tk root 窗口)
tray_added = False
tray_menu = None
_orig_wndproc = None   # Tk root 窗口的原始 WndProc, 用于子类化后回调
_WNDPROC_T = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM)

# 共享标志位: WndProc 里只写这个 int(纯 C 操作, 零 Tk 操作, 绝对安全),
# 由主线程 update_loop 轮询读取并执行对应动作。
tray_event_flag = ctypes.c_int(0)   # 0=无 1=显示主界面 2=右键菜单


def _tray_wndproc(hwnd, msg, wparam, lparam):
    """子类化后的 Tk root 窗口过程。收到 WM_TRAY 时**只写共享标志位**
    (纯 C 操作), 绝不在 WndProc 里碰任何 Tk 对象 —— 避免 Tcl 重入崩溃。
    动作由主线程 update_loop 轮询 tray_event_flag 后执行。"""
    if msg == WM_TRAY:
        lp = lparam
        if lp == WM_RBUTTONUP or lp == WM_CONTEXTMENU:
            tray_event_flag.value = 2
        elif lp == WM_LBUTTONUP:
            tray_event_flag.value = 1
        return 0  # 已处理, 不再传给原 WndProc
    if _orig_wndproc is not None:
        return _orig_wndproc(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_tray_wndproc_c = _WNDPROC_T(_tray_wndproc)


def add_tray_icon():
    global tray_added, tray_hwnd, _orig_wndproc
    # 用 Tk 的 root 窗口作为托盘消息接收窗口(它有自己的消息泵, 不会重入)
    tray_hwnd = root.winfo_id()
    hwnd_c = wintypes.HWND(tray_hwnd)
    # 子类化 root 窗口: 保存原 WndProc, 替换成我们的
    GWL_WNDPROC = -4
    _orig_wndproc = _WNDPROC_T(user32.SetWindowLongPtrW(hwnd_c, GWL_WNDPROC,
                                                         ctypes.cast(_tray_wndproc_c, ctypes.c_void_p).value))
    # 加载 tray_icon.ico 作为托盘图标 (LoadImage 不直接支持 PNG, 用 .ico)
    icon_path = resource_path('tray_icon.ico')
    hicon = None
    if os.path.exists(icon_path):
        try:
            IMAGE_ICON = 1  # LR_LOADFROMFILE=0x00000010
            hicon = win32gui.LoadImage(0, icon_path, IMAGE_ICON, 128, 128, 0x00000010)
            if not hicon:
                raise RuntimeError('LoadImage 返回 None')
        except Exception as e:
            log('加载托盘图标失败, 用默认图标: %s' % e)
            hicon = None
    if not hicon:
        hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
    nid = _NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
    nid.hWnd = hwnd_c
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = hicon
    nid.szTip = T['tip']
    tray_added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
    log('托盘图标添加: %s (hwnd=%s, hicon=%s)' % ('成功' if tray_added else '失败', tray_hwnd, hicon))


def del_tray_icon():
    global tray_added, _orig_wndproc
    if tray_added and tray_hwnd:
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = wintypes.HWND(tray_hwnd)
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        tray_added = False
    # 恢复原 WndProc(防止 root.destroy 时调用已失效的 Python 回调)
    if _orig_wndproc is not None and tray_hwnd:
        try:
            user32.SetWindowLongPtrW(wintypes.HWND(tray_hwnd), -4,
                                     ctypes.cast(_orig_wndproc, ctypes.c_void_p).value)
        except Exception:
            pass
        _orig_wndproc = None


def show_tray_menu():
    """右键托盘: 弹菜单(显示主界面 / 退出)。"""
    global tray_menu
    try:
        if tray_menu is not None:
            tray_menu.destroy()
    except Exception:
        pass
    tray_menu = tk.Menu(root, tearoff=0)
    # 用 after 延迟执行, 避开菜单 grab 状态与 deiconify 的冲突
    tray_menu.add_command(label=T['tray_show'],
                          command=lambda: root.after(100, show_main_window))
    tray_menu.add_separator()
    tray_menu.add_command(label=T['tray_quit'],
                          command=lambda: root.after(100, quit_app))
    # 在鼠标位置弹出
    x, y = root.winfo_pointerx(), root.winfo_pointery()
    try:
        tray_menu.tk_popup(x, y)
    finally:
        try:
            tray_menu.grab_release()
        except Exception:
            pass


def show_main_window():
    try:
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        root.focus_force()
        log('显示主界面')
    except Exception as e:
        log('显示主界面失败: %s' % e)


def minimize_to_tray():
    root.withdraw()
    log('最小化到托盘')


# ================= 多语言 =================

STRINGS = {
    'zh': {
        'title': '鼠标指示',
        'hotkey_label': '鼠标指示热键',
        'hotkey_hint': '点输入框后按一个键...',
        'autostart': '开机启动',
        'save': '保存',
        'saved': '已保存',
        'unsaved': '有未保存的修改',
        'tray_show': '显示主界面',
        'tray_quit': '退出',
        'tip': '鼠标指示',
    },
    'en': {
        'title': 'Mouse Pointer',
        'hotkey_label': 'Pointer Hotkey',
        'hotkey_hint': 'Click input box, press a key...',
        'autostart': 'Start with Windows',
        'save': 'Save',
        'saved': 'Saved',
        'unsaved': 'Unsaved changes',
        'tray_show': 'Show Main Window',
        'tray_quit': 'Exit',
        'tip': 'Mouse Pointer',
    },
}


def detect_lang():
    """检测系统语言, 默认中文。"""
    try:
        loc = locale.getlocale()[0] or ''
        if loc.lower().startswith('zh'):
            return 'zh'
        if loc.lower().startswith('en'):
            return 'en'
    except Exception:
        pass
    # fallback: 看 Windows UI 语言 (GetUserDefaultUILanguage)
    try:
        lang = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF  # LANGID 主语言号
        if lang == 0x04:  # LANG_CHINESE
            return 'zh'
        if lang == 0x09:  # LANG_ENGLISH
            return 'en'
    except Exception:
        pass
    return 'zh'


LANG = detect_lang()
T = STRINGS[LANG]  # 当前语言字符串表


# ================= 开机启动 =================

AUTOSTART_REG_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTOSTART_NAME = 'MouseHighlight'


def get_autostart_cmd():
    """返回注册表 Run 项的值: 打包后是 exe 路径, 源码运行是 pythonw.exe + 脚本路径。"""
    if getattr(sys, 'frozen', False):
        # 单文件 exe: 直接指向 exe 自身
        return '"%s"' % os.path.abspath(sys.executable)
    py = sys.executable.replace('python.exe', 'pythonw.exe')
    script = os.path.abspath(__file__)
    return '"%s" "%s"' % (py, script)


def is_autostart_enabled():
    k = None
    try:
        k = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0,
                                win32con.KEY_READ)
        try:
            v, _ = win32api.RegQueryValueEx(k, AUTOSTART_NAME)
            return bool(v)
        except Exception:
            return False
    except Exception:
        return False
    finally:
        if k is not None:
            try:
                win32api.RegCloseKey(k)
            except Exception:
                pass


def set_autostart(enable):
    k = None
    try:
        k = win32api.RegOpenKey(win32con.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0,
                                win32con.KEY_SET_VALUE)
        if enable:
            cmd = get_autostart_cmd()
            win32api.RegSetValueEx(k, AUTOSTART_NAME, 0, win32con.REG_SZ, cmd)
        else:
            try:
                win32api.RegDeleteValue(k, AUTOSTART_NAME)
            except Exception:
                pass
        log('开机启动: %s' % ('开' if enable else '关'))
        return True
    except Exception as e:
        log('设置开机启动失败: %s' % e)
        return False
    finally:
        if k is not None:
            try:
                win32api.RegCloseKey(k)
            except Exception:
                pass


# ================= 配置 (持久化) =================

def load_config():
    """读取完整配置(热键 + 开机启动)。开机启动以注册表为准(可被外部修改),
    但配置文件里也存一份用于同步显示。"""
    global hotkey
    hk = DEFAULT_HOTKEY
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user_data = json.load(f)
            if isinstance(user_data, dict):
                if user_data.get('key') in KEY_OPTIONS:
                    hk = user_data['key']
        except Exception as e:
            log('读取配置失败, 使用默认: %s' % e)
    hotkey = hk
    return hk


def save_config(hk, autostart):
    global hotkey
    hotkey = hk
    try:
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data['key'] = hk
        data['autostart'] = autostart
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        log('保存配置失败: %s' % e)


# ================= 面板 =================

def make_panel():
    """面板: 标题栏(— 最小化 + ✕ 关闭) + 热键捕获输入框 + 开机启动 + 保存按钮。"""
    global root
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', 0.94)
    root.configure(bg=PANEL_BG)
    root.title(T['title'])

    W = root.winfo_screenwidth()
    PANEL_W = 240
    root.geometry('%dx140+%d+20' % (PANEL_W, max(0, W - PANEL_W - 20)))

    # ---- 标题栏 ----
    title_bar = tk.Frame(root, bg='#2d2d36', height=32)
    title_bar.pack(fill='x')
    title_bar.pack_propagate(False)

    title = tk.Label(title_bar, text=T['title'], bg='#2d2d36', fg='white',
                     font=('Microsoft YaHei', 11, 'bold'), anchor='w', padx=8)
    title.pack(side='left', fill='both', expand=True)

    def drag_start(e):
        root._dx = e.x_root - root.winfo_x()
        root._dy = e.y_root - root.winfo_y()

    def drag_move(e):
        root.geometry('+%d+%d' % (e.x_root - root._dx, e.y_root - root._dy))

    for w in [title, title_bar]:
        w.bind('<Button-1>', drag_start)
        w.bind('<B1-Motion>', drag_move)

    # 关闭按钮 ✕ (放最右边)
    close_btn = tk.Label(title_bar, text='✕', bg='#ff453a', fg='white',
                         font=('Microsoft YaHei', 12, 'bold'), width=3, anchor='center',
                         cursor='hand2')
    close_btn.pack(side='right', fill='y')
    close_btn.bind('<Enter>', lambda e: close_btn.config(bg='#ff6961'))
    close_btn.bind('<Leave>', lambda e: close_btn.config(bg='#ff453a'))
    close_btn.bind('<Button-1>', lambda e: quit_app())

    # 最小化按钮 — (放在 ✕ 左边)
    min_btn = tk.Label(title_bar, text='—', bg='#3a3a44', fg='white',
                       font=('Microsoft YaHei', 11, 'bold'), width=3, anchor='center',
                       cursor='hand2')
    min_btn.pack(side='right', fill='y')
    min_btn.bind('<Enter>', lambda e: min_btn.config(bg='#4a4a55'))
    min_btn.bind('<Leave>', lambda e: min_btn.config(bg='#3a3a44'))
    min_btn.bind('<Button-1>', lambda e: minimize_to_tray())

    # ---- 主体 ----
    body = tk.Frame(root, bg=PANEL_BG)
    body.pack(fill='both', expand=True, padx=12, pady=8)

    # --- 热键 ---
    row1 = tk.Frame(body, bg=PANEL_BG)
    row1.pack(fill='x', pady=2)
    tk.Label(row1, text=T['hotkey_label'], bg=PANEL_BG, fg='#cccccc',
             font=('Microsoft YaHei', 10), width=10, anchor='w').pack(side='left')

    key_var = tk.StringVar(value=hotkey)
    key_entry = tk.Entry(row1, textvariable=key_var, font=('Microsoft YaHei', 10),
                         width=12, justify='center', relief='solid', bd=1)
    key_entry.pack(side='left', padx=(4, 0))
    key_entry._captured = False
    key_entry._capturing = False

    def suspend_hotkey(s):
        global hotkey_suspended
        hotkey_suspended = s

    def on_key_press(e):
        # 优先用 e.keycode 捕获: Windows 上 Tk 的 keycode == 虚拟键码(VK),
        # 是纯物理键位, Menu键/标点键都能精确识别(keysym 对 Menu/标点不可靠)。
        name = VK_TO_NAME.get(e.keycode)
        if name is None:
            # 回退: 用 keysym 转键名(覆盖某些 keycode 拿不到的场景)
            name = TK_KEYSYM_TO_NAME.get(e.keysym)
        if name is None:
            return 'break'  # 忽略不能识别的键
        _capture_key(name)
        return 'break'

    def _capture_key(name):
        """捕获到有效键后的统一处理: 写入、标 dirty、失焦。"""
        key_var.set(name)
        key_entry.config(fg='black')
        key_entry._captured = True
        mark_dirty()
        root.focus_set()  # 让输入框失焦, 停止捕获

    def _poll_capture():
        """轮询兜底: 用 GetAsyncKeyState 检测 Menu键/标点键等 Tk 可能不派发
        KeyPress 事件的按键。输入框聚焦期间每 30ms 轮询一次。"""
        if not key_entry._capturing:
            return
        # 遍历所有可选键的 VK, 检测哪个刚被按下(最高位=按下)
        for name, vk in KEY_OPTIONS.items():
            if vk is None:
                continue
            st = user32.GetAsyncKeyState(vk) & 0x8000
            if st:
                _capture_key(name)
                return
        root.after(30, _poll_capture)

    def on_entry_focus_in(_e):
        # 获得焦点: 显示占位符提示, 并临时停用全局热键(避免按下的键触发手套)
        global hotkey_suspended
        key_entry._captured = False
        key_entry._capturing = True
        key_var.set('')
        key_entry.config(fg='#888888')
        suspend_hotkey(True)
        root.after(30, _poll_capture)

    def on_entry_focus_out(_e):
        # 失焦: 若未捕获有效键则恢复当前热键显示, 并恢复全局热键
        key_entry._capturing = False
        if not key_entry._captured:
            key_var.set(hotkey)
            key_entry.config(fg='black')
        suspend_hotkey(False)

    key_entry.bind('<KeyPress>', on_key_press)
    key_entry.bind('<FocusIn>', on_entry_focus_in)
    key_entry.bind('<FocusOut>', on_entry_focus_out)

    # --- 开机启动 ---
    row2 = tk.Frame(body, bg=PANEL_BG)
    row2.pack(fill='x', pady=4)
    autostart_var = tk.IntVar(value=1 if is_autostart_enabled() else 0)

    def on_autostart_toggle():
        mark_dirty()

    chk = tk.Checkbutton(row2, text=T['autostart'], bg=PANEL_BG, fg='#cccccc',
                         selectcolor=PANEL_BG, activebackground=PANEL_BG,
                         activeforeground='#cccccc',
                         font=('Microsoft YaHei', 10), variable=autostart_var,
                         command=on_autostart_toggle, anchor='w')
    chk.pack(side='left')

    # --- 保存按钮 + 状态 ---
    row3 = tk.Frame(body, bg=PANEL_BG)
    row3.pack(fill='x', pady=(6, 0))
    status_var = tk.StringVar(value='')

    save_btn = tk.Button(row3, text=T['save'], bg='#34c759', fg='white',
                         relief='flat', font=('Microsoft YaHei', 10),
                         activebackground='#28a745', activeforeground='white',
                         cursor='hand2', command=lambda: on_save_click())
    save_btn.pack(side='right')

    status_lbl = tk.Label(row3, text='', bg=PANEL_BG, fg='#888888',
                          font=('Microsoft YaHei', 9), anchor='w')
    status_lbl.pack(side='left', padx=(0, 8))

    panel_ui.update({
        'key_var': key_var,
        'autostart_var': autostart_var,
        'save_btn': save_btn,
        'status_var': status_var,
        'status_lbl': status_lbl,
    })

    root.update_idletasks()
    need_h = max(root.winfo_reqheight(), 140)
    root.geometry('%dx%d' % (PANEL_W, need_h))

    # ---- 内部: 标记/清除 dirty、保存回调 ----
    # 必须在 save_btn 创建后定义, 用 lambda 间接引用避免前向声明

    def mark_dirty():
        save_btn.config(state='normal', bg='#34c759')
        status_var.set(T['unsaved'])
        status_lbl.config(fg='#ff9f0a')

    def on_save_click():
        new_key = key_var.get()
        if new_key not in KEY_OPTIONS:
            status_var.set('无效热键')
            status_lbl.config(fg='#ff453a')
            return
        enable = bool(autostart_var.get())
        # 1. 写配置 + 注册表
        save_config(new_key, enable)
        set_autostart(enable)
        # 2. 应用热键(主线程只设置 pending, 热键线程轮询重注册)
        apply_hotkey(new_key)
        # 3. 状态回写
        save_btn.config(state='disabled', bg='#3a3a44')
        status_var.set(T['saved'])
        status_lbl.config(fg='#34c759')
        log('保存: 热键=%s 开机启动=%s' % (new_key, enable))


def apply_hotkey(new_key):
    """主线程切换热键: 只写入 hotkey_pending, 由热键线程轮询后自行重注册
    (RegisterHotKey/UnregisterHotKey 必须在同一线程)。"""
    global hotkey_pending
    hotkey_pending = new_key


def quit_app():
    global stop
    stop = True
    try:
        uninstall_hotkey()
    except Exception:
        pass
    try:
        del_tray_icon()
    except Exception:
        pass
    try:
        destroy_glove_window()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


def update_loop():
    if stop:
        return
    # 轮询托盘事件标志(WndProc 只写了 int, 这里在 Tk mainloop 内安全执行)
    flag = tray_event_flag.value
    if flag != 0:
        tray_event_flag.value = 0
        try:
            if flag == 1:
                show_main_window()
            elif flag == 2:
                show_tray_menu()
        except Exception as e:
            log('托盘事件处理失败: %s' % e)
    if glove_visible:
        now = int(datetime.datetime.now().timestamp() * 1000)
        # 1500ms 豁免 + 25px 阈值, 防止按热键时系统抖动立刻把手套切没
        if now - glove_show_time >= 1500:
            x = root.winfo_pointerx()
            y = root.winfo_pointery()
            if abs(x - anchor_x) > 25 or abs(y - anchor_y) > 25:
                hide_glove()
    root.after(50, update_loop)


def main():
    global hotkey
    hotkey = load_config()
    make_panel()
    load_glove()
    make_glove_window()
    add_tray_icon()
    threading.Thread(target=hotkey_loop, args=(hotkey,), daemon=True).start()
    log('鼠标指示已启动 [热键=%s lang=%s]' % (hotkey, LANG))
    update_loop()
    root.mainloop()


if __name__ == '__main__':
    main()
