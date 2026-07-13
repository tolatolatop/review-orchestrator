from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from github import Auth, GithubIntegration


class GitHubAuthenticationError(RuntimeError):
    pass


class GitHubTokenProvider(Protocol):
    async def get_token(self, repo_full_name: str) -> str | None: ...

    async def aclose(self) -> None: ...


class StaticGitHubTokenProvider:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def get_token(self, repo_full_name: str) -> str | None:
        del repo_full_name
        return self._token

    async def aclose(self) -> None:
        return None


class GitHubAppTokenProvider:
    """Resolve repositories to GitHub App installations and refresh their tokens."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_path: str,
        api_base_url: str = "https://api.github.com",
        installation_id: int | None = None,
    ) -> None:
        try:
            private_key = Path(private_key_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise GitHubAuthenticationError(
                f"Unable to read GitHub App private key: {private_key_path}"
            ) from exc

        try:
            self._app_auth = Auth.AppAuth(app_id, private_key)
            # Signing a JWT validates the App ID and PEM without making a request.
            _ = self._app_auth.token
        except Exception as exc:
            raise GitHubAuthenticationError(
                "Unable to create a GitHub App JWT; check GITHUB_APP_ID and the "
                "private key PEM."
            ) from exc
        self._integration = GithubIntegration(
            auth=self._app_auth,
            base_url=api_base_url.rstrip("/"),
        )
        self._default_installation_id = installation_id
        self._installation_ids: dict[str, int] = {}
        self._installation_auth: dict[int, Auth.AppInstallationAuth] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, repo_full_name: str) -> str:
        installation_id = await self._installation_id(repo_full_name)
        try:
            return await self._token_for_installation(installation_id)
        except Exception as exc:
            if self._default_installation_id is not None:
                raise GitHubAuthenticationError(
                    "Unable to obtain a GitHub App token for installation "
                    f"{installation_id}."
                ) from exc

            # An app can be uninstalled and reinstalled while this process remains up.
            # Discard stale repository/installation mappings and resolve it once more.
            normalized = repo_full_name.strip().lower()
            async with self._lock:
                self._installation_ids.pop(normalized, None)
                self._installation_auth.pop(installation_id, None)
            replacement_id = await self._installation_id(repo_full_name)
            try:
                return await self._token_for_installation(replacement_id)
            except Exception as retry_exc:
                raise GitHubAuthenticationError(
                    "Unable to obtain a GitHub App token for repository "
                    f"{repo_full_name}."
                ) from retry_exc

    async def aclose(self) -> None:
        await asyncio.to_thread(self._integration.close)

    async def _token_for_installation(self, installation_id: int) -> str:
        async with self._lock:
            auth = self._installation_auth.get(installation_id)
            if auth is None:
                auth = self._app_auth.get_installation_auth(
                    installation_id,
                    requester=self._integration.requester,
                )
                self._installation_auth[installation_id] = auth
            # PyGithub refreshes AppInstallationAuth.token automatically before expiry.
            return await asyncio.to_thread(lambda: auth.token)

    async def _installation_id(self, repo_full_name: str) -> int:
        if self._default_installation_id is not None:
            return self._default_installation_id

        normalized = repo_full_name.strip().lower()
        cached = self._installation_ids.get(normalized)
        if cached is not None:
            return cached

        owner, separator, repo = repo_full_name.partition("/")
        if not separator or not owner or not repo:
            raise GitHubAuthenticationError(
                f"Invalid GitHub repository name: {repo_full_name}"
            )
        try:
            installation = await asyncio.to_thread(
                self._integration.get_installation,
                owner,
                repo,
            )
        except Exception as exc:
            raise GitHubAuthenticationError(
                f"Unable to resolve GitHub App installation for {repo_full_name}."
            ) from exc

        async with self._lock:
            self._installation_ids[normalized] = installation.id
        return installation.id
