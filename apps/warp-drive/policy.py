"""Deterministic worktree-round and CPU admission policy."""

POLICY_VERSION = "warp-drive-admission-v5"
MAX_ACTIVE_AGED_ESCAPES = 2


def estimated_cores(size, workers, target_cores, light_ready=False):
    per_worker = 1.0 if size == "light" else 4.0
    estimate = max(1.0, workers * per_worker)
    estimate = min(estimate, max(0.01, target_cores))
    if size == "heavy" and light_ready:
        estimate = min(estimate, max(0.01, target_cores - min(1.0, target_cores / 2)))
    return estimate


def choose_worktree(rows):
    """Return the next worktree in a persisted round, or None when empty.

    Each row has id, served_round, order_position, boosted, credit, and sequence.
    """
    if not rows:
        return None
    current_round = min(row["round"] for row in rows)
    available = [row for row in rows if row["served_round"] < current_round]
    if not available:
        current_round += 1
        for row in rows:
            row["round"] = current_round
        available = rows
    available.sort(key=lambda row: (-int(row["boosted"]), row["order_position"],
                                    -row["credit"], row["sequence"], row["id"]))
    return available[0]


def admission_decision(request, *, target_cores, projected_cores, light_ready,
                       pressure, sample_fresh, now, last_heavy_grant,
                       heavy_wait_seconds, active_heavy_cores,
                       active_managed_cores=None, active_aged_escape_count=0):
    """Return (allowed, reason, estimated_cores, drain_requested)."""
    if not sample_fresh:
        return False, "host sample stale or unavailable", 0.0, False
    if pressure not in ("normal", None):
        return False, "memory pressure observed", 0.0, False
    estimate = estimated_cores(request["size"], request["workers"], target_cores,
                               light_ready=light_ready)
    if request["size"] == "light":
        if projected_cores + estimate > target_cores:
            return False, "host target has no room for light work", estimate, False
        return True, "light allowance admitted", estimate, False
    heavy_budget = target_cores
    if light_ready:
        heavy_budget -= min(1.0, target_cores / 2)
    if projected_cores + estimate <= heavy_budget:
        if last_heavy_grant is not None and now - last_heavy_grant < 5:
            return False, "heavy startup observation interval", estimate, False
        return True, "heavy work fits observed host headroom", estimate, False
    if (heavy_wait_seconds >= 120 and
            projected_cores <= heavy_budget / 2):
        if active_aged_escape_count >= MAX_ACTIVE_AGED_ESCAPES:
            return False, "aged escape limit reached", estimate, False
        if last_heavy_grant is not None and now - last_heavy_grant < 5:
            return False, "heavy startup observation interval", estimate, False
        return True, "aged heavy escape with meaningful host headroom", estimate, False
    if heavy_wait_seconds >= 120 and active_aged_escape_count >= MAX_ACTIVE_AGED_ESCAPES:
        return False, "aged escape limit reached", estimate, False
    if heavy_wait_seconds >= 120 and active_aged_escape_count > 0:
        return False, "host target leaves insufficient heavy capacity", estimate, False
    if active_managed_cores is None:
        active_managed_cores = active_heavy_cores
    unmanaged_estimate = max(0.0, projected_cores - active_managed_cores)
    if (heavy_wait_seconds >= 120 and active_managed_cores > 0 and
            unmanaged_estimate <= heavy_budget / 2):
        return False, "draining managed capacity for aged heavy work", estimate, True
    return False, "host target leaves insufficient heavy capacity", estimate, False
