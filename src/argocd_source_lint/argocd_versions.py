from __future__ import annotations

# The ArgoCD version each closed-vocabulary value used by `unknown-sync-option`
# and `unknown-resource-hook` first became valid in. Every entry was traced to
# the real argoproj/argo-cd commit that introduced it and the earliest release
# tag containing that commit; never estimated. See DESIGN.md for the
# sourcing methodology and the handful of cases where documentation lagged
# behind the actual code change.
#
# A value absent from one of these tables isn't recognized in that context at
# any version: same meaning as before this module existed.

Version = tuple[int, ...]


def parse_version(raw: str) -> Version:
    """Parses "3.2", "3.2.0", or "v3.2.0" into a comparable tuple of ints."""
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
    """Compares two version tuples that may have different lengths, treating
    a missing trailing component as 0 (so (3, 2) == (3, 2, 0))."""
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
    # Unlike every other key above, these two were NOT valid at the
    # Application level before this; they only existed as the per-resource
    # `argocd.argoproj.io/sync-options` annotation (see
    # RESOURCE_SYNC_OPTION_INTRODUCED). Confirmed via the real code commit
    # (argoproj/argo-cd#23370, "feat: add Prune and Delete as application
    # level sync option"), not just a docs change.
    "Prune": (3, 4, 0),
    "Delete": (3, 4, 0),
}

# Per-resource `argocd.argoproj.io/sync-options` annotation keys: a
# different, smaller set with its own timing, sometimes much earlier than the
# same string at the Application level.
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
    """Whether `key` is recognized by `table`, optionally narrowed to what's
    actually available as of `declared` (a parsed `argocd_version`). Passing
    `declared=None` reproduces the pre-versioning behavior: any key present in
    the table at all is considered known, regardless of when it first shipped."""
    introduced = table.get(key)
    if introduced is None:
        return False
    return declared is None or introduced_at_or_before(introduced, declared)
