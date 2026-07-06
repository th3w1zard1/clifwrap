from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from .config import AccountConfig, CapacityControlConfig, ProviderConfig
from .state import QueueItem, UsageCacheEntry, drop_queue_items, enqueue_queue_item, get_usage_cache_entry, list_queue_items, replace_queue_item, set_usage_cache_entry


@dataclass
class CapacitySnapshot:
    account_name: str
    remaining: int | None
    detail: str
    checked_at: int
    error: str | None = None
    from_cache: bool = False

    @property
    def known(self) -> bool:
        return self.remaining is not None and self.error is None


@dataclass
class AdmissionDecision:
    action: str
    reason: str
    estimated_cost: int
    reserve_threshold: int
    account_name: str | None = None
    capacity_approved: bool = False
    unknown_capacity: bool = False
    queue_item: QueueItem | None = None
    remediation_message: str | None = None


def has_capacity_control(provider: ProviderConfig) -> bool:
    return provider.capacity_control is not None


def estimate_command_cost(provider: ProviderConfig, args: list[str]) -> int:
    capacity = provider.capacity_control
    if not capacity:
        return 0
    if not args:
        return capacity.default_cost
    command_key = " ".join(args[:2]) if len(args) >= 2 else args[0]
    if command_key in capacity.command_costs:
        return capacity.command_costs[command_key]
    if args[0] in capacity.command_costs:
        return capacity.command_costs[args[0]]
    return capacity.default_cost


def _policy_snapshot(capacity: CapacityControlConfig, estimated_cost: int) -> dict[str, object]:
    return {
        "default_action": capacity.default_action,
        "unknown_capacity_action": capacity.unknown_capacity_action,
        "reserve_threshold": capacity.reserve_threshold,
        "default_cost": capacity.default_cost,
        "estimated_cost": estimated_cost,
        "queue_retention_seconds": capacity.queue_retention_seconds,
        "queue_max_items": capacity.queue_max_items,
        "snapshot_ttl_seconds": capacity.snapshot_ttl_seconds,
        "remediation_message": capacity.remediation_message,
        "remediation_commands": list(capacity.remediation_commands),
    }


def remediation_message(provider: ProviderConfig) -> str | None:
    capacity = provider.capacity_control
    if not capacity:
        return None
    if capacity.remediation_message:
        return capacity.remediation_message
    if capacity.remediation_commands:
        return "Remediation: " + " | ".join(capacity.remediation_commands)
    return None


def capacity_snapshots(
    provider: ProviderConfig,
    accounts: list[AccountConfig],
    snapshot_fetcher,
) -> list[CapacitySnapshot]:
    capacity = provider.capacity_control
    ttl = capacity.snapshot_ttl_seconds if capacity else 60
    now = int(time.time())
    snapshots: list[CapacitySnapshot] = []
    for account in accounts:
        try:
            detail, remaining = snapshot_fetcher(account)
            snapshot = CapacitySnapshot(
                account_name=account.name,
                remaining=remaining,
                detail=detail,
                checked_at=now,
            )
            if capacity:
                set_usage_cache_entry(
                    UsageCacheEntry(
                        provider=provider.name,
                        account=account.name,
                        checked_at=now,
                        remaining=remaining,
                        detail=detail,
                        error=None,
                    )
                )
            snapshots.append(snapshot)
        except Exception as exc:
            cache = get_usage_cache_entry(provider.name, account.name)
            if cache and now - cache.checked_at <= ttl:
                snapshots.append(
                    CapacitySnapshot(
                        account_name=account.name,
                        remaining=cache.remaining,
                        detail=cache.detail,
                        checked_at=cache.checked_at,
                        error=cache.error,
                        from_cache=True,
                    )
                )
                continue
            snapshots.append(
                CapacitySnapshot(
                    account_name=account.name,
                    remaining=None,
                    detail="usage unavailable",
                    checked_at=now,
                    error=str(exc),
                )
            )
    return snapshots


