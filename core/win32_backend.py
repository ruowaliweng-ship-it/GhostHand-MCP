"""Win32 low-level backend: window enumeration, PrintWindow capture, PostMessage, 10ms stealth click, clipboard."""

from __future__ import annotations

import base64
import ctypes
import io
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
    import win32ui

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


PW_RENDERFULLCONTENT = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_SHOWNOACTIVATE = 4

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None

# Make process DPI aware so window rects and PrintWindow bitmaps match physical pixels.
if user32 is not None:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _ensure_win32() -> None:
    if not HAS_WIN32:
        raise RuntimeError("pywin32 is required on Windows for Win32Backend operations.")


def get_process_name(hwnd: int) -> str:
    """Return executable basename (e.g. 'notepad.exe') for a window handle."""
    if not HAS_WIN32 or not user32 or not kernel32:
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return ""
        try:
            buf_len = wintypes.DWORD(2048)
            buf = ctypes.create_unicode_buffer(2048)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(buf_len)):
                return Path(buf.value).name
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass
    return ""


def get_window_info(hwnd: int) -> Dict[str, Any]:
    """Return compact metadata dictionary for a single window handle."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        raise ValueError(f"Invalid window handle: {hwnd}")
    title = win32gui.GetWindowText(hwnd) or ""
    process = get_process_name(hwnd)
    if win32gui.IsIconic(hwnd):
        state = "minimized"
    else:
        placement = win32gui.GetWindowPlacement(hwnd)
        state = "maximized" if placement[1] == win32con.SW_SHOWMAXIMIZED else "normal"
    rect = list(win32gui.GetWindowRect(hwnd))
    return {
        "hwnd": int(hwnd),
        "title": title,
        "process": process,
        "state": state,
        "rect": rect,
    }


def list_windows(filter_text: Optional[str] = None) -> List[Dict[str, Any]]:
    """Enumerate visible top-level windows with compact metadata (Z-order via GetTopWindow/GetWindow)."""
    _ensure_win32()
    raw_hwnds: List[int] = []
    flt = (filter_text or "").strip().lower()

    # Walk top-level Z-order directly (avoids pywin32 EnumWindows Error 122 on restricted/alternate desktops)
    if user32 is not None:
        hwnd = int(user32.GetTopWindow(0))
        seen = set()
        while hwnd and hwnd not in seen:
            seen.add(hwnd)
            try:
                if win32gui.IsWindowVisible(hwnd):
                    raw_hwnds.append(hwnd)
            except Exception:
                pass
            hwnd = int(user32.GetWindow(hwnd, win32con.GW_HWNDNEXT))
    else:
        def _callback(hwnd: int, _extra: Any) -> bool:
            try:
                if win32gui.IsWindowVisible(hwnd):
                    raw_hwnds.append(int(hwnd))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_callback, None)

    results: List[Dict[str, Any]] = []
    for hwnd in raw_hwnds:
        try:
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                continue
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            is_minimized = bool(win32gui.IsIconic(hwnd))
            if not is_minimized and (width <= 0 or height <= 0):
                continue

            process = get_process_name(hwnd)
            if flt and (flt not in title.lower() and flt not in process.lower()):
                continue

            if is_minimized:
                state = "minimized"
            else:
                placement = win32gui.GetWindowPlacement(hwnd)
                state = "maximized" if placement[1] == win32con.SW_SHOWMAXIMIZED else "normal"

            results.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "process": process,
                    "state": state,
                    "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
                }
            )
        except Exception:
            continue

    return results


def get_foreground_hwnd() -> int:
    _ensure_win32()
    return int(win32gui.GetForegroundWindow())


def restore_window_no_activate(hwnd: int) -> bool:
    """Silently restore a minimized window without stealing foreground focus."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    if not win32gui.IsIconic(hwnd):
        return True
    # SW_SHOWNOACTIVATE restores/shows the window without activating it.
    win32gui.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    time.sleep(0.08)
    return True


