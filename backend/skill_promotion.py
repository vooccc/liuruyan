"""
System-level promotion flow for role-learned skill improvements.

Shared skills under `skills/` remain the canonical instructions. When a role has
enough field-tested experience with a skill, it can submit a promotion
candidate. Candidates are reviewed explicitly, then applied back into the
shared skill file as an additive "field-tested improvements" section.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent_config import get_agent_manager
from .skill_growth import get_skill_growth_manager
from .skill_loader import get_skill_loader


PROMOTION_SECTION_TITLE = "## Field-Tested Improvements"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip()).strip("-").lower()
    return slug or "candidate"


def _trim_lines(items: List[str], limit: int = 4) -> List[str]:
    result: List[str] = []
    for item in items or []:
        text = " ".join(str(item).split()).strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _truncate(text: str, max_len: int = 220) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


class SkillPromotionManager:
    def __init__(self, base_dir: Optional[Path] = None):
        root = base_dir or (Path(__file__).parent / "data" / "skill_promotions")
        self.data_dir = root
        self.data_file = root / "promotion_candidates.json"

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "candidates": [],
        }

    def load_state(self) -> Dict[str, Any]:
        if self.data_file.exists():
            try:
                data = json.loads(self.data_file.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("updated_at", None)
                    data.setdefault("candidates", [])
                    return data
            except Exception:
                pass
        return self._default_state()

    def save_state(self, state: Dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now_iso()
        self.data_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_candidates(
        self,
        *,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        skill_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        state = self.load_state()
        candidates = list(state.get("candidates") or [])

        if agent_id:
            candidates = [item for item in candidates if item.get("agent_id") == agent_id]
        if status:
            candidates = [item for item in candidates if item.get("status") == status]
        if skill_name:
            candidates = [item for item in candidates if item.get("skill_name") == skill_name]

        candidates.sort(
            key=lambda item: (
                item.get("updated_at") or "",
                item.get("created_at") or "",
            ),
            reverse=True,
        )
        return candidates

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        for item in self.load_state().get("candidates") or []:
            if item.get("id") == candidate_id:
                return item
        return None

    def _build_candidate_signature(self, candidate: Dict[str, Any]) -> str:
        fingerprint = {
            "agent_id": candidate.get("agent_id"),
            "skill_name": candidate.get("skill_name"),
            "source_xp": candidate.get("source_xp"),
            "successful_uses": candidate.get("successful_uses"),
            "failed_uses": candidate.get("failed_uses"),
            "heuristics": candidate.get("heuristics") or [],
            "pitfalls": candidate.get("pitfalls") or [],
            "last_summary": candidate.get("last_summary") or "",
        }
        raw = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def create_candidate(self, agent_id: str, skill_name: str) -> Dict[str, Any]:
        agent_manager = get_agent_manager()
        agent = agent_manager.get_agent(agent_id)
        if not agent:
            raise ValueError(f"agent '{agent_id}' does not exist")

        skill_loader = get_skill_loader()
        skill = skill_loader.get_skill(skill_name)
        if not skill:
            raise ValueError(f"skill '{skill_name}' does not exist")

        mastery = get_skill_growth_manager(agent_id).get_skill_record(skill.name)
        if not mastery.get("promotable"):
            raise ValueError("skill is not promotable yet")

        candidate = {
            "id": "",
            "signature": "",
            "status": "pending",
            "agent_id": agent_id,
            "agent_name": agent.get("name", agent_id),
            "skill_name": skill.name,
            "skill_path": str(skill.path),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "source_level": mastery.get("level", 0),
            "source_level_label": mastery.get("level_label", ""),
            "source_xp": mastery.get("xp", 0),
            "total_uses": mastery.get("total_uses", 0),
            "successful_uses": mastery.get("successful_uses", 0),
            "failed_uses": mastery.get("failed_uses", 0),
            "heuristics": _trim_lines(mastery.get("heuristics") or [], limit=5),
            "pitfalls": _trim_lines(mastery.get("pitfalls") or [], limit=5),
            "last_summary": _truncate(mastery.get("last_summary", "")),
            "review_notes": "",
            "reviewed_at": None,
            "approved_at": None,
            "applied_at": None,
            "applied_excerpt": "",
        }
        candidate["signature"] = self._build_candidate_signature(candidate)
        candidate["id"] = (
            f"{_slugify(skill.name)}-{_slugify(agent_id)}-"
            f"{candidate['created_at'].replace(':', '').replace('-', '')[:15]}"
        )

        state = self.load_state()
        for existing in state.get("candidates") or []:
            if (
                existing.get("signature") == candidate["signature"]
                and existing.get("status") in {"pending", "approved", "applied"}
            ):
                return {"created": False, "candidate": existing}

        state["candidates"].append(candidate)
        self.save_state(state)
        return {"created": True, "candidate": candidate}

    def review_candidate(self, candidate_id: str, *, approved: bool, review_notes: str = "") -> Dict[str, Any]:
        state = self.load_state()
        for candidate in state.get("candidates") or []:
            if candidate.get("id") != candidate_id:
                continue

            if candidate.get("status") == "applied":
                raise ValueError("applied candidate cannot be reviewed again")

            candidate["status"] = "approved" if approved else "rejected"
            candidate["review_notes"] = (review_notes or "").strip()
            candidate["reviewed_at"] = _now_iso()
            candidate["updated_at"] = candidate["reviewed_at"]
            if approved:
                candidate["approved_at"] = candidate["updated_at"]
            self.save_state(state)
            return candidate

        raise ValueError(f"candidate '{candidate_id}' does not exist")

    def apply_candidate(self, candidate_id: str, review_notes: str = "") -> Dict[str, Any]:
        state = self.load_state()
        for candidate in state.get("candidates") or []:
            if candidate.get("id") != candidate_id:
                continue

            status = candidate.get("status")
            if status == "applied":
                return candidate
            if status != "approved":
                raise ValueError("candidate must be approved before applying")

            skill_path = Path(candidate.get("skill_path") or "")
            if not skill_path.exists():
                raise ValueError("target skill file no longer exists")

            block = self._build_skill_patch_block(candidate)
            existing = skill_path.read_text(encoding="utf-8")
            suffix = "\n" if existing.endswith("\n") else "\n\n"
            if PROMOTION_SECTION_TITLE not in existing:
                existing = existing.rstrip() + f"\n\n{PROMOTION_SECTION_TITLE}\n"
                suffix = "\n\n"

            skill_path.write_text(existing.rstrip() + suffix + block + "\n", encoding="utf-8")

            candidate["status"] = "applied"
            candidate["applied_at"] = _now_iso()
            candidate["updated_at"] = candidate["applied_at"]
            if review_notes.strip():
                candidate["review_notes"] = review_notes.strip()
            candidate["applied_excerpt"] = block
            self.save_state(state)

            get_skill_loader().reload()
            return candidate

        raise ValueError(f"candidate '{candidate_id}' does not exist")

    def _build_skill_patch_block(self, candidate: Dict[str, Any]) -> str:
        created_at = candidate.get("created_at") or _now_iso()
        try:
            date_text = datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
        except ValueError:
            date_text = created_at

        lines = [
            f"### {date_text} · {candidate.get('agent_name', candidate.get('agent_id', 'unknown'))}",
            (
                f"- Source: `{candidate.get('agent_id', 'unknown')}` · "
                f"Lv{candidate.get('source_level', 0)} {candidate.get('source_level_label', '')} · "
                f"XP {candidate.get('source_xp', 0)}"
            ),
            (
                f"- Field stats: total {candidate.get('total_uses', 0)}, "
                f"success {candidate.get('successful_uses', 0)}, "
                f"failure {candidate.get('failed_uses', 0)}"
            ),
        ]

        heuristics = _trim_lines(candidate.get("heuristics") or [], limit=5)
        pitfalls = _trim_lines(candidate.get("pitfalls") or [], limit=5)
        if heuristics:
            lines.append("- Proven heuristics:")
            lines.extend([f"  - {item}" for item in heuristics])
        if pitfalls:
            lines.append("- Repeated pitfalls to avoid:")
            lines.extend([f"  - {item}" for item in pitfalls])

        summary = _truncate(candidate.get("last_summary", ""))
        if summary:
            lines.append(f"- Latest field note: {summary}")

        review_notes = _truncate(candidate.get("review_notes", ""), max_len=320)
        if review_notes:
            lines.append(f"- Review note: {review_notes}")

        return "\n".join(lines)


_promotion_manager: Optional[SkillPromotionManager] = None


def get_skill_promotion_manager() -> SkillPromotionManager:
    global _promotion_manager
    if _promotion_manager is None:
        _promotion_manager = SkillPromotionManager()
    return _promotion_manager
