"""Database-backed Agent Preset resources and runtime selection."""

from __future__ import annotations

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import AgentPreset, utc_now
from review_orchestrator.domain.schemas import (
    AgentPresetCreate,
    AgentPresetDefinition,
    AgentPresetLimitsOverride,
    AgentPresetListResponse,
    AgentPresetModelOverride,
    AgentPresetRead,
    AgentPresetTaskKind,
    AgentPresetUpdate,
)
from review_orchestrator.integrations.pi_agent import (
    AgentDomainPreset,
    AgentDomainPresetLimits,
    AgentDomainPresetModel,
    AgentDomainPresetOverrides,
    AgentPresetResourceReference,
)


class AgentPresetConflictError(ValueError):
    pass


class AgentPresetValidationError(ValueError):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("Agent Preset update is invalid.")
        self.errors = errors


def preset_scope_key(
    scope: str,
    provider: str | None,
    repo_full_name: str | None,
) -> str:
    if scope == "global":
        return "global"
    assert provider is not None and repo_full_name is not None
    return f"repository:{provider}:{repo_full_name}"


def _preset_read(preset: AgentPreset) -> AgentPresetRead:
    return AgentPresetRead(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        task_kind=preset.task_kind,
        scope=preset.scope,
        provider=preset.provider,
        repo_full_name=preset.repo_full_name,
        agent_id=preset.agent_id,
        task_type=preset.task_type,
        repository_skills=list(preset.repository_skills_json or []),
        model=(
            AgentPresetModelOverride.model_validate(preset.model_json)
            if preset.model_json is not None
            else None
        ),
        tools=(list(preset.tools_json) if preset.tools_json is not None else None),
        limits=(
            AgentPresetLimitsOverride.model_validate(preset.limits_json)
            if preset.limits_json is not None
            else None
        ),
        enabled=preset.enabled,
        revision=preset.revision,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


async def create_agent_preset(
    session: AsyncSession,
    payload: AgentPresetCreate,
) -> AgentPresetRead:
    values = payload.model_dump(mode="json")
    preset = AgentPreset(
        name=values["name"],
        description=values["description"],
        task_kind=values["task_kind"],
        scope=values["scope"],
        scope_key=preset_scope_key(
            values["scope"], values["provider"], values["repo_full_name"]
        ),
        provider=values["provider"],
        repo_full_name=values["repo_full_name"],
        agent_id=values["agent_id"],
        task_type=values["task_type"],
        repository_skills_json=values["repository_skills"],
        model_json=values["model"],
        tools_json=values["tools"],
        limits_json=values["limits"],
        enabled=values["enabled"],
    )
    session.add(preset)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AgentPresetConflictError(
            "Preset name or task-kind/scope already exists."
        ) from exc
    await session.refresh(preset)
    return _preset_read(preset)


async def list_agent_presets(
    session: AsyncSession,
    *,
    task_kind: str | None = None,
    scope: str | None = None,
    provider: str | None = None,
    repo_full_name: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AgentPresetListResponse:
    filters = []
    if task_kind is not None:
        filters.append(AgentPreset.task_kind == task_kind)
    if scope is not None:
        filters.append(AgentPreset.scope == scope)
    if provider is not None:
        filters.append(AgentPreset.provider == provider)
    if repo_full_name is not None:
        filters.append(AgentPreset.repo_full_name == repo_full_name)
    if enabled is not None:
        filters.append(AgentPreset.enabled.is_(enabled))
    total = int(
        (
            await session.execute(select(func.count(AgentPreset.id)).where(*filters))
        ).scalar_one()
    )
    items = list(
        (
            await session.execute(
                select(AgentPreset)
                .where(*filters)
                .order_by(
                    AgentPreset.task_kind,
                    AgentPreset.scope_key,
                    AgentPreset.name,
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    return AgentPresetListResponse(
        items=[_preset_read(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_agent_preset(
    session: AsyncSession,
    preset_id: str,
) -> AgentPresetRead | None:
    preset = await session.get(AgentPreset, preset_id)
    return None if preset is None else _preset_read(preset)


async def update_agent_preset(
    session: AsyncSession,
    preset_id: str,
    payload: AgentPresetUpdate,
) -> AgentPresetRead | None:
    preset = await session.get(AgentPreset, preset_id)
    if preset is None:
        return None
    current = _preset_read(preset).model_dump(
        exclude={"id", "revision", "created_at", "updated_at"},
        mode="json",
    )
    current.update(payload.model_dump(exclude_unset=True, mode="json"))
    try:
        definition = AgentPresetDefinition.model_validate(current)
    except ValidationError as exc:
        raise AgentPresetValidationError(
            exc.errors(include_context=False, include_url=False)
        ) from exc
    values = definition.model_dump(mode="json")
    preset.name = values["name"]
    preset.description = values["description"]
    preset.task_kind = values["task_kind"]
    preset.scope = values["scope"]
    preset.scope_key = preset_scope_key(
        values["scope"], values["provider"], values["repo_full_name"]
    )
    preset.provider = values["provider"]
    preset.repo_full_name = values["repo_full_name"]
    preset.agent_id = values["agent_id"]
    preset.task_type = values["task_type"]
    preset.repository_skills_json = values["repository_skills"]
    preset.model_json = values["model"]
    preset.tools_json = values["tools"]
    preset.limits_json = values["limits"]
    preset.enabled = values["enabled"]
    preset.revision += 1
    preset.updated_at = utc_now()
    session.add(preset)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AgentPresetConflictError(
            "Preset name or task-kind/scope already exists."
        ) from exc
    await session.refresh(preset)
    return _preset_read(preset)


async def delete_agent_preset(session: AsyncSession, preset_id: str) -> bool:
    preset = await session.get(AgentPreset, preset_id)
    if preset is None:
        return False
    await session.delete(preset)
    await session.commit()
    return True


async def resolve_agent_preset(
    session: AsyncSession,
    *,
    task_kind: AgentPresetTaskKind,
    provider: str,
    repo_full_name: str,
    fallback_agent_id: str,
    fallback_task_type: str,
    fallback_repository_skills: list[str],
) -> AgentDomainPreset:
    repository_key = preset_scope_key("repository", provider, repo_full_name)
    preset = (
        await session.execute(
            select(AgentPreset)
            .where(
                AgentPreset.task_kind == task_kind.value,
                AgentPreset.enabled.is_(True),
                AgentPreset.scope_key.in_([repository_key, "global"]),
            )
            .order_by(
                # Exact repository configuration wins over the global default.
                (AgentPreset.scope_key == repository_key).desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if preset is None:
        return AgentDomainPreset(
            agent_id=fallback_agent_id,
            task_type=fallback_task_type,
            repository_skills=fallback_repository_skills,
        )
    model = (
        AgentDomainPresetModel.model_validate(preset.model_json)
        if preset.model_json is not None
        else None
    )
    limits = (
        AgentDomainPresetLimits.model_validate(preset.limits_json)
        if preset.limits_json is not None
        else None
    )
    overrides = (
        AgentDomainPresetOverrides(
            model=model,
            tools=(list(preset.tools_json) if preset.tools_json is not None else None),
            limits=limits,
        )
        if model is not None or preset.tools_json is not None or limits is not None
        else None
    )
    return AgentDomainPreset(
        agent_id=preset.agent_id,
        task_type=preset.task_type,
        repository_skills=list(preset.repository_skills_json or []),
        resource=AgentPresetResourceReference(
            id=preset.id,
            name=preset.name,
            revision=preset.revision,
        ),
        overrides=overrides,
    )


def configured_preset_snapshot(preset: AgentDomainPreset) -> dict:
    snapshot: dict = {
        "schema_version": "1",
        "composition": {
            "agent": {"id": preset.agent_id},
            "repository": {"skills": preset.repository_skills},
            "task_type": {"id": preset.task_type},
        },
    }
    if preset.resource is not None:
        snapshot["resource"] = preset.resource.model_dump()
    if preset.overrides is not None:
        snapshot["overrides"] = preset.overrides.model_dump(
            by_alias=True, exclude_none=True
        )
    return snapshot
