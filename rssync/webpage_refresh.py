"""Pluggable policies for deciding when archived webpages are refreshed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebpageRefreshContext:
    """Read-only inputs available to a webpage refresh strategy."""

    canonical_url: str
    feed_url: str
    cache_valid: bool
    rss_changed: bool


class WebpageRefreshStrategy(Protocol):
    """Decide whether one RSS webpage reference should be downloaded."""

    name: str

    def should_fetch(self, context: WebpageRefreshContext) -> bool:
        """Return whether the referenced webpage needs a network request."""


class WebpageRefreshRegistryError(ValueError):
    """Raised when refresh strategy registration or resolution fails."""


class AlwaysRefreshStrategy:
    """Refresh every webpage reference on every successful RSS fetch."""

    name = "always"

    def should_fetch(self, context: WebpageRefreshContext) -> bool:
        return True


class OnRssChangeStrategy:
    """Refresh existing webpages only after a meaningful RSS change."""

    name = "on-rss-change"

    def should_fetch(self, context: WebpageRefreshContext) -> bool:
        return not context.cache_valid or context.rss_changed


class MissingOnlyStrategy:
    """Treat a valid archived webpage as immutable."""

    name = "missing-only"

    def should_fetch(self, context: WebpageRefreshContext) -> bool:
        return not context.cache_valid


class WebpageRefreshRegistry:
    """Register and resolve webpage refresh strategies by configuration name."""

    def __init__(
        self,
        strategies: Iterable[WebpageRefreshStrategy] = (),
    ) -> None:
        self._strategies: dict[str, WebpageRefreshStrategy] = {}
        for strategy in strategies:
            self.register(strategy)

    @property
    def strategies(self) -> Mapping[str, WebpageRefreshStrategy]:
        """Return an immutable view of registered strategies."""

        return MappingProxyType(self._strategies)

    def register(self, strategy: WebpageRefreshStrategy) -> None:
        """Register one strategy and reject invalid or duplicate names."""

        name = strategy.name
        if not isinstance(name, str) or not name:
            raise WebpageRefreshRegistryError(
                "webpage refresh strategy name must be non-empty"
            )
        if name in self._strategies:
            raise WebpageRefreshRegistryError(
                f"duplicate webpage refresh strategy: {name}"
            )
        if not callable(getattr(strategy, "should_fetch", None)):
            raise WebpageRefreshRegistryError(
                f"webpage refresh strategy {name!r} does not implement "
                "the strategy protocol"
            )
        self._strategies[name] = strategy

    def resolve(self, name: str) -> WebpageRefreshStrategy:
        """Resolve a strategy or raise a configuration-friendly error."""

        try:
            return self._strategies[name]
        except KeyError as error:
            raise WebpageRefreshRegistryError(
                f"unknown webpage refresh strategy: {name}"
            ) from error


def default_webpage_refresh_registry() -> WebpageRefreshRegistry:
    """Create a fresh registry containing all built-in strategies."""

    return WebpageRefreshRegistry(
        (
            AlwaysRefreshStrategy(),
            OnRssChangeStrategy(),
            MissingOnlyStrategy(),
        )
    )