def capture_window(
    hwnd: int,
    max_width: int = 1280,
    output_path: Optional[str] = None,
    include_base64: bool = True,
) -> Dict[str, Any]:
    """Capture a window using PrintWindow(PW_RENDERFULLCONTENT) even when occluded, without stealing focus."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        raise ValueError(f"Invalid window handle: {hwnd}")

    if win32gui.IsIconic(hwnd):
        restore_window_no_activate(hwnd)

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(1, right - left)
    height = max(1, bottom - top)

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()

    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        res = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if res == 0:
            # Fallback to standard PrintWindow flags=0
            res = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)

        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

    orig_w, orig_h = img.size
    if max_width and orig_w > max_width:
        scale = max_width / float(orig_w)
        new_h = max(1, int(orig_h * scale))
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    saved_path = None
    if output_path:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png_bytes)
        saved_path = str(p)

    result: Dict[str, Any] = {
        "hwnd": int(hwnd),
        "title": win32gui.GetWindowText(hwnd) or "",
        "width": img.size[0],
        "height": img.size[1],
        "original_size": [orig_w, orig_h],
        "byte_size": len(png_bytes),
        "print_window_ok": bool(res),
        "saved_path": saved_path,
    }
    if include_base64:
        result["image_base64"] = base64.b64encode(png_bytes).decode("ascii")
    return result


def post_click(
    hwnd: int,
    screen_x: int,
    screen_y: int,
    button: str = "left",
    double: bool = False,
) -> bool:
    """L2 background click via PostMessage without moving the physical cursor."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    cx, cy = win32gui.ScreenToClient(hwnd, (int(screen_x), int(screen_y)))
    lparam = win32api.MAKELONG(cx, cy)

    if button == "right":
        down_msg, up_msg, dbl_msg, wparam = (
            win32con.WM_RBUTTONDOWN,
            win32con.WM_RBUTTONUP,
            win32con.WM_RBUTTONDBLCLK,
            win32con.MK_RBUTTON,
        )
    else:
        down_msg, up_msg, dbl_msg, wparam = (
            win32con.WM_LBUTTONDOWN,
            win32con.WM_LBUTTONUP,
            win32con.WM_LBUTTONDBLCLK,
            win32con.MK_LBUTTON,
        )

    win32gui.PostMessage(hwnd, down_msg, wparam, lparam)
    win32gui.PostMessage(hwnd, up_msg, 0, lparam)
    if double:
        win32gui.PostMessage(hwnd, dbl_msg, wparam, lparam)
        win32gui.PostMessage(hwnd, up_msg, 0, lparam)
    return True


def post_text(hwnd: int, text: str) -> bool:
    """L2 background text injection via WM_CHAR."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    for ch in text:
        if ch == "\n":
            win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord("\r"), 0)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(ch), 0)
    return True


def post_scroll(hwnd: int, screen_x: int, screen_y: int, delta: int) -> bool:
    """L2 background scroll via WM_MOUSEWHEEL (delta: positive=up, negative=down)."""
    _ensure_win32()
    if not win32gui.IsWindow(hwnd):
        return False
    lparam = win32api.MAKELONG(int(screen_x), int(screen_y))
    wparam = win32api.MAKELONG(0, int(delta) & 0xFFFF)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
    return True


def get_cursor_pos(strict: bool = False) -> Optional[Tuple[int, int]]:
    """Return physical cursor (x, y), or None if desktop input is currently restricted (e.g. locked screen)."""
    _ensure_win32()
    try:
        pos = win32api.GetCursorPos()
        return int(pos[0]), int(pos[1])
    except Exception:
        if strict:
            raise
        return None


def stealth_cursor_click(
    screen_x: int,
    screen_y: int,
    button: str = "left",
    double: bool = False,
    yield_ms: int = 300,
    speed_threshold_px: int = 15,
) -> Dict[str, Any]:
    """L3 fallback: yield 300ms if user is actively moving the mouse, then click and snap back within ~10ms."""
    _ensure_win32()
    p1 = get_cursor_pos()
    if p1 is None:
        return {
            "layer": "L3_STEALTH_CURSOR",
            "ok": False,
            "error": "GetCursorPos denied (desktop locked or non-interactive session).",
        }
    time.sleep(0.025)
    p2 = get_cursor_pos() or p1
    dist = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
    yielded = False
    if dist >= speed_threshold_px:
        yielded = True
        time.sleep(max(0.0, yield_ms / 1000.0))

    orig = get_cursor_pos() or p2
    orig_x, orig_y = orig[0], orig[1]
    t0 = time.perf_counter()

    if button == "right":
        down_flag, up_flag = win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP
    else:
        down_flag, up_flag = win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP

    try:
        win32api.SetCursorPos((int(screen_x), int(screen_y)))
        win32api.mouse_event(down_flag, 0, 0, 0, 0)
        win32api.mouse_event(up_flag, 0, 0, 0, 0)
        if double:
            win32api.mouse_event(down_flag, 0, 0, 0, 0)
            win32api.mouse_event(up_flag, 0, 0, 0, 0)
    finally:
        try:
            win32api.SetCursorPos((orig_x, orig_y))
        except Exception:
            pass

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {
        "layer": "L3_STEALTH_CURSOR",
        "ok": True,
        "target": [int(screen_x), int(screen_y)],
        "restored_cursor": [orig_x, orig_y],
        "yielded_for_user": yielded,
        "elapsed_ms": elapsed_ms,
    }


def clipboard_get(retries: int = 5) -> str:
    """Safely read UTF-16 text from Windows clipboard."""
    _ensure_win32()
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.03)
    raise RuntimeError("Failed to open Windows clipboard for reading.")


def clipboard_set(text: str, retries: int = 5) -> bool:
    """Safely write UTF-16 text to Windows clipboard."""
    _ensure_win32()
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.03)
    raise RuntimeError("Failed to open Windows clipboard for writing.")
