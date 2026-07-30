from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.models.schemas import SkillSummary
from app.tools.registry import ToolRegistry, tool_registry


class SkillManifest(BaseModel):
    name: str
    version: str
    description: str
    product_types: list[str] = Field(default_factory=list)
    allowed_tools: list[str]
    quality_gates: dict[str, float | int | str] = Field(default_factory=dict)
    max_retries: int = 2
    approval_required: bool = True


class LoadedSkill(BaseModel):
    manifest: SkillManifest
    instructions: str
    directory: Path

    model_config = {"arbitrary_types_allowed": True}


class SkillRegistry:
    def __init__(
        self,
        root: str | Path,
        tools: ToolRegistry = tool_registry,
    ) -> None:
        self.root = Path(root)
        self.tools = tools
        self._skills: dict[str, LoadedSkill] = {}

    def load_all(self) -> None:
        if not self.root.exists():
            return
        for manifest_path in sorted(self.root.glob("*/manifest.yaml")):
            skill_directory = manifest_path.parent
            manifest = SkillManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
            missing = [name for name in manifest.allowed_tools if name not in {t.name for t in self.tools.list()}]
            if missing:
                raise ValueError(f"Skill {manifest.name} references unknown tools: {missing}")
            instructions_path = skill_directory / "SKILL.md"
            instructions = instructions_path.read_text() if instructions_path.exists() else ""
            self._skills[manifest.name] = LoadedSkill(
                manifest=manifest,
                instructions=instructions,
                directory=skill_directory,
            )

    def get(self, name: str) -> LoadedSkill:
        return self._skills[name]

    def select_for_product_type(self, product_type: str) -> LoadedSkill:
        for skill in self._skills.values():
            if product_type in skill.manifest.product_types:
                return skill
        raise KeyError(f"No skill registered for product type: {product_type}")

    def summaries(self) -> list[SkillSummary]:
        return [
            SkillSummary(
                name=skill.manifest.name,
                version=skill.manifest.version,
                description=skill.manifest.description,
                allowed_tools=skill.manifest.allowed_tools,
                approval_required=skill.manifest.approval_required,
            )
            for skill in sorted(self._skills.values(), key=lambda item: item.manifest.name)
        ]
