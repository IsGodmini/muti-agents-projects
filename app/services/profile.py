"""User profile and preference memory.

File-based storage. Preferences are visible, editable, and deletable.
Sensitive data (passport, payment) is never stored.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class TravelPreference(BaseModel):
    preferred_pace: str = "moderate"
    budget_tier: str = "medium"
    hotel_type: str = ""
    early_riser: bool | None = None
    max_daily_walking_km: float | None = None
    dietary: list[str] = Field(default_factory=list)
    prefers_popular_spots: bool | None = None
    preferred_transport: list[str] = Field(default_factory=list)
    disliked_categories: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    user_id: str
    display_name: str = ""
    preference: TravelPreference = Field(default_factory=TravelPreference)
    trip_history: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ProfileStore:
    """File-based user profile persistence."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.profiles_dir = data_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.profiles_dir / f"{safe_id}.json"

    def load(self, user_id: str) -> UserProfile | None:
        path = self._path(user_id)
        if not path.exists():
            return None
        return UserProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, profile: UserProfile) -> None:
        profile.updated_at = datetime.now(UTC).isoformat()
        if not profile.created_at:
            profile.created_at = profile.updated_at
        self._path(profile.user_id).write_text(
            profile.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("Saved profile for %s", profile.user_id)

    def update_preference(self, user_id: str, **kwargs) -> UserProfile:
        profile = self.load(user_id) or UserProfile(user_id=user_id)
        pref_data = profile.preference.model_dump()
        pref_data.update(kwargs)
        profile.preference = TravelPreference(**pref_data)
        self.save(profile)
        return profile

    def record_trip(self, user_id: str, plan_id: str) -> None:
        profile = self.load(user_id) or UserProfile(user_id=user_id)
        if plan_id not in profile.trip_history:
            profile.trip_history.append(plan_id)
        self.save(profile)

    def delete(self, user_id: str) -> bool:
        path = self._path(user_id)
        if path.exists():
            path.unlink()
            return True
        return False


profile_store = ProfileStore()
