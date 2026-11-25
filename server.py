"""GhostHand-MCP standard MCP stdio server entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Ensure local project root is importable regardless of working directory
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import batch as batch_mod
from core import cdp_backend, win32_backend
from core.guard import SafetyGuard
from core.plugins import PluginRegistry
from core.uia_backend import UIABackend

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - fallback for mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build_server(config_path: Optional[str] = None) -> MCPServer:
    cfg = Path(config_path) if config_path else (PROJECT_ROOT / "ghosthand.config.json")
    guard = SafetyGuard.from_config_file(cfg if cfg.exists() else None)
    plugins = PluginRegistry()

    server_name = "ghosthand-mcp"
    instructions = (
        "GhostHand-MCP: Stealth, protocol-first Windows desktop automation engine. "
        "Prefer inspect_window + stealth_click/stealth_type/run_batch (L0 CDP -> L1 UIA -> L2 PostMessage -> L3 10ms snap-back) "
        "instead of capturing full-screen images."
    )

    uia = UIABackend(guard=guard)
    mcp = MCPServer(name=server_name, instructions=instructions)
    mcp._ghosthand_ctx = {  # type: ignore[attr-defined]
        "guard": guard,
        "uia": uia,
        "plugins": plugins,
    }

    def _resolve_target(hwnd_val: Union[int, str]) -> Union[int, Dict[str, Any]]:
        if isinstance(hwnd_val, int):
            return hwnd_val
        s = str(hwnd_val).strip()
        if s.isdigit():
            return int(s)
        return {"ok": False, "error": f"Invalid hwnd '{hwnd_val}'. Use list_windows to obtain an integer hwnd."}

    @mcp.tool()
    def list_windows(filter_text: Optional[str] = None) -> str:
        """List visible top-level Windows with compact metadata (hwnd, title, process, state, rect)."""
        try:
            wins = win32_backend.list_windows(filter_text=filter_text)
            return _dumps({"ok": True, "count": len(wins), "windows": wins})
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def inspect_window(
        hwnd: Union[int, str],
        max_nodes: int = 80,
        filter_text: Optional[str] = None,
    ) -> str:
        """Inspect a single window and return a slim semantic control tree ([id] Role "Name" (patterns))."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = uia.inspect_window(resolved, max_nodes=max_nodes, filter_text=filter_text)
            if res.get("ok"):
                compact = {
                    "ok": True,
                    "hwnd": res["hwnd"],
                    "title": res["title"],
                    "process": res["process"],
                    "state": res["state"],
                    "node_count": res["node_count"],
                    "tree": res["tree"],
                }
                return _dumps(compact)
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def capture_window(
        hwnd: Union[int, str],
        max_width: int = 1280,
        output_path: Optional[str] = None,
        include_base64: bool = True,
    ) -> str:
        """Capture a single window in the background via PrintWindow (works even when occluded, never steals focus)."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            info = win32_backend.get_window_info(resolved)
            decision = guard.check_window_access(info["title"], info["process"], action="capture")
            if not decision.allowed:
                return _dumps({"ok": False, **decision.to_dict()})
            res = win32_backend.capture_window(
                resolved,
                max_width=max_width,
                output_path=output_path,
                include_base64=include_base64,
            )
            return _dumps({"ok": True, **res})
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def stealth_click(
        hwnd: Union[int, str],
        element_id: Optional[int] = None,
        name: Optional[str] = None,
        button: str = "left",
        double: bool = False,
        confirmed: bool = False,
    ) -> str:
        """Four-layer stealth click (L1 UIA -> L2 PostMessage -> L3 10ms snap-back cursor) with high-risk guardrails."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = uia.stealth_click(
                hwnd=resolved,
                element_id=element_id,
                name=name,
                button=button,
                double=double,
                confirmed=confirmed,
            )
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def stealth_type(
        hwnd: Union[int, str],
        text: str,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
        clear: bool = False,
    ) -> str:
        """Stealth text input: prefers L1 UIA ValuePattern.SetValue (zero IME garble), falls back to L2 WM_CHAR."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = uia.stealth_type(
                hwnd=resolved,
                text=text,
                element_id=element_id,
                name=name,
                clear=clear,
            )
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def stealth_scroll(
        hwnd: Union[int, str],
        direction: str = "down",
        amount: int = 3,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> str:
        """Stealth scroll via L1 UIA ScrollPattern or L2 WM_MOUSEWHEEL without moving the mouse."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = uia.stealth_scroll(
                hwnd=resolved,
                direction=direction,
                amount=amount,
                element_id=element_id,
                name=name,
            )
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def run_batch(
        hwnd: Union[int, str],
        steps: List[Dict[str, Any]],
        stop_on_error: bool = True,
    ) -> str:
        """Run a multi-step UI pipeline locally in one call (click, type, scroll, wait_for, sleep, inspect)."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = batch_mod.run_batch(uia, resolved, steps, stop_on_error=stop_on_error)
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def wait_for(
        hwnd: Union[int, str],
        text: str,
        timeout_sec: float = 10.0,
    ) -> str:
        """Locally poll a window until a control/text matching `text` appears."""
        try:
            resolved = _resolve_target(hwnd)
            if isinstance(resolved, dict):
                return _dumps(resolved)
            res = batch_mod.wait_for(uia, resolved, text=text, timeout_sec=timeout_sec)
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    async def cdp_action(
        op: str,
        port: int = 9222,
        tab_match: Optional[str] = None,
        selector: Optional[str] = None,
        value: Optional[str] = None,
        url: Optional[str] = None,
    ) -> str:
        """L0 protocol-first Chrome/Edge/Electron CDP control (list_tabs, inspect, click, type, get_text, navigate)."""
        try:
            res = await cdp_backend.cdp_action_async(
                op=op,
                port=port,
                tab_match=tab_match,
                selector=selector,
                value=value,
                url=url,
            )
            return _dumps(res)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    def clipboard(op: str, text: Optional[str] = None) -> str:
        """Safely read ('get') or write ('set') UTF-16 text on the Windows clipboard."""
        try:
            op_l = (op or "").strip().lower()
            if op_l == "get":
                return _dumps({"ok": True, "op": "get", "text": win32_backend.clipboard_get()})
            if op_l == "set":
                win32_backend.clipboard_set(text or "")
                return _dumps({"ok": True, "op": "set", "length": len(text or "")})
            return _dumps({"ok": False, "error": f"Unsupported clipboard op '{op}'. Use 'get' or 'set'."})
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="GhostHand-MCP Windows stealth automation stdio server")
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to ghosthand.config.json.",
    )
    args = parser.parse_args()
    server = build_server(config_path=args.config)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
