"""L0 Protocol-first backend: Chrome / Edge / Electron CDP direct control over WebSocket."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import websockets

    HAS_WEBSOCKETS = True
except ImportError:
    websockets = None  # type: ignore
    HAS_WEBSOCKETS = False


def list_cdp_tabs(port: int = 9222, timeout: float = 1.5) -> Dict[str, Any]:
    """Enumerate debuggable tabs via http://127.0.0.1:<port>/json."""
    url = f"http://127.0.0.1:{int(port)}/json"
    # Bypass system HTTP proxies for loopback CDP calls
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            tabs = json.loads(raw)
    except Exception as exc:
        return {
            "ok": False,
            "cdp_available": False,
            "port": int(port),
            "error": f"CDP endpoint {url} unreachable ({exc}). Fallback to L1 UIA recommended.",
            "hint": "Launch Chrome/Edge with --remote-debugging-port=9222 to enable L0 CDP.",
            "tabs": [],
        }

    compact_tabs = []
    for t in tabs:
        if t.get("type") not in ("page", "webview", None):
            continue
        compact_tabs.append(
            {
                "id": t.get("id", ""),
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "ws_url": t.get("webSocketDebuggerUrl", ""),
            }
        )
    return {
        "ok": True,
        "cdp_available": True,
        "port": int(port),
        "tabs": compact_tabs,
    }


def select_tab(tabs: List[Dict[str, Any]], tab_match: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Select the best matching tab by title or URL substring, or first valid page tab."""
    if not tabs:
        return None
    if not tab_match:
        return tabs[0]
    needle = tab_match.strip().lower()
    for t in tabs:
        if needle in (t.get("title") or "").lower() or needle in (t.get("url") or "").lower():
            return t
    return None


async def _cdp_send(ws_url: str, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    if not HAS_WEBSOCKETS:
        raise RuntimeError("websockets package is required for CDP WebSocket commands.")
    req_id = 1
    payload = {"id": req_id, "method": method, "params": params or {}}
    async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=2.0) as ws:
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})


async def _cdp_eval(ws_url: str, expression: str, timeout: float = 5.0) -> Any:
    res = await _cdp_send(
        ws_url,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=timeout,
    )
    if "exceptionDetails" in res:
        raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
    return res.get("result", {}).get("value")


async def cdp_action_async(
    op: str,
    port: int = 9222,
    tab_match: Optional[str] = None,
    selector: Optional[str] = None,
    value: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a protocol-first CDP action (list_tabs, inspect, click, type, get_text, navigate)."""
    op_l = (op or "").strip().lower()
    listing = list_cdp_tabs(port=port)
    if not listing["ok"]:
        return listing

    if op_l == "list_tabs":
        return listing

    tab = select_tab(listing["tabs"], tab_match=tab_match)
    if tab is None:
        return {
            "ok": False,
            "layer": "L0_CDP",
            "error": f"No tab matched '{tab_match}'.",
            "available_tabs": [{"title": t["title"], "url": t["url"]} for t in listing["tabs"]],
        }

    ws_url = tab.get("ws_url")
    if not ws_url:
        return {"ok": False, "layer": "L0_CDP", "error": "Selected tab has no webSocketDebuggerUrl."}

    if op_l == "navigate":
        if not url:
            return {"ok": False, "layer": "L0_CDP", "error": "'url' is required for navigate."}
        await _cdp_send(ws_url, "Page.navigate", {"url": url})
        return {"ok": True, "layer": "L0_CDP", "op": "navigate", "tab": tab["title"], "url": url}

    if op_l == "inspect":
        js = """
        (() => {
          const els = Array.from(document.querySelectorAll('a,button,input,textarea,select,[role="button"]'));
          return {
            title: document.title,
            url: location.href,
            elements: els.slice(0, 60).map((el, idx) => ({
              idx: idx + 1,
              tag: el.tagName.toLowerCase(),
              id: el.id || '',
              name: el.getAttribute('name') || el.getAttribute('aria-label') || (el.innerText || '').trim().slice(0, 80),
              type: el.getAttribute('type') || '',
              selector: el.id ? ('#' + el.id) : (el.tagName.toLowerCase() + (el.getAttribute('name') ? `[name="${el.getAttribute('name')}"]` : ''))
            }))
          };
        })()
        """
        data = await _cdp_eval(ws_url, js)
        return {"ok": True, "layer": "L0_CDP", "op": "inspect", "data": data}

    if op_l == "click":
        if not selector:
            return {"ok": False, "layer": "L0_CDP", "error": "'selector' is required for click."}
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return {{ found: false }};
          el.click();
          return {{ found: true, tag: el.tagName.toLowerCase(), text: (el.innerText || '').trim().slice(0, 80) }};
        }})()
        """
        data = await _cdp_eval(ws_url, js)
        return {"ok": bool(data and data.get("found")), "layer": "L0_CDP", "op": "click", "selector": selector, "data": data}

    if op_l == "type":
        if not selector:
            return {"ok": False, "layer": "L0_CDP", "error": "'selector' is required for type."}
        text_val = value or ""
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return {{ found: false }};
          el.focus();
          el.value = {json.dumps(text_val)};
          el.dispatchEvent(new Event('input', {{ bubbles: true }}));
          el.dispatchEvent(new Event('change', {{ bubbles: true }}));
          return {{ found: true, value: el.value }};
        }})()
        """
        data = await _cdp_eval(ws_url, js)
        return {"ok": bool(data and data.get("found")), "layer": "L0_CDP", "op": "type", "selector": selector, "data": data}

    if op_l == "get_text":
        sel = selector or "body"
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return {{ found: false, text: '' }};
          return {{ found: true, title: document.title, text: (el.innerText || el.value || '').trim().slice(0, 2000) }};
        }})()
        """
        data = await _cdp_eval(ws_url, js)
        return {"ok": bool(data and data.get("found")), "layer": "L0_CDP", "op": "get_text", "selector": sel, "data": data}

    return {"ok": False, "layer": "L0_CDP", "error": f"Unsupported CDP op '{op}'."}


def cdp_action(
    op: str,
    port: int = 9222,
    tab_match: Optional[str] = None,
    selector: Optional[str] = None,
    value: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Synchronous wrapper for cdp_action_async."""
    return asyncio.run(
        cdp_action_async(
            op=op,
            port=port,
            tab_match=tab_match,
            selector=selector,
            value=value,
            url=url,
        )
    )
