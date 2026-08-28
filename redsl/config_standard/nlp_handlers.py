"""NLP agent tool handlers — callable Python implementations of CONFIG_AGENT_TOOLS.

These are the actual functions invoked when the LLM calls a tool via structured
function calling. Each handler receives a ``store`` context and validated
arguments, executes the tool's logic, and returns a JSON-serialisable result.

Critically:
- ``inspect_config`` never returns secret *values*, only metadata.
- ``test_api_key`` returns only {working: bool}, not the key itself.
- ``propose_changes`` is the *only* write path — returns a proposal that is
  later validated and applied by ConfigApplier.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .models import (
    CONFIRMATION_REQUIRED,
    ConfigChange,
    ConfigChangeProposal,
    ConfigPreconditions,
    ProposalMetadata,
    get_risk_level,
    search_schema_matches,
)
from .paths import get_nested_value
from .store import ConfigStore

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

SUPPORTED_TOOLS = frozenset(
    {"inspect_config", "search_config_schema", "propose_changes", "test_api_key"}
)


class ToolError(RuntimeError):
    """Raised when a tool call fails validation or execution."""


def dispatch_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    store: ConfigStore,
) -> dict[str, Any]:
    """Route an LLM tool call to the correct handler.

    Parameters
    ----------
    tool_name:
        Exact tool name from CONFIG_AGENT_TOOLS.
    arguments:
        Validated argument dict from structured output.
    store:
        ConfigStore providing read access to the manifest.

    Returns
    -------
    dict[str, Any]
        JSON-serialisable result that will be passed back to the LLM.

    Raises
    ------
    ToolError
        If the tool is unknown, arguments invalid, or execution fails.
    """
    if tool_name not in SUPPORTED_TOOLS:
        raise ToolError(f"Unknown tool: {tool_name!r}. Supported: {sorted(SUPPORTED_TOOLS)}")

    if tool_name == "inspect_config":
        return _handle_inspect_config(arguments, store=store)
    if tool_name == "search_config_schema":
        return _handle_search_config_schema(arguments)
    if tool_name == "propose_changes":
        return _handle_propose_changes(arguments, store=store)
    if tool_name == "test_api_key":
        return _handle_test_api_key(arguments, store=store)

    raise ToolError(f"Handler not implemented for tool: {tool_name!r}")  # unreachable


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------


def _handle_inspect_config(
    arguments: dict[str, Any], *, store: ConfigStore
) -> dict[str, Any]:
    """Return the current value at *path* — without exposing secret plaintext."""
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ToolError("inspect_config requires a non-empty 'path' argument")

    path = path.strip()
    doc = store.load()
    payload = doc.model_dump(mode="json")

    # Block direct exposure of secret values
    _assert_not_secret_value_path(path)

    value = get_nested_value(payload, path)
    risk = get_risk_level(path)
    requires_confirmation = risk in CONFIRMATION_REQUIRED

    return {
        "path": path,
        "value": value,
        "risk_level": risk,
        "requires_confirmation": requires_confirmation,
        "config_version": doc.metadata.version,
        "config_fingerprint": doc.metadata.fingerprint,
    }


def _handle_search_config_schema(arguments: dict[str, Any]) -> dict[str, Any]:
    """Full-text search over CONFIG_PATH_CATALOG."""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("search_config_schema requires a non-empty 'query' argument")

    matches = search_schema_matches(query.strip())
    return {
        "query": query,
        "matches": matches,
        "total": len(matches),
    }


def _handle_propose_changes(
    arguments: dict[str, Any], *, store: ConfigStore
) -> dict[str, Any]:
    """Validate and package a ConfigChangeProposal.

    The proposal is *not* applied here — it is returned to the caller for
    validation and (when required) human confirmation before ConfigApplier runs.
    """
    # Parse changes list
    raw_changes = arguments.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ToolError("propose_changes requires a non-empty 'changes' list")

    doc = store.load()
    payload = doc.model_dump(mode="json")

    changes: list[ConfigChange] = []
    blocked: list[str] = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ToolError(f"Each change must be a dict, got: {type(raw)}")
        path = raw.get("path", "")
        risk = get_risk_level(path)
        if risk == "unknown":
            blocked.append(path)
            continue

        # Fill in current_value from live config for audit trail
        current = get_nested_value(payload, path)
        change_data = {
            **raw,
            "current_value": current,
            "risk_level": risk,
            "requires_confirmation": risk in CONFIRMATION_REQUIRED,
        }
        # Secrets path: strip actual new_value if it's plaintext (safety net)
        if _is_secret_value_path(path) and "new_value" in change_data:
            _assert_no_plaintext_secret(change_data["new_value"])

        try:
            change = ConfigChange.model_validate(change_data)
        except Exception as exc:
            raise ToolError(f"Invalid change for path {path!r}: {exc}") from exc
        changes.append(change)

    if blocked:
        raise ToolError(
            f"NLP cannot modify unknown config paths: {blocked}. "
            "Use search_config_schema to find valid paths."
        )

    # Compute aggregate risk level
    all_risks = [str(c.risk_level) for c in changes]
    aggregate_risk = _highest_risk(all_risks)

    # Preconditions from current manifest
    preconditions = ConfigPreconditions(
        config_version=doc.metadata.version,
        config_fingerprint=doc.metadata.fingerprint,
    )

    summary = arguments.get("summary", "")
    proposal = ConfigChangeProposal(
        metadata=ProposalMetadata(
            source=f"nlp:{summary or 'user request'}",
            risk_level=aggregate_risk,  # type: ignore[arg-type]
        ),
        changes=changes,
        summary=summary,
        requires_new_secret=bool(arguments.get("requires_new_secret", False)),
        new_secret_name=arguments.get("new_secret_name"),
        preconditions=preconditions,
    )

    serialized = proposal.model_dump(mode="json")
    return {
        "proposal": serialized,
        "aggregate_risk": aggregate_risk,
        "requires_confirmation": aggregate_risk in CONFIRMATION_REQUIRED,
        "change_count": len(changes),
    }


def _handle_test_api_key(
    arguments: dict[str, Any], *, store: ConfigStore
) -> dict[str, Any]:
    """Check whether the configured API key for *provider* is working.

    Returns only {working: bool, ...} — the key itself is never returned.
    """
    provider = arguments.get("provider")
    if provider not in ("openrouter", "anthropic", "openai"):
        raise ToolError(
            f"test_api_key: unsupported provider {provider!r}. "
            "Choose from: openrouter, anthropic, openai"
        )

    doc = store.load()
    secret_name_map = {
        "openrouter": "openrouter_api_key",
        "anthropic": "anthropic_api_key",
        "openai": "openai_api_key",
    }
    secret_name = secret_name_map[provider]
    secret_spec = doc.secrets.get(secret_name)

    if secret_spec is None:
        return {
            "provider": provider,
            "working": False,
            "reason": f"No secret configured for {provider} (expected key: {secret_name!r})",
        }

    # Resolve and test without returning the key
    try:
        from .agent_bridge import resolve_secret_ref

        key_value = resolve_secret_ref(secret_spec)
    except Exception as exc:
        return {
            "provider": provider,
            "working": False,
            "reason": f"Cannot resolve secret ref {secret_spec.ref!r}: {exc}",
        }

    if not key_value:
        return {
            "provider": provider,
            "working": False,
            "reason": "Secret resolved to empty string",
        }

    working, reason = _smoke_test_provider(provider, key_value)
    return {
        "provider": provider,
        "working": working,
        "reason": reason,
        "key_ref": secret_spec.ref,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _highest_risk(risks: list[str]) -> str:
    if not risks:
        return "low"
    return max(risks, key=lambda r: _RISK_ORDER.get(r, -1))


_SECRET_PATH_RE = re.compile(r"\bsecrets\b")


def _is_secret_value_path(path: str) -> bool:
    return bool(_SECRET_PATH_RE.search(path))


def _assert_not_secret_value_path(path: str) -> None:
    """Raise if path points to a secret's *value* (not its ref/metadata)."""
    if path.endswith(".value") or path in ("secrets", "spec.secrets"):
        raise ToolError(
            f"inspect_config cannot expose secret values at path {path!r}. "
            "Use the path to metadata (e.g. spec.secrets.foo.ref) instead."
        )


