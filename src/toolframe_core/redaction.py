"""Redact management views without changing the configuration used by workers."""

import re
from urllib.parse import quote

import tomlkit

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"password|passphrase|secret|token|api[_-]?key|authorization|credential",
    re.IGNORECASE,
)
URL_AUTH = re.compile(r"((?:git\+)?(?:https?|ssh)://)[^\s/@]+@", re.IGNORECASE)
URL_QUERY = re.compile(
    r"([?&](?:token|api[_-]?key|password|secret|authorization)=)[^&\s]+", re.IGNORECASE
)
BEARER = re.compile(r"(\bBearer\s+)[^\s\"']+", re.IGNORECASE)


def secret_values(value, schema=None, *, key="", root=None):
    schema = schema if isinstance(schema, dict) else {}
    root = schema if root is None else root
    reference = schema.get("$ref", "")
    if reference.startswith("#/"):
        resolved = root
        for part in reference[2:].split("/"):
            resolved = (
                resolved.get(part.replace("~1", "/").replace("~0", "~"), {})
                if isinstance(resolved, dict)
                else {}
            )
        schema = {**(resolved if isinstance(resolved, dict) else {}), **schema}
    protected = (
        SENSITIVE_KEY.search(key)
        or schema.get("writeOnly")
        or schema.get("x-secret")
        or schema.get("format") == "password"
    )
    if protected:
        if isinstance(value, str) and value:
            return {value}
        if type(value) in (int, float):
            return {str(value)}
    result = set()
    if isinstance(value, dict):
        for name, item in value.items():
            child = schema.get("properties", {}).get(name, {})
            if protected:
                child = {
                    **(child if isinstance(child, dict) else {}),
                    "writeOnly": True,
                }
            result.update(secret_values(item, child, key=name, root=root))
    elif isinstance(value, (list, tuple)):
        for item in value:
            child = schema.get("items", {})
            if protected:
                child = {
                    **(child if isinstance(child, dict) else {}),
                    "writeOnly": True,
                }
            result.update(secret_values(item, child, key=key, root=root))
    return result


def redact(value, secrets, *, field=""):
    if isinstance(value, dict):
        if field == "request" and "config" in value:
            value = {**value, "config": REDACTED}
        return {
            key: REDACTED
            if SENSITIVE_KEY.search(key)
            else redact(item, secrets, field=key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets, field=field) for item in value]
    if not isinstance(value, str):
        return (
            REDACTED if type(value) in (int, float) and str(value) in secrets else value
        )
    if value in secrets:
        return REDACTED
    # Content/generation IDs remain useful even with short numeric passwords.
    if re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{64}", value):
        return value
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4 or field in {
            "message",
            "error",
            "reason",
            "lines",
            "logs",
            "traceback",
            "stderr",
            "stdout",
        }:
            value = value.replace(secret, REDACTED).replace(
                quote(secret, safe=""), REDACTED
            )
    value = URL_AUTH.sub(r"\1[REDACTED]@", value)
    value = URL_QUERY.sub(r"\1[REDACTED]", value)
    return BEARER.sub(r"\1[REDACTED]", value)


def redact_toml(text, redactor):
    document = tomlkit.parse(text)
    sanitized = redactor(document.unwrap())

    def overlay(original, safe):
        for key, value in safe.items():
            if isinstance(value, dict):
                overlay(original[key], value)
            elif original[key] != value:
                original[key] = value

    overlay(document, sanitized)
    return tomlkit.dumps(document)
