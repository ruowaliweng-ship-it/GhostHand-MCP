"""Live Windows stealth verification test: background Notepad + PrintWindow + UIA ValuePattern + 0 cursor movement."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import win32_backend
from core.guard import SafetyGuard
from core.uia_backend import UIABackend

if sys.platform != "win32":
    pytest.skip("Windows live test requires Windows OS", allow_module_level=True)


def _find_notepad_hwnd_by_pid(pid: int, timeout: float = 6.0) -> int:
    import win32process

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in win32_backend.list_windows():
            hwnd = w["hwnd"]
            try:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                continue
            if wpid == pid and w["process"].lower() == "notepad.exe":
                return hwnd
        time.sleep(0.15)
    return 0


def test_windows_live_stealth_notepad():
    """Launch Notepad in background (no focus steal), test PrintWindow + UIA ValuePattern + clipboard + cleanup."""
    fg_before = win32_backend.get_foreground_hwnd()
    cursor_before = win32_backend.get_cursor_pos()

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4  # SW_SHOWNOACTIVATE
    proc = subprocess.Popen(["notepad.exe"], startupinfo=si)

    try:
        hwnd = _find_notepad_hwnd_by_pid(proc.pid, timeout=6.0)
        assert hwnd != 0, "Failed to locate launched Notepad window handle."

        cap = win32_backend.capture_window(hwnd, max_width=1280, include_base64=True)
        assert cap["print_window_ok"] is True
        assert cap["width"] > 100 and cap["height"] > 100
        assert cap["byte_size"] > 1000
        assert len(cap["image_base64"]) > 1000

        uia = UIABackend(guard=SafetyGuard())
        tree_res = uia.inspect_window(hwnd, max_nodes=40)
        assert tree_res["ok"] is True
        assert tree_res["node_count"] >= 1

        payload = "Hello GhostHand-MCP! 幽灵手无感写入测试 2026"
        type_res = uia.stealth_type(hwnd=hwnd, text=payload, clear=True)
        assert type_res["ok"] is True
        assert type_res["verified"] is True
        assert type_res["layer"] in ("L1_UIA", "L2_POSTMSG")
        assert "幽灵手无感写入测试" in type_res["readback_preview"]
        assert "Hello GhostHand-MCP!" in type_res["readback_preview"]

        cursor_after = win32_backend.get_cursor_pos()
        fg_after = win32_backend.get_foreground_hwnd()
        if cursor_before is not None and cursor_after is not None:
            assert cursor_after == cursor_before, f"Cursor moved from {cursor_before} to {cursor_after}!"
        assert fg_after == fg_before, f"Foreground window changed from {fg_before} to {fg_after}!"

        print(
            f"\n[LIVE VERIFIED] hwnd={hwnd}, layer={type_res['layer']}, "
            f"print_window={cap['width']}x{cap['height']} ({cap['byte_size']}B), "
            f"fg_preserved={fg_before == fg_after}, readback={type_res['readback_preview']!r}"
        )

        orig_clip = win32_backend.clipboard_get()
        try:
            test_clip = "GhostHand_Clipboard_Test_2026"
            assert win32_backend.clipboard_set(test_clip) is True
            assert win32_backend.clipboard_get() == test_clip
        finally:
            win32_backend.clipboard_set(orig_clip)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
