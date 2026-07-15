"""Provider Core HTTP API exposing provider-neutral platform operations."""

from __future__ import annotations

import hmac
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from review_orchestrator.integrations.providers import (
    CommentPublishRequest,
    CommentPublishResult,
    GitCheckoutRequest,
    GitCheckoutTarget,
    NormalizedWebhook,
    PlatformQueryRequest,
    PlatformQueryResult,
    Provider,
    ProviderCapabilityError,
    ProviderError,
    ProviderOperationError,
    ProviderRegistry,
    ProviderWebhookError,
)

router = APIRouter(prefix="/v1", tags=["provider-core"])
bearer_scheme = HTTPBearer(auto_error=False)


def _registry(request: Request) -> ProviderRegistry:
    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise RuntimeError("Provider registry was not initialized by the application.")
    return registry


async def _authorize(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> None:
    expected = request.app.state.settings.provider_core_api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Provider Core API token is not configured.",
        )
    supplied = credentials.credentials if credentials is not None else ""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Provider Core bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


auth_dependency = Depends(_authorize)


def _provider(request: Request, key: str) -> Provider:
    registry = _registry(request)
    try:
        provider = registry.require(key)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported provider: {key}",
        ) from exc
    if not isinstance(provider, Provider):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Provider {key!r} does not implement Provider Core.",
        )
    return provider


def _raise_provider_http_error(error: ProviderError) -> None:
    if isinstance(error, ProviderCapabilityError):
        status_code = status.HTTP_400_BAD_REQUEST
        code = "provider_capability_error"
    elif isinstance(error, ProviderOperationError):
        status_code = status.HTTP_502_BAD_GATEWAY
        code = "provider_operation_error"
    else:
        status_code = status.HTTP_502_BAD_GATEWAY
        code = "provider_error"
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "provider": error.provider,
            "operation": error.operation,
            "message": "Provider operation failed.",
        },
    ) from error


@router.post("/webhooks/{provider}/normalize", dependencies=[auth_dependency])
async def normalize_webhook_endpoint(
    provider: str,
    request: Request,
) -> dict[str, Any]:
    implementation = _provider(request, provider)
    try:
        result: NormalizedWebhook = await implementation.normalize_webhook(
            dict(request.headers),
            await request.body(),
        )
    except ProviderWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ProviderError as exc:
        _raise_provider_http_error(exc)
    return asdict(result)


@router.post(
    "/git/{provider}/resolve-checkout",
    response_model=GitCheckoutTarget,
    dependencies=[auth_dependency],
)
async def resolve_checkout_endpoint(
    provider: str,
    payload: GitCheckoutRequest,
    request: Request,
) -> GitCheckoutTarget:
    try:
        return await _provider(request, provider).resolve_git_checkout(payload)
    except ProviderError as exc:
        _raise_provider_http_error(exc)


@router.post(
    "/comments/{provider}/publish",
    response_model=CommentPublishResult,
    dependencies=[auth_dependency],
)
async def publish_comments_endpoint(
    provider: str,
    payload: CommentPublishRequest,
    request: Request,
) -> CommentPublishResult:
    try:
        return await _provider(request, provider).publish_comments(payload)
    except ProviderError as exc:
        _raise_provider_http_error(exc)


@router.post(
    "/query/{provider}",
    response_model=PlatformQueryResult,
    dependencies=[auth_dependency],
)
async def query_endpoint(
    provider: str,
    payload: PlatformQueryRequest,
    request: Request,
) -> PlatformQueryResult:
    try:
        return await _provider(request, provider).query(payload)
    except ProviderError as exc:
        _raise_provider_http_error(exc)
