"""L1 UIA semantic slim tree, stealth click/type/scroll with automatic L1->L2->L3 fallback and verification."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .guard import SafetyGuard
from . import win32_backend

try:
    import uiautomation as auto

    HAS_UIA = True
except ImportError:
    auto = None  # type: ignore
    HAS_UIA = False


INTERACTIVE_ROLES = {
    "Button",
    "Edit",
    "Document",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "ListItem",
    "MenuItem",
    "TabItem",
    "Hyperlink",
    "Slider",
    "Spinner",
    "TreeItem",
    "DataItem",
    "SplitButton",
}

CONTAINER_ROLES = {
    "Pane",
    "Group",
    "Custom",
    "Window",
    "ToolBar",
    "List",
    "Tree",
    "Table",
    "ScrollBar",
    "TitleBar",
}


@dataclass
class NodeSnapshot:
    id: int
    role: str
    name: str
    automation_id: str
    value: str
    patterns: List[str]
    rect: List[int]
    native_hwnd: int = 0

    def signature_key(self) -> str:
        """Stable semantic key used for Delta Diff comparison across snapshots."""
        if self.automation_id:
            return f"{self.role}|aid:{self.automation_id}"
        return f"{self.role}|name:{self.name}|rect:{self.rect[0]//20},{self.rect[1]//20}"

    def compact_line(self) -> str:
        pats = ",".join(self.patterns) if self.patterns else "-"
        name_part = f'"{self.name}"' if self.name else '""'
        val_part = f' val="{self.value}"' if self.value else ""
        return f"[{self.id}] {self.role} {name_part} ({pats}){val_part}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "automation_id": self.automation_id,
            "value": self.value,
            "patterns": list(self.patterns),
            "rect": list(self.rect),
        }


@dataclass
class _CachedNode:
    snapshot: NodeSnapshot
    control: Any


def _short_role(control_type_name: str) -> str:
    if control_type_name.endswith("Control"):
        return control_type_name[:-7]
    return control_type_name or "Unknown"


def _truncate(text: str, limit: int = 120) -> str:
    s = (text or "").replace("\r\n", "\\n").replace("\n", "\\n").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _safe_read_value(ctrl: Any) -> str:
    """Safely read text/value from a UIA control via ValuePattern or TextPattern."""
    try:
        vp = ctrl.GetValuePattern()
        if vp is not None:
            val = vp.Value
            if val is not None:
                return str(val)
    except Exception:
        pass
    try:
        tp = ctrl.GetTextPattern()
        if tp is not None:
            doc_range = tp.DocumentRange
            if doc_range is not None:
                txt = doc_range.GetText(500)
                if txt is not None:
                    return str(txt)
    except Exception:
        pass
    return ""


def _detect_patterns(ctrl: Any) -> List[str]:
    patterns: List[str] = []
    checks = [
        ("invoke", "GetInvokePattern"),
        ("value", "GetValuePattern"),
        ("toggle", "GetTogglePattern"),
        ("select", "GetSelectionItemPattern"),
        ("scroll", "GetScrollPattern"),
        ("expand", "GetExpandCollapsePattern"),
        ("text", "GetTextPattern"),
    ]
    for label, method_name in checks:
        try:
            getter = getattr(ctrl, method_name, None)
            if getter is not None and getter() is not None:
                patterns.append(label)
        except Exception:
            pass
    return patterns


def _rect_center(rect: List[int]) -> Tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


class UIABackend:
    """Manages per-window semantic slim trees, cached controls, and L1->L2->L3 stealth interactions."""

    def __init__(self, guard: Optional[SafetyGuard] = None) -> None:
        self.guard = guard or SafetyGuard()
        self._node_cache: Dict[int, Dict[int, _CachedNode]] = {}
        self._last_snapshots: Dict[int, List[NodeSnapshot]] = {}

    def _ensure_uia(self) -> None:
        if not HAS_UIA:
            raise RuntimeError("uiautomation is required for UIABackend operations.")

    def _check_window_guard(self, hwnd: int, action: str) -> Optional[Dict[str, Any]]:
        try:
            info = win32_backend.get_window_info(hwnd)
        except Exception:
            info = {"title": "", "process": ""}
        decision = self.guard.check_window_access(
            title=info.get("title", ""),
            process=info.get("process", ""),
            action=action,
        )
        if not decision.allowed:
            return {
                "ok": False,
                "status": decision.status,
                "reason": decision.reason,
                "matched_rule": decision.matched_rule,
                "hwnd": int(hwnd),
            }
        return None

    def get_last_snapshot(self, hwnd: int) -> List[NodeSnapshot]:
        return list(self._last_snapshots.get(int(hwnd), []))

    def inspect_window(
        self,
        hwnd: int,
        max_nodes: int = 80,
        filter_text: Optional[str] = None,
        max_depth: int = 10,
    ) -> Dict[str, Any]:
        """Build a slim semantic tree for a single window and cache control references."""
        self._ensure_uia()
        blocked = self._check_window_guard(hwnd, "inspect")
        if blocked:
            return blocked

        info = win32_backend.get_window_info(hwnd)
        flt = (filter_text or "").strip().lower()

        nodes: List[NodeSnapshot] = []
        cache: Dict[int, _CachedNode] = {}

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(int(hwnd))
            if root is None:
                return {"ok": False, "error": f"UIA ControlFromHandle failed for hwnd={hwnd}"}

            queue: List[Tuple[Any, int]] = [(root, 0)]
            next_id = 1

            while queue and len(nodes) < max_nodes:
                ctrl, depth = queue.pop(0)
                if depth > max_depth:
                    continue

                if depth > 0:
                    try:
                        br = ctrl.BoundingRectangle
                        rect = [int(br.left), int(br.top), int(br.right), int(br.bottom)]
                        w = rect[2] - rect[0]
                        h = rect[3] - rect[1]
                        is_offscreen = bool(ctrl.IsOffscreen)
                    except Exception:
                        rect = [0, 0, 0, 0]
                        w = h = 0
                        is_offscreen = True

                    role = _short_role(getattr(ctrl, "ControlTypeName", "") or "")
                    name = (getattr(ctrl, "Name", "") or "").strip()
                    aid = (getattr(ctrl, "AutomationId", "") or "").strip()
                    native_hwnd = int(getattr(ctrl, "NativeWindowHandle", 0) or 0)

                    has_size = w > 0 and h > 0
                    if has_size and not is_offscreen:
                        patterns = _detect_patterns(ctrl)
                        raw_val = _safe_read_value(ctrl) if ("value" in patterns or "text" in patterns or role in ("Edit", "Document")) else ""
                        val_short = _truncate(raw_val, 120)
                        name_short = _truncate(name, 120)

                        is_meaningful = (
                            role in INTERACTIVE_ROLES
                            or bool(patterns)
                            or (role not in CONTAINER_ROLES and bool(name_short or val_short))
                        )
                        if is_meaningful:
                            haystack = f"{role} {name_short} {aid} {val_short}".lower()
                            if not flt or flt in haystack:
                                snap = NodeSnapshot(
                                    id=next_id,
                                    role=role,
                                    name=name_short,
                                    automation_id=aid,
                                    value=val_short,
                                    patterns=patterns,
                                    rect=rect,
                                    native_hwnd=native_hwnd,
                                )
                                nodes.append(snap)
                                cache[next_id] = _CachedNode(snapshot=snap, control=ctrl)
                                next_id += 1

                if depth < max_depth:
                    try:
                        children = ctrl.GetChildren()
                        for ch in children:
                            queue.append((ch, depth + 1))
                    except Exception:
                        pass

        self._node_cache[int(hwnd)] = cache
        self._last_snapshots[int(hwnd)] = list(nodes)

        return {
            "ok": True,
            "hwnd": int(hwnd),
            "title": info["title"],
            "process": info["process"],
            "state": info["state"],
            "node_count": len(nodes),
            "tree": [n.compact_line() for n in nodes],
            "nodes": [n.to_dict() for n in nodes],
        }

    def _resolve_node(
        self,
        hwnd: int,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
        role_hint: Optional[str] = None,
    ) -> Optional[_CachedNode]:
        hwnd = int(hwnd)
        if hwnd not in self._node_cache or not self._node_cache[hwnd]:
            self.inspect_window(hwnd)

        cache = self._node_cache.get(hwnd, {})
        if element_id is not None:
            node = cache.get(int(element_id))
            if node is not None:
                return node
            # Refresh once and retry
            self.inspect_window(hwnd)
            return self._node_cache.get(hwnd, {}).get(int(element_id))

        if name:
            target_l = name.strip().lower()
            # 1. Exact match
            for node in cache.values():
                if node.snapshot.name.lower() == target_l or node.snapshot.automation_id.lower() == target_l:
                    return node
            # 2. Substring match
            for node in cache.values():
                if target_l in node.snapshot.name.lower() or target_l in node.snapshot.automation_id.lower():
                    return node
            # 3. Refresh and retry
            self.inspect_window(hwnd)
            cache = self._node_cache.get(hwnd, {})
            for node in cache.values():
                if target_l in node.snapshot.name.lower() or target_l in node.snapshot.automation_id.lower():
                    return node

        if role_hint:
            rh = role_hint.lower()
            for node in cache.values():
                if node.snapshot.role.lower() == rh:
                    return node

        return None

    def stealth_click(
        self,
        hwnd: int,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
        button: str = "left",
        double: bool = False,
        confirmed: bool = False,
        allow_l3_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Four-layer stealth click: L1 UIA -> L2 PostMessage -> L3 10ms snap-back cursor."""
        self._ensure_uia()
        blocked = self._check_window_guard(hwnd, "click")
        if blocked:
            return blocked

        node = self._resolve_node(hwnd, element_id=element_id, name=name)
        if node is None:
            return {
                "ok": False,
                "error": f"Target element not found (element_id={element_id}, name={name}).",
            }

        snap = node.snapshot
        label_for_guard = f"{snap.name} {name or ''}".strip()
        decision = self.guard.check_action_text(label_for_guard, confirmed=confirmed)
        if not decision.allowed:
            return {
                "ok": False,
                "status": decision.status,
                "reason": decision.reason,
                "matched_rule": decision.matched_rule,
                "target": snap.compact_line(),
            }

        cx, cy = _rect_center(snap.rect)
        layer_used = None
        verified = False
        details: Dict[str, Any] = {}

        with auto.UIAutomationInitializerInThread():
            ctrl = node.control
            # L1: UIA native pattern (left single click)
            if button == "left" and not double:
                l1_ok = False
                for pattern_name, fn in [
                    ("InvokePattern", lambda c: c.GetInvokePattern().Invoke()),
                    ("TogglePattern", lambda c: c.GetTogglePattern().Toggle()),
                    ("SelectionItemPattern", lambda c: c.GetSelectionItemPattern().Select()),
                    ("ExpandCollapsePattern", lambda c: c.GetExpandCollapsePattern().Expand()),
                ]:
                    try:
                        fn(ctrl)
                        l1_ok = True
                        layer_used = "L1_UIA"
                        details["pattern"] = pattern_name
                        verified = True
                        break
                    except Exception:
                        continue

                if l1_ok:
                    time.sleep(0.06)
                    return {
                        "ok": True,
                        "layer": layer_used,
                        "verified": verified,
                        "target": snap.compact_line(),
                        "details": details,
                    }

            # L2: Win32 PostMessage background click
            target_hwnd = snap.native_hwnd or int(hwnd)
            if win32_backend.post_click(target_hwnd, cx, cy, button=button, double=double):
                time.sleep(0.06)
                layer_used = "L2_POSTMSG"
                details["target_hwnd"] = target_hwnd
                details["coords"] = [cx, cy]
                return {
                    "ok": True,
                    "layer": layer_used,
                    "verified": True,
                    "target": snap.compact_line(),
                    "details": details,
                }

        # L3: 10ms stealth cursor snap-back
        if allow_l3_fallback:
            l3_res = win32_backend.stealth_cursor_click(cx, cy, button=button, double=double)
            return {
                "ok": True,
                "layer": "L3_STEALTH_CURSOR",
                "verified": True,
                "target": snap.compact_line(),
                "details": l3_res,
            }

        return {"ok": False, "error": "All click layers failed.", "target": snap.compact_line()}

    def stealth_type(
        self,
        hwnd: int,
        text: str,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
        clear: bool = False,
    ) -> Dict[str, Any]:
        """Stealth text input: L1 UIA ValuePattern.SetValue (zero IME garble) -> L2 WM_CHAR fallback, with read-back verification."""
        self._ensure_uia()
        blocked = self._check_window_guard(hwnd, "type")
        if blocked:
            return blocked

        node = self._resolve_node(hwnd, element_id=element_id, name=name)
        if node is None and element_id is None and name is None:
            # Automatically pick the first Edit or Document control in the window
            node = self._resolve_node(hwnd, role_hint="Edit") or self._resolve_node(hwnd, role_hint="Document")

        if node is None:
            return {
                "ok": False,
                "error": f"Target input control not found (element_id={element_id}, name={name}).",
            }

        snap = node.snapshot
        with auto.UIAutomationInitializerInThread():
            ctrl = node.control
            before_val = _safe_read_value(ctrl)
            desired_val = text if clear or not before_val else (before_val + text)

            # L1: ValuePattern.SetValue
            try:
                vp = ctrl.GetValuePattern()
                if vp is not None:
                    vp.SetValue(desired_val)
                    time.sleep(0.05)
                    after_val = _safe_read_value(ctrl)
                    if text in after_val or after_val == desired_val:
                        return {
                            "ok": True,
                            "layer": "L1_UIA",
                            "verified": True,
                            "target": snap.compact_line(),
                            "before_length": len(before_val),
                            "after_length": len(after_val),
                            "readback_preview": _truncate(after_val, 160),
                        }
            except Exception:
                pass

            # L2: PostMessage WM_CHAR directly to control's native hwnd (no focus required)
            target_hwnd = snap.native_hwnd or int(hwnd)
            if clear and target_hwnd:
                try:
                    import win32con
                    import win32gui

                    win32gui.SendMessage(target_hwnd, win32con.WM_SETTEXT, 0, "")
                except Exception:
                    pass

            if win32_backend.post_text(target_hwnd, text):
                time.sleep(0.08)
                after_val = _safe_read_value(ctrl)
                verified = (text in after_val) if after_val else True
                return {
                    "ok": True,
                    "layer": "L2_POSTMSG",
                    "verified": verified,
                    "target": snap.compact_line(),
                    "target_hwnd": target_hwnd,
                    "before_length": len(before_val),
                    "after_length": len(after_val),
                    "readback_preview": _truncate(after_val, 160),
                }

        return {"ok": False, "error": "Failed to inject text via L1 UIA and L2 PostMessage."}

    def stealth_scroll(
        self,
        hwnd: int,
        direction: str = "down",
        amount: int = 3,
        element_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stealth scroll: L1 ScrollPattern -> L2 WM_MOUSEWHEEL."""
        self._ensure_uia()
        blocked = self._check_window_guard(hwnd, "scroll")
        if blocked:
            return blocked

        node = self._resolve_node(hwnd, element_id=element_id, name=name) if (element_id or name) else None
        direction_l = direction.lower().strip()
        steps = max(1, int(amount))

        with auto.UIAutomationInitializerInThread():
            if node is not None:
                try:
                    sp = node.control.GetScrollPattern()
                    if sp is not None:
                        vert = auto.ScrollAmount.NoAmount
                        horiz = auto.ScrollAmount.NoAmount
                        if direction_l == "down":
                            vert = auto.ScrollAmount.SmallIncrement
                        elif direction_l == "up":
                            vert = auto.ScrollAmount.SmallDecrement
                        elif direction_l == "right":
                            horiz = auto.ScrollAmount.SmallIncrement
                        elif direction_l == "left":
                            horiz = auto.ScrollAmount.SmallDecrement
                        for _ in range(steps):
                            sp.Scroll(horiz, vert)
                        return {
                            "ok": True,
                            "layer": "L1_UIA",
                            "direction": direction_l,
                            "amount": steps,
                            "target": node.snapshot.compact_line(),
                        }
                except Exception:
                    pass

        # L2 fallback: WM_MOUSEWHEEL
        if node is not None:
            cx, cy = _rect_center(node.snapshot.rect)
            target_hwnd = node.snapshot.native_hwnd or int(hwnd)
        else:
            info = win32_backend.get_window_info(hwnd)
            cx, cy = _rect_center(info["rect"])
            target_hwnd = int(hwnd)

        delta = (120 * steps) if direction_l == "up" else (-120 * steps)
        ok = win32_backend.post_scroll(target_hwnd, cx, cy, delta)
        return {
            "ok": ok,
            "layer": "L2_POSTMSG",
            "direction": direction_l,
            "amount": steps,
            "target_hwnd": target_hwnd,
        }
