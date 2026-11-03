from __future__ import annotations

# The first Argo CD release accepting each value checked by `unknown-sync-option`
# and `unknown-resource-hook`, taken from the introducing argoproj/argo-cd commit
# and the earliest tag containing it (sourcing notes in DESIGN.md). A value
# absent from a table is not recognized in that context at any version.

Version = tuple[int, ...]


def parse_version(raw: str) -> Version:
    """Parse "3.2", "3.2.0" or "v3.2.0" into a comparable tuple of ints."""
    text = raw.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts = text.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a valid ArgoCD version: {raw!r}")
    return tuple(int(part) for part in parts)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def introduced_at_or_before(introduced: Version, declared: Version) -> bool:
    """Compare versions, treating missing trailing components as 0."""
    length = max(len(introduced), len(declared))
    introduced_padded = introduced + (0,) * (length - len(introduced))
    declared_padded = declared + (0,) * (length - len(declared))
    return introduced_padded <= declared_padded


# Application-level `spec.syncPolicy.syncOptions` keys.
APPLICATION_SYNC_OPTION_INTRODUCED: dict[str, Version] = {
    "Validate": (1, 2, 0),
    "SkipDryRunOnMissingResource": (1, 5, 3),
    "CreateNamespace": (1, 7, 0),
    "ApplyOutOfSyncOnly": (2, 0, 0),
    "PrunePropagationPolicy": (2, 0, 0),
    "PruneLast": (2, 0, 0),
    "Replace": (2, 0, 0),
    "FailOnSharedResource": (2, 2, 0),
    "RespectIgnoreDifferences": (2, 3, 0),
    "ServerSideApply": (2, 5, 0),
    "ClientSideApplyMigration": (3, 3, 0),
    # Before 3.4 these were only valid as per-resource annotations
    # (argoproj/argo-cd#23370).
    "Prune": (3, 4, 0),
    "Delete": (3, 4, 0),
}

# Per-resource `argocd.argoproj.io/sync-options` annotation keys. Some predate
# the same key at the Application level.
RESOURCE_SYNC_OPTION_INTRODUCED: dict[str, Version] = {
    "Prune": (1, 1, 0),
    "Validate": (1, 2, 0),
    "SkipDryRunOnMissingResource": (1, 5, 3),
    "PruneLast": (2, 0, 0),
    "Replace": (2, 0, 0),
    "Delete": (2, 7, 0),
    "ServerSideApply": (2, 5, 0),
    "Force": (2, 12, 0),
}

# `argocd.argoproj.io/hook` values.
HOOK_VALUE_INTRODUCED: dict[str, Version] = {
    "PreSync": (0, 6, 1),
    "Sync": (0, 6, 1),
    "Skip": (0, 6, 1),
    "PostSync": (0, 6, 1),
    "SyncFail": (1, 2, 0),
    "PostDelete": (2, 10, 0),
    "PreDelete": (3, 3, 0),
}

# `argocd.argoproj.io/hook-delete-policy` values.
HOOK_DELETE_POLICY_INTRODUCED: dict[str, Version] = {
    "HookSucceeded": (0, 11, 0),
    "HookFailed": (0, 11, 0),
    "BeforeHookCreation": (1, 3, 0),
}


def is_known(key: str, table: dict[str, Version], declared: Version | None) -> bool:
    """Whether `key` is in `table` and, if `declared` is given, available in that version."""
    introduced = table.get(key)
    if introduced is None:
        return False
    return declared is None or introduced_at_or_before(introduced, declared)
