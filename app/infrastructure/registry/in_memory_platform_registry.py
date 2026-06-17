from __future__ import annotations

from app.domain.platform import Platform, PlatformDescriptor, PlatformLifecycle


class InMemoryPlatformRegistry:
    def __init__(
        self,
        descriptors: list[PlatformDescriptor],
        lifecycles: dict[Platform, PlatformLifecycle] | None = None,
    ):
        self._descriptors: dict[Platform, PlatformDescriptor] = {
            descriptor.platform: descriptor for descriptor in descriptors
        }
        self._lifecycles: dict[Platform, PlatformLifecycle] = dict(lifecycles or {})

    def list(self) -> list[PlatformDescriptor]:
        return list(self._descriptors.values())

    def get(self, platform: Platform) -> PlatformDescriptor | None:
        return self._descriptors.get(platform)

    def get_lifecycle(self, platform: Platform) -> PlatformLifecycle | None:
        return self._lifecycles.get(platform)
