"""Runtime registries populated by built-in and future user extensions."""

from __future__ import annotations

from wisp.providers.base import Provider


class UnknownProviderError(KeyError):
    """Raised when a provider name is not registered in the runtime."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"Unknown provider: {self.name}"


class ProviderRegistry:
    """Registry of model providers available to the agent runtime."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider, *, replace: bool = True) -> None:
        """Register a provider by its declared name."""

        if not replace and provider.name in self._providers:
            msg = f"Provider already registered: {provider.name}"
            raise ValueError(msg)
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        """Return a registered provider by name."""

        try:
            return self._providers[name]
        except KeyError as exc:
            raise UnknownProviderError(name) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered provider names in registration order."""

        return tuple(self._providers.keys())