def _enqueue_decision(
    provider: ProviderConfig,
    args: list[str],
    stdin_data: bytes | None,
    reason: str,
    estimated_cost: int,
    existing_item: QueueItem | None = None,
) -> AdmissionDecision:
    assert provider.capacity_control is not None
    capacity = provider.capacity_control
    drop_queue_items(provider=provider.name, expired_only=True)
    queue_items = list_queue_items(provider.name)
    if existing_item is None and len(queue_items) >= capacity.queue_max_items:
        return AdmissionDecision(
            action="shed",
            reason=f"queue full for {provider.name} ({len(queue_items)}/{capacity.queue_max_items})",
            estimated_cost=estimated_cost,
            reserve_threshold=capacity.reserve_threshold,
            remediation_message=remediation_message(provider),
        )
    if existing_item is None:
        queue_item = enqueue_queue_item(
            provider.name,
            args,
            base64.b64encode(stdin_data).decode("ascii") if stdin_data is not None else None,
            reason,
            _policy_snapshot(capacity, estimated_cost),
            retention_seconds=capacity.queue_retention_seconds,
        )
    else:
        existing_item.replay_count += 1
        existing_item.last_replayed_at = int(time.time())
        existing_item.reason = reason
        queue_item = existing_item
        replace_queue_item(existing_item)
    return AdmissionDecision(
        action="queue",
        reason=reason,
        estimated_cost=estimated_cost,
        reserve_threshold=capacity.reserve_threshold,
        queue_item=queue_item,
        remediation_message=remediation_message(provider),
    )


def admission_decision(
    provider: ProviderConfig,
    args: list[str],
    snapshots: list[CapacitySnapshot],
    *,
    active_account_name: str | None,
    stdin_data: bytes | None,
    existing_item: QueueItem | None = None,
) -> AdmissionDecision:
    capacity = provider.capacity_control
    if not capacity:
        return AdmissionDecision(action="execute", reason="capacity control disabled", estimated_cost=0, reserve_threshold=0)
    estimated_cost = estimate_command_cost(provider, args)
    required_remaining = estimated_cost + capacity.reserve_threshold
    named = {snapshot.account_name: snapshot for snapshot in snapshots}
    if active_account_name and active_account_name in named:
        active_snapshot = named[active_account_name]
        if active_snapshot.known and active_snapshot.remaining >= required_remaining:
            return AdmissionDecision(
                action="execute",
                reason=f"{active_account_name} has remaining {active_snapshot.remaining} >= required {required_remaining}",
                estimated_cost=estimated_cost,
                reserve_threshold=capacity.reserve_threshold,
                account_name=active_account_name,
                capacity_approved=True,
            )
    for snapshot in snapshots:
        if snapshot.known and snapshot.remaining >= required_remaining:
            return AdmissionDecision(
                action="execute",
                reason=f"{snapshot.account_name} selected with remaining {snapshot.remaining} >= required {required_remaining}",
                estimated_cost=estimated_cost,
                reserve_threshold=capacity.reserve_threshold,
                account_name=snapshot.account_name,
                capacity_approved=True,
            )
    if snapshots and all(not snapshot.known for snapshot in snapshots):
        if capacity.unknown_capacity_action == "allow":
            return AdmissionDecision(
                action="execute",
                reason="usage is unavailable for every account and policy allows execution",
                estimated_cost=estimated_cost,
                reserve_threshold=capacity.reserve_threshold,
                account_name=active_account_name or (snapshots[0].account_name if snapshots else None),
                unknown_capacity=True,
                remediation_message=remediation_message(provider),
            )
        if capacity.unknown_capacity_action == "queue":
            return _enqueue_decision(
                provider,
                args,
                stdin_data,
                "usage is unavailable for every account and policy requires queueing",
                estimated_cost,
                existing_item=existing_item,
            )
        return AdmissionDecision(
            action="shed",
            reason="usage is unavailable for every account and policy denies upstream execution",
            estimated_cost=estimated_cost,
            reserve_threshold=capacity.reserve_threshold,
            unknown_capacity=True,
            remediation_message=remediation_message(provider),
        )
    if capacity.default_action == "execute":
        return AdmissionDecision(
            action="execute",
            reason="no account met the reserve threshold, but policy allows execution",
            estimated_cost=estimated_cost,
            reserve_threshold=capacity.reserve_threshold,
            account_name=active_account_name or (snapshots[0].account_name if snapshots else None),
            remediation_message=remediation_message(provider),
        )
    if capacity.default_action == "queue":
        return _enqueue_decision(
            provider,
            args,
            stdin_data,
            f"no account has the required remaining capacity ({required_remaining})",
            estimated_cost,
            existing_item=existing_item,
        )
    return AdmissionDecision(
        action="shed",
        reason=f"no account has the required remaining capacity ({required_remaining})",
        estimated_cost=estimated_cost,
        reserve_threshold=capacity.reserve_threshold,
        remediation_message=remediation_message(provider),
    )
