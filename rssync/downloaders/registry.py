"""Downloader backend discovery and worker-local instance management."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from threading import Lock, local
from typing import Any

from rssync.config import DownloaderPresetConfig
from rssync.downloaders.base import (
    Downloader,
    DownloaderBackendFactory,
    DownloaderRuntimeContext,
)
from rssync.downloaders.requests_backend import RequestsBackendFactory

ENTRY_POINT_GROUP = "rssync.downloaders"


class DownloaderRegistryError(ValueError):
    """Raised when backend discovery or preset construction fails."""


class DownloaderRegistry:
    """Registry containing built-in and entry-point downloader factories."""

    def __init__(self, *, load_plugins: bool = True) -> None:
        self._factories: dict[str, DownloaderBackendFactory] = {}
        self.register("requests", RequestsBackendFactory())
        if load_plugins:
            self.load_entry_points()

    def register(self, name: str, factory: DownloaderBackendFactory) -> None:
        """Register one backend factory under a unique name."""

        if not name or name in self._factories:
            raise DownloaderRegistryError(f"duplicate downloader backend: {name}")
        if not callable(getattr(factory, "validate_options", None)) or not callable(
            getattr(factory, "create", None)
        ):
            raise DownloaderRegistryError(
                f"downloader backend {name!r} does not implement the factory protocol"
            )
        self._factories[name] = factory

    def load_entry_points(self) -> None:
        """Load third-party factories from ``rssync.downloaders``."""

        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
            loaded = entry_point.load()
            factory = loaded() if isinstance(loaded, type) else loaded
            self.register(entry_point.name, factory)

    def factory(self, name: str) -> DownloaderBackendFactory:
        """Return a registered backend factory."""

        try:
            return self._factories[name]
        except KeyError as error:
            raise DownloaderRegistryError(
                f"unknown downloader backend: {name}"
            ) from error


@dataclass(frozen=True, slots=True)
class _ValidatedPreset:
    config: DownloaderPresetConfig
    factory: DownloaderBackendFactory
    options: Any


class DownloaderManager:
    """Create one downloader instance per preset and worker thread."""

    def __init__(
        self,
        presets: Mapping[str, DownloaderPresetConfig],
        registry: DownloaderRegistry | None = None,
    ) -> None:
        self.registry = registry or DownloaderRegistry()
        self.runtime = DownloaderRuntimeContext()
        self._presets: dict[str, _ValidatedPreset] = {}
        for name, preset in presets.items():
            factory = self.registry.factory(preset.backend)
            try:
                options = factory.validate_options(preset.options)
            except (TypeError, ValueError) as error:
                raise DownloaderRegistryError(
                    f"invalid options for downloader preset {name!r}: {error}"
                ) from error
            self._presets[name] = _ValidatedPreset(preset, factory, options)
        self._local = local()
        self._instances: list[Downloader] = []
        self._instances_lock = Lock()

    def get(self, preset_name: str) -> Downloader:
        """Return this worker thread's instance for a preset."""

        instances = getattr(self._local, "instances", None)
        if instances is None:
            instances = {}
            self._local.instances = instances
        if preset_name not in instances:
            preset = self._presets[preset_name]
            instance = preset.factory.create(preset.options, self.runtime)
            instances[preset_name] = instance
            with self._instances_lock:
                self._instances.append(instance)
        return instances[preset_name]

    def backend_name(self, preset_name: str) -> str:
        """Return the configured backend name for a preset."""

        return self._presets[preset_name].config.backend

    def close(self) -> None:
        """Close all downloader instances created during the run."""

        with self._instances_lock:
            instances, self._instances = self._instances, []
        for instance in instances:
            instance.close()
