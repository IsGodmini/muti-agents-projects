from pathlib import Path

from app.skills.loader import SkillRegistry
from app.tools import travel as _travel_tools  # noqa: F401


def test_skill_registry_validates_tool_permissions() -> None:
    registry = SkillRegistry(Path("skills"))
    registry.load_all()
    family = registry.get("family_trip_planning")
    assert "calculate_product_cost" in family.manifest.allowed_tools
    assert family.manifest.approval_required is True
