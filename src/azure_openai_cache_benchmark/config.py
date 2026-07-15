from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError
from .serialization import sha256_text

DOTENV_ALLOWED_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_KEY")


def load_allowed_env_file(
    path: Path,
    environ: MutableMapping[str, str],
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "found": False,
            "name": resolved.name,
            "loaded_keys": [],
            "present_keys": [],
        }
    if not resolved.is_file():
        raise ConfigurationError(f"Dotenv path is not a file: {resolved}")
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise ConfigurationError(
            "python-dotenv is required. Install the package dependencies."
        ) from exc
    values = dotenv_values(resolved, interpolate=False)
    present_keys = sorted(key for key in DOTENV_ALLOWED_KEYS if key in values)
    loaded_keys: list[str] = []
    for key in DOTENV_ALLOWED_KEYS:
        raw = values.get(key)
        value = str(raw).strip() if raw is not None else ""
        if value and not environ.get(key, "").strip():
            environ[key] = value
            loaded_keys.append(key)
    return {
        "found": True,
        "name": resolved.name,
        "loaded_keys": sorted(loaded_keys),
        "present_keys": present_keys,
    }


def normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ConfigurationError("Azure OpenAI base URL is empty.")
    parsed = urlsplit(raw)
    hostname = parsed.hostname or ""
    is_loopback = hostname.casefold() == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("Base URL must not contain user information.")
    scheme_allowed = parsed.scheme == "https" or (
        parsed.scheme == "http" and is_loopback
    )
    if not scheme_allowed or not parsed.netloc:
        raise ConfigurationError(
            "Base URL must be absolute HTTPS, or HTTP on a loopback proxy."
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "Azure OpenAI base URL must not contain query or fragment data."
        )
    path = parsed.path.rstrip("/")
    lowered = path.casefold()
    if "/openai/deployments/" in lowered:
        raise ConfigurationError(
            "Use the Azure OpenAI v1 root, not a deployment-specific URL."
        )
    if lowered.endswith("/openai/v1"):
        normalized_path = path + "/"
    elif is_loopback and lowered.endswith("/v1"):
        normalized_path = path + "/"
    elif is_loopback and not path:
        normalized_path = "/v1/"
    elif lowered.endswith("/openai"):
        normalized_path = path + "/v1/"
    elif not path:
        normalized_path = "/openai/v1/"
    else:
        normalized_path = path + "/openai/v1/"
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def resolve_endpoint_env(env: Mapping[str, str]) -> tuple[str, str]:
    base_url = env.get("OPENAI_BASE_URL", "").strip()
    if base_url:
        return normalize_base_url(base_url), "OPENAI_BASE_URL"
    raise ConfigurationError("Set OPENAI_BASE_URL for Azure OpenAI v1.")


def endpoint_fingerprint(base_url: str) -> dict[str, str]:
    parsed = urlsplit(base_url)
    return {
        "scheme": parsed.scheme,
        "hostname_sha256": sha256_text(parsed.hostname or ""),
        "path": parsed.path,
    }


def redact_text(
    value: str,
    secrets: Iterable[str] = (),
    base_url: str | None = None,
) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
    if base_url:
        parsed = urlsplit(base_url)
        for endpoint_value in (base_url, parsed.netloc, parsed.hostname or ""):
            if endpoint_value:
                redacted = redacted.replace(
                    endpoint_value,
                    "[REDACTED_ENDPOINT]",
                )
    return re.sub(
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED_SECRET]",
        redacted,
    )
