"""Local batch pipeline executor (run_batch) and local polling wait_for."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .uia_backend import UIABackend


def wait_for(
    backend: UIABackend,
    hwnd: int,
    text: str,
    timeout_sec: float = 10.0,
    poll_interval: float = 0.25,
) -> Dict[str, Any]:
    """Locally poll a window's slim semantic tree until matching text/control appears."""
    needle = (text or "").strip().lower()
    if not needle:
        return {"ok": False, "error": "'text' must not be empty."}

    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    attempts = 0
    while True:
        attempts += 1
        res = backend.inspect_window(hwnd, max_nodes=120)
        if not res.get("ok"):
            return res

        for node in res.get("nodes", []):
            hay = f"{node.get('name', '')} {node.get('value', '')} {node.get('automation_id', '')}".lower()
            if needle in hay:
                return {
                    "ok": True,
                    "found": True,
                    "attempts": attempts,
                    "matched_node": node,
                }

        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "found": False,
                "attempts": attempts,
                "error": f"Timed out after {timeout_sec}s waiting for '{text}'.",
            }
        time.sleep(poll_interval)


def run_batch(
    backend: UIABackend,
    hwnd: int,
    steps: List[Dict[str, Any]],
    stop_on_error: bool = True,
) -> Dict[str, Any]:
    """Execute a sequence of UI actions locally in one call to minimize LLM round-trips."""
    results: List[Dict[str, Any]] = []
    all_ok = True

    for idx, step in enumerate(steps):
        action = str(step.get("action") or step.get("op") or "").strip().lower()
        step_res: Dict[str, Any]

        if action == "click":
            step_res = backend.stealth_click(
                hwnd=hwnd,
                element_id=step.get("element_id"),
                name=step.get("name"),
                button=step.get("button", "left"),
                double=bool(step.get("double", False)),
                confirmed=bool(step.get("confirmed", False)),
            )
        elif action == "type":
            step_res = backend.stealth_type(
                hwnd=hwnd,
                text=str(step.get("text", "")),
                element_id=step.get("element_id"),
                name=step.get("name"),
                clear=bool(step.get("clear", False)),
            )
        elif action == "scroll":
            step_res = backend.stealth_scroll(
                hwnd=hwnd,
                direction=str(step.get("direction", "down")),
                amount=int(step.get("amount", 3)),
                element_id=step.get("element_id"),
                name=step.get("name"),
            )
        elif action == "wait_for":
            step_res = wait_for(
                backend=backend,
                hwnd=hwnd,
                text=str(step.get("text", "")),
                timeout_sec=float(step.get("timeout_sec", 10.0)),
            )
        elif action == "sleep":
            sec = max(0.0, min(30.0, float(step.get("seconds", step.get("sec", 0.2)))))
            time.sleep(sec)
            step_res = {"ok": True, "action": "sleep", "seconds": sec}
        elif action == "inspect":
            step_res = backend.inspect_window(
                hwnd=hwnd,
                max_nodes=int(step.get("max_nodes", 80)),
                filter_text=step.get("filter_text"),
            )
        else:
            step_res = {"ok": False, "error": f"Unsupported batch action '{action}'."}

        results.append({"step": idx + 1, "action": action, "result": step_res})
        if not step_res.get("ok"):
            all_ok = False
            if stop_on_error:
                break

    return {
        "ok": all_ok,
        "hwnd": int(hwnd),
        "total_steps": len(steps),
        "executed_steps": len(results),
        "results": results,
    }
