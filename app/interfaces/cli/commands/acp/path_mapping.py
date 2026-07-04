from __future__ import annotations

from pathlib import Path

from app.config import Settings


def map_cwd(host_cwd: str | None, settings: Settings) -> str | None:
    """Map a host cwd to a container cwd for ACP sessions.

    Returns None if the cwd cannot be mapped. The caller (session/new)
    should reject the request instead of falling back to Path.cwd().
    """
    container_root = _container_root(settings)
    if container_root is None:
        return None

    # Rule 3: empty cwd -> container root
    if host_cwd is None or host_cwd.strip() == "":
        return str(container_root)

    cwd = Path(host_cwd).expanduser()
    # Reject relative paths -- they are ambiguous when the agent runs
    # in a container with a different CWD.
    if not cwd.is_absolute():
        return None

    try:
        cwd_resolved = cwd.resolve(strict=False)
    except (OSError, ValueError):
        return None

    # Rule 2: already in container root -> use as-is
    try:
        if cwd_resolved == container_root or container_root in cwd_resolved.parents:
            return str(cwd_resolved)
    except TypeError:
        # Path.parents comparison can fail on some platforms
        pass

    # Rule 1: under host root -> replace prefix with container root
    host_root = settings.acp_host_workspace_root
    if host_root is not None:
        try:
            host_root_resolved = host_root.expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return None
        try:
            if cwd_resolved == host_root_resolved or host_root_resolved in cwd_resolved.parents:
                relative = cwd_resolved.relative_to(host_root_resolved)
                return str(container_root / relative)
        except (ValueError, TypeError):
            pass

    # Rule 4: cannot map
    return None


def _container_root(settings: Settings) -> Path | None:
    if settings.acp_container_workspace_root is not None:
        try:
            return settings.acp_container_workspace_root.expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return None
    if settings.workspace_root is not None:
        return settings.workspace_root
    return None
