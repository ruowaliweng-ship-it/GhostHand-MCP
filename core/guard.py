"""Safety guardrails: high-risk keyword interception and protected window filtering."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_HIGH_RISK_KEYWORDS: List[str] = [
    "支付",
    "付款",
    "转账",
    "确认购买",
    "永久删除",
    "格式化",
    "卸载",
    "Pay",
    "Purchase",
    "Delete permanently",
    "Format",
]


@dataclass
class GuardDecision:
    allowed: bool
    status: str = "OK"  # OK | NEEDS_USER_CONFIRMATION | NEEDS_PETER_CONFIRMATION | BLOCKED_PROTECTED
    reason: str = ""
    matched_rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


@dataclass
class SafetyGuard:
    high_risk_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_HIGH_RISK_KEYWORDS))
    protected_processes: List[str] = field(default_factory=list)
    protected_titles: List[str] = field(default_factory=list)
    confirmation_status: str = "NEEDS_USER_CONFIRMATION"

    @classmethod
    def from_config_file(cls, config_path: Optional[Path] = None) -> "SafetyGuard":
        guard = cls()
        if config_path is None:
            return guard
        path = Path(config_path)
        if not path.exists():
            return guard
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return guard

        extra_kw = data.get("high_risk_keywords")
        if isinstance(extra_kw, list):
            guard.extend_keywords(extra_kw)
        procs = data.get("protected_processes")
        if isinstance(procs, list):
            guard.extend_protected_processes(procs)
        titles = data.get("protected_titles")
        if isinstance(titles, list):
            guard.extend_protected_titles(titles)
        return guard

    def extend_keywords(self, keywords: Iterable[str]) -> None:
        existing = {k.lower() for k in self.high_risk_keywords}
        for kw in keywords:
            if kw and kw.lower() not in existing:
                self.high_risk_keywords.append(kw)
                existing.add(kw.lower())

    def extend_protected_processes(self, processes: Iterable[str]) -> None:
        existing = {p.lower() for p in self.protected_processes}
        for proc in processes:
            if proc and proc.lower() not in existing:
                self.protected_processes.append(proc)
                existing.add(proc.lower())

    def extend_protected_titles(self, titles: Iterable[str]) -> None:
        existing = {t.lower() for t in self.protected_titles}
        for title in titles:
            if title and title.lower() not in existing:
                self.protected_titles.append(title)
                existing.add(title.lower())

    def check_window_access(
        self,
        title: str = "",
        process: str = "",
        action: str = "interact",
    ) -> GuardDecision:
        """Check whether a window is in the protected list."""
        proc_l = (process or "").lower()
        title_l = (title or "").lower()

        for p in self.protected_processes:
            p_l = p.lower()
            if p_l and p_l in proc_l:
                return GuardDecision(
                    allowed=False,
                    status="BLOCKED_PROTECTED",
                    reason=f"Target process '{process}' is protected against automated '{action}'.",
                    matched_rule=f"protected_process:{p}",
                )

        for t in self.protected_titles:
            t_l = t.lower()
            if t_l and t_l in title_l:
                return GuardDecision(
                    allowed=False,
                    status="BLOCKED_PROTECTED",
                    reason=f"Target window '{title}' is protected against automated '{action}'.",
                    matched_rule=f"protected_title:{t}",
                )

        return GuardDecision(allowed=True)

    def check_action_text(
        self,
        text: str,
        confirmed: bool = False,
    ) -> GuardDecision:
        """Check whether an element label or action text matches high-risk keywords."""
        if confirmed or not text:
            return GuardDecision(allowed=True)

        text_l = text.lower()
        for kw in self.high_risk_keywords:
            if kw and kw.lower() in text_l:
                return GuardDecision(
                    allowed=False,
                    status=self.confirmation_status,
                    reason=f"High-risk action '{text}' matched keyword '{kw}'. User confirmation required.",
                    matched_rule=f"high_risk_keyword:{kw}",
                )

        return GuardDecision(allowed=True)
