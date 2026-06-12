import json
import re
from pathlib import Path
from typing import Any


MEMORY_PATH = Path(__file__).with_name("client_memory.json")


def _default_profile(client_name: str) -> dict[str, Any]:
    return {
        "client_name": client_name,
        "past_projects": [],
        "observed_patterns": [],
        "preferred_tools": [],
        "avoided_tools": [],
        "session_observations": [],
    }


def _load_memory() -> dict[str, Any]:
    if not MEMORY_PATH.exists():
        return {"clients": {}}

    with MEMORY_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return {"clients": {}}

    data.setdefault("clients", {})
    return data


def _save_memory(data: dict[str, Any]) -> None:
    with MEMORY_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")


def _normalize_key(client_name: str) -> str:
    cleaned = re.sub(r"\s+", " ", client_name).strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    return cleaned


def _candidate_keys(client_name: str) -> list[str]:
    raw = client_name.strip()
    candidates: list[str] = []

    def add(value: str) -> None:
        normalized = _normalize_key(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(raw)

    simplified = re.sub(
        r"\b(yes|yeah|yep|yup|existing|new|client|customer|for|is|this|its|it's|an|a)\b",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    simplified = re.sub(r"[:\-]+", " ", simplified)
    simplified = re.sub(r"\s+", " ", simplified).strip()
    add(simplified)

    if "," in raw:
        add(raw.split(",", 1)[0])
        add(raw.split(",", 1)[1])

    return candidates


def _merge_unique(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    seen = {item.casefold() for item in existing}

    for item in additions:
        cleaned = item.strip()
        if not cleaned:
            continue
        if cleaned.casefold() in seen:
            continue
        merged.append(cleaned)
        seen.add(cleaned.casefold())

    return merged


def get_client_profile(client_name: str) -> dict[str, Any]:
    memory = _load_memory()
    profile = None

    for candidate in _candidate_keys(client_name):
        profile = memory["clients"].get(candidate)
        if profile is not None:
            break

    if profile is None:
        return {
            "client_name": client_name,
            "found": False,
            "message": "No client profile found yet.",
        }

    return {
        "found": True,
        "matched_client_name": profile.get("client_name", client_name),
        **profile,
    }


def update_client_profile(
    client_name: str,
    *,
    past_projects: list[str] | None = None,
    observed_patterns: list[str] | None = None,
    preferred_tools: list[str] | None = None,
    avoided_tools: list[str] | None = None,
    session_observations: list[str] | None = None,
) -> dict[str, Any]:
    memory = _load_memory()
    client_key = _normalize_key(client_name)
    existing = memory["clients"].get(client_key, _default_profile(client_name))

    existing["client_name"] = existing.get("client_name") or client_name
    existing["past_projects"] = _merge_unique(
        existing.get("past_projects", []), past_projects or []
    )
    existing["observed_patterns"] = _merge_unique(
        existing.get("observed_patterns", []), observed_patterns or []
    )
    existing["preferred_tools"] = _merge_unique(
        existing.get("preferred_tools", []), preferred_tools or []
    )
    existing["avoided_tools"] = _merge_unique(
        existing.get("avoided_tools", []), avoided_tools or []
    )
    existing["session_observations"] = _merge_unique(
        existing.get("session_observations", []), session_observations or []
    )

    memory["clients"][client_key] = existing
    _save_memory(memory)

    return {
        "updated": True,
        **existing,
    }