def _assert_no_plaintext_secret(value: Any) -> None:
    """Raise if *value* looks like a plaintext secret key."""
    if not isinstance(value, str):
        return
    for pattern in (
        re.compile(r"sk-or-v1-[a-zA-Z0-9]{16,}"),
        re.compile(r"sk-[a-zA-Z0-9]{20,}"),
        re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"),
    ):
        if pattern.search(value):
            raise ToolError(
                "propose_changes received what appears to be a plaintext secret. "
                "Pass a [REDACTED_SECRET_N] placeholder instead — "
                "the system will resolve it via SecretInterceptor."
            )


def _smoke_test_provider(provider: str, key: str) -> tuple[bool, str]:
    """Make a minimal API call to verify the key works. Returns (working, reason)."""
    try:
        import urllib.request

        endpoints: dict[str, tuple[str, dict[str, str]]] = {
            "openrouter": (
                "https://openrouter.ai/api/v1/models",
                {
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": os.getenv(
                        "OPENROUTER_APP_URL", "https://github.com/autogrammar/redsl"
                    ),
                    "X-OpenRouter-Title": os.getenv(
                        "OPENROUTER_APP_NAME", "redsl-config-agent"
                    ),
                },
            ),
            "anthropic": (
                "https://api.anthropic.com/v1/models",
                {
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
            ),
            "openai": (
                "https://api.openai.com/v1/models",
                {"Authorization": f"Bearer {key}"},
            ),
        }
        url, headers = endpoints[provider]
        req = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status = resp.status
        if status == 200:
            return True, "OK"
        return False, f"HTTP {status}"
    except Exception as exc:
        return False, str(exc)


__all__ = [
    "SUPPORTED_TOOLS",
    "ToolError",
    "dispatch_tool",
]
