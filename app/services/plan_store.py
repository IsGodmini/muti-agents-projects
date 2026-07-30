from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class PlanStore:
    """File-based persistence for plan versions and approval records."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir

    def _plan_dir(self, plan_id: str) -> Path:
        plan_dir = self.data_dir / "plans" / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir

    def save_version(self, plan_id: str, reason: str, snapshot: dict) -> dict[str, str]:
        plan_dir = self._plan_dir(plan_id)
        existing = sorted(plan_dir.glob("v*.json"))
        version_number = len(existing) + 1
        version = f"v{version_number}.0"
        record = {
            "plan_id": plan_id,
            "version": version,
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot,
        }
        path = plan_dir / f"{version}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Saved plan %s %s → %s", plan_id, version, path)
        return {"plan_id": plan_id, "version": version, "reason": reason, "path": str(path)}

    def save_approval(self, plan_id: str, decision: dict) -> dict[str, str]:
        plan_dir = self._plan_dir(plan_id)
        record = {
            "plan_id": plan_id,
            "decision": decision,
            "created_at": datetime.now(UTC).isoformat(),
        }
        path = plan_dir / "approval.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Saved approval for %s → %s", plan_id, path)
        return {"plan_id": plan_id, "status": "recorded", "path": str(path)}

    def load_latest_version(self, plan_id: str) -> dict | None:
        plan_dir = self.data_dir / "plans" / plan_id
        if not plan_dir.exists():
            return None
        versions = sorted(plan_dir.glob("v*.json"))
        if not versions:
            return None
        return json.loads(versions[-1].read_text(encoding="utf-8"))


plan_store = PlanStore()
