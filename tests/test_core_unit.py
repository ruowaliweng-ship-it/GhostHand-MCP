"""Unit tests for GhostHand-MCP core modules and MCP stdio handshake."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import batch as batch_mod
from core import cdp_backend
from core.guard import SafetyGuard
from core.plugins import GameBrainPlugin, PluginRegistry, VisionLocatorPlugin


def test_guard_high_risk_keywords_and_confirmation():
    guard = SafetyGuard()
    for risky in ["立即支付", "确认购买", "永久删除文件", "Format Drive", "Pay Now"]:
        d = guard.check_action_text(risky, confirmed=False)
        assert not d.allowed
        assert d.status == "NEEDS_USER_CONFIRMATION"

        d_ok = guard.check_action_text(risky, confirmed=True)
        assert d_ok.allowed
        assert d_ok.status == "OK"

    safe = guard.check_action_text("保存草稿", confirmed=False)
    assert safe.allowed


def test_guard_config_file_loading():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "ghosthand.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "protected_processes": ["KeePassXC.exe"],
                    "protected_titles": ["Secret Vault"],
                    "high_risk_keywords": ["Wire Transfer"],
                }
            ),
            encoding="utf-8",
        )
        guard = SafetyGuard.from_config_file(cfg)
        assert not guard.check_window_access(title="Anything", process="KeePassXC.exe").allowed
        assert not guard.check_window_access(title="My Secret Vault - Locked", process="app.exe").allowed
        assert not guard.check_action_text("Confirm Wire Transfer").allowed
        assert guard.check_window_access(title="Notepad", process="notepad.exe").allowed


class _FakeUIABackend:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._inspect_count = 0

    def stealth_type(self, hwnd: int, text: str, element_id=None, name=None, clear=False):
        self.calls.append(("type", {"hwnd": hwnd, "text": text, "name": name, "clear": clear}))
        return {"ok": True, "layer": "L1_UIA", "verified": True}

    def stealth_click(self, hwnd: int, element_id=None, name=None, button="left", double=False, confirmed=False):
        self.calls.append(("click", {"hwnd": hwnd, "name": name}))
        if name == "FAIL_BTN":
            return {"ok": False, "error": "button disabled"}
        return {"ok": True, "layer": "L1_UIA", "verified": True}

    def stealth_scroll(self, hwnd: int, direction="down", amount=3, element_id=None, name=None):
        self.calls.append(("scroll", {"direction": direction, "amount": amount}))
        return {"ok": True, "layer": "L1_UIA"}

    def inspect_window(self, hwnd: int, max_nodes: int = 80, filter_text=None):
        self._inspect_count += 1
        nodes = [{"id": 1, "name": "Loading", "value": "", "automation_id": ""}]
        if self._inspect_count >= 2:
            nodes.append({"id": 2, "name": "Submission Complete", "value": "Done", "automation_id": "done_lbl"})
        return {"ok": True, "nodes": nodes}


def test_run_batch_and_wait_for():
    fake = _FakeUIABackend()
    res = batch_mod.run_batch(
        fake,  # type: ignore[arg-type]
        hwnd=12345,
        steps=[
            {"action": "type", "name": "Username", "text": "alice", "clear": True},
            {"action": "click", "name": "Submit"},
            {"action": "wait_for", "text": "Submission Complete", "timeout_sec": 2.0},
            {"action": "scroll", "direction": "down", "amount": 2},
        ],
    )
    assert res["ok"] is True
    assert res["executed_steps"] == 4
    assert res["results"][2]["result"]["found"] is True

    res_err = batch_mod.run_batch(
        fake,  # type: ignore[arg-type]
        hwnd=12345,
        steps=[
            {"action": "click", "name": "FAIL_BTN"},
            {"action": "click", "name": "ShouldNotRun"},
        ],
        stop_on_error=True,
    )
    assert res_err["ok"] is False
    assert res_err["executed_steps"] == 1


class _DummyVision(VisionLocatorPlugin):
    def locate(self, image_bytes: bytes, query: str) -> Optional[Tuple[int, int, float]]:
        return (42, 84, 0.99) if query == "play" else None


class _DummyBrain(GameBrainPlugin):
    def step(self, hwnd: int, instruction: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"ok": True, "hwnd": hwnd, "instruction": instruction}


def test_plugin_registry():
    reg = PluginRegistry()
    assert reg.vision_locators == []
    assert reg.game_brains == {}
    reg.register_vision(_DummyVision())
    reg.register_game_brain("rts", _DummyBrain())
    assert reg.vision_locators[0].locate(b"png", "play") == (42, 84, 0.99)
    assert reg.game_brains["rts"].step(100, "defend")["ok"] is True


def test_cdp_tab_selection_and_graceful_fallback():
    tabs = [
        {"id": "1", "title": "New Tab", "url": "chrome://newtab/", "ws_url": "ws://127.0.0.1:9222/1"},
        {"id": "2", "title": "GitHub - GhostHand", "url": "https://github.com/example", "ws_url": "ws://127.0.0.1:9222/2"},
    ]
    matched = cdp_backend.select_tab(tabs, "github")
    assert matched is not None and matched["id"] == "2"
    assert cdp_backend.select_tab(tabs, None)["id"] == "1"  # type: ignore[index]
    assert cdp_backend.select_tab(tabs, "nonexistent") is None

    res = cdp_backend.list_cdp_tabs(port=59991, timeout=0.3)
    assert res["ok"] is False
    assert res["cdp_available"] is False
    assert "L1 UIA" in res["error"]


async def _mcp_handshake_list_tools() -> List[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_script = str(PROJECT_ROOT / "server.py")
    params = StdioServerParameters(
        command=sys.executable,
        args=[server_script],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            server_info = getattr(init_res, "server_info", None) or getattr(init_res, "serverInfo", None)
            assert server_info is not None
            tools_res = await session.list_tools()
            return [t.name for t in tools_res.tools]


def test_mcp_stdio_handshake():
    tools = asyncio.run(_mcp_handshake_list_tools())
    expected_core = {
        "list_windows",
        "inspect_window",
        "capture_window",
        "stealth_click",
        "stealth_type",
        "stealth_scroll",
        "run_batch",
        "wait_for",
        "cdp_action",
        "clipboard",
    }
    assert set(tools) == expected_core
