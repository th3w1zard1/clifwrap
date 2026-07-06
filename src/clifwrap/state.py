from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .config import state_dir


@dataclass
class UsageCacheEntry:
    provider: str
    account: str
    checked_at: int
    remaining: int | None
    detail: str
    error: str | None = None


@dataclass
class QueueItem:
    id: str
    provider: str
    argv: list[str]
    stdin_b64: str | None
    enqueued_at: int
    replay_count: int
    reason: str
    policy_snapshot: dict[str, Any]
    expires_at: int
    last_replayed_at: int | None = None


def _defaults_path():
    return state_dir() / "defaults.json"


def _usage_cache_path():
    return state_dir() / "usage-cache.json"


def _queue_path():
    return state_dir() / "queue.json"


def _load_json_dict(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _load_defaults() -> dict[str, str]:
    payload = _load_json_dict(_defaults_path())
    return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}


def get_default_account(provider: str) -> str | None:
    return _load_defaults().get(provider)


def set_default_account(provider: str, account: str) -> None:
    path = _defaults_path()
    payload = _load_defaults()
    payload[provider] = account
    _write_json(path, payload)


def clear_default_account(provider: str) -> None:
    path = _defaults_path()
    payload = _load_defaults()
    if provider not in payload:
        return
    del payload[provider]
    _write_json(path, payload)


def _load_usage_cache() -> dict[str, dict[str, dict[str, Any]]]:
    payload = _load_json_dict(_usage_cache_path())
    providers = payload.get("providers")
    return providers if isinstance(providers, dict) else {}


def get_usage_cache_entry(provider: str, account: str) -> UsageCacheEntry | None:
    raw = _load_usage_cache().get(provider, {}).get(account)
    if not isinstance(raw, dict):
        return None
    checked_at = raw.get("checked_at")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int):
        return None
    remaining = raw.get("remaining")
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int)):
        remaining = None
    detail = raw.get("detail")
    error = raw.get("error")
    if not isinstance(detail, str):
        return None
    if error is not None and not isinstance(error, str):
        error = None
    return UsageCacheEntry(
        provider=provider,
        account=account,
        checked_at=checked_at,
        remaining=remaining,
        detail=detail,
        error=error,
    )


def set_usage_cache_entry(entry: UsageCacheEntry) -> None:
    path = _usage_cache_path()
    payload = _load_json_dict(path)
    providers = payload.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        payload["providers"] = providers
    accounts = providers.setdefault(entry.provider, {})
    if not isinstance(accounts, dict):
        accounts = {}
        providers[entry.provider] = accounts
    accounts[entry.account] = asdict(entry)
    _write_json(path, payload)


def clear_usage_cache(provider: str | None = None) -> None:
    path = _usage_cache_path()
    if provider is None:
        if path.exists():
            path.unlink()
        return
    payload = _load_json_dict(path)
    providers = payload.get("providers")
    if not isinstance(providers, dict) or provider not in providers:
        return
    del providers[provider]
    _write_json(path, payload)


def _queue_item_from_raw(raw: dict[str, Any], *, index: int) -> QueueItem:
    required_str = ("id", "provider", "reason")
    for field_name in required_str:
        if not isinstance(raw.get(field_name), str) or not raw[field_name]:
            raise ValueError(f"queue item {index} missing valid {field_name}")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError(f"queue item {index} has invalid argv")
    stdin_b64 = raw.get("stdin_b64")
    if stdin_b64 is not None and not isinstance(stdin_b64, str):
        raise ValueError(f"queue item {index} has invalid stdin_b64")
    policy_snapshot = raw.get("policy_snapshot")
    if not isinstance(policy_snapshot, dict):
        raise ValueError(f"queue item {index} has invalid policy_snapshot")
    int_fields = ("enqueued_at", "replay_count", "expires_at")
    parsed_ints: dict[str, int] = {}
    for field_name in int_fields:
        value = raw.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"queue item {index} has invalid {field_name}")
        parsed_ints[field_name] = value
    last_replayed_at = raw.get("last_replayed_at")
    if last_replayed_at is not None and (isinstance(last_replayed_at, bool) or not isinstance(last_replayed_at, int)):
        raise ValueError(f"queue item {index} has invalid last_replayed_at")
    return QueueItem(
        id=raw["id"],
        provider=raw["provider"],
        argv=list(argv),
        stdin_b64=stdin_b64,
        enqueued_at=parsed_ints["enqueued_at"],
        replay_count=parsed_ints["replay_count"],
        reason=raw["reason"],
        policy_snapshot=dict(policy_snapshot),
        expires_at=parsed_ints["expires_at"],
        last_replayed_at=last_replayed_at,
    )


def list_queue_items(provider: str | None = None) -> list[QueueItem]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed queue state at {path}: {exc}") from exc
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError(f"Malformed queue state at {path}: top-level items must be an array")
    parsed = [_queue_item_from_raw(raw, index=index) for index, raw in enumerate(items)]
    if provider is None:
        return parsed
    return [item for item in parsed if item.provider == provider]


def save_queue_items(items: list[QueueItem]) -> None:
    _write_json(_queue_path(), {"items": [asdict(item) for item in items]})


def enqueue_queue_item(
    provider: str,
    argv: list[str],
    stdin_b64: str | None,
    reason: str,
    policy_snapshot: dict[str, Any],
    *,
    retention_seconds: int,
) -> QueueItem:
    now = int(time.time())
    item = QueueItem(
        id=uuid.uuid4().hex,
        provider=provider,
        argv=list(argv),
        stdin_b64=stdin_b64,
        enqueued_at=now,
        replay_count=0,
        reason=reason,
        policy_snapshot=dict(policy_snapshot),
        expires_at=now + retention_seconds,
    )
    items = list_queue_items()
    items.append(item)
    save_queue_items(items)
    return item


def replace_queue_item(updated_item: QueueItem) -> None:
    items = list_queue_items()
    replaced = False
    for index, item in enumerate(items):
        if item.id == updated_item.id:
            items[index] = updated_item
            replaced = True
            break
    if not replaced:
        raise ValueError(f"Unknown queue item: {updated_item.id}")
    save_queue_items(items)


def drop_queue_items(ids: set[str] | None = None, *, provider: str | None = None, expired_only: bool = False) -> list[QueueItem]:
    items = list_queue_items()
    kept: list[QueueItem] = []
    dropped: list[QueueItem] = []
    now = int(time.time())
    for item in items:
        matches_provider = provider is None or item.provider == provider
        matches_id = ids is None or item.id in ids
        matches_expired = not expired_only or item.expires_at <= now
        if matches_provider and matches_id and matches_expired:
            dropped.append(item)
            continue
        kept.append(item)
    save_queue_items(kept)
    return dropped
