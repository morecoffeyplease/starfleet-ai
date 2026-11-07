"""Deterministic worktree-round and CPU admission policy."""

POLICY_VERSION = "warp-drive-admission-v8"
MAX_ACTIVE_AGED_ESCAPES = 2
MEMORY_SEVERITIES = frozenset(("normal", "warning", "critical", "unknown"))


def memory_severity(value):
    """Return one supported memory severity, treating legacy and invalid values as unknown."""
    return value if isinstance(value, str) and value in MEMORY_SEVERITIES else "unknown"


def memory_admission_reason(size, severity, active_light_count=0):
    """Return the memory-policy blocker for a reservation, if one applies."""
    severity = memory_severity(severity)
    if severity == "normal":
        return None
    if severity == "warning":
        if size != "light":
            return "memory warning pauses heavy work"
        if active_light_count:
            return "memory warning light reservation already active"
        return None
    if severity == "critical":
        return "critical memory pressure"
    return "memory severity unknown"


def memory_restricts_admission(severity):
    """Return whether memory severity narrows ordinary admission."""
    return memory_severity(severity) != "normal"


def is_memory_block_reason(reason):
    return reason in {
        "memory warning pauses heavy work",
        "memory warning light reservation already active",
        "critical memory pressure",
        "memory severity unknown",
    }


def estimated_cores(size, workers, target_cores, light_ready=False,
                    requested_cpu_cores=None):
    if requested_cpu_cores is None:
        per_worker = 1.0 if size == "light" else 4.0
        estimate = max(1.0, workers * per_worker)
    else:
        estimate = float(requested_cpu_cores)
    estimate = min(estimate, max(0.01, target_cores))
    if size == "heavy" and light_ready:
        estimate = min(estimate, max(0.01, target_cores - min(1.0, target_cores / 2)))
    return estimate


def request_estimate(request, target_cores, light_ready=False):
    """Return one request's effective admission estimate, preserving legacy fallback."""
    try:
        requested = request["requested_cpu_cores"]
    except (KeyError, IndexError, TypeError):
        try:
            requested = request["cpu_cores"]
        except (KeyError, IndexError, TypeError):
            requested = None
    return estimated_cores(request["size"], request["workers"], target_cores,
                           light_ready=light_ready,
                           requested_cpu_cores=requested)


def ordering_served_round(served_round):
    """Return the persisted service round used for ordering.

    Existing never-served rows retain round zero until their first service.
    Newly registered worktrees initialize their stored value to current-1.
    """
    return int(served_round)


def request_order_key(item):
    """Order a request by boost, missed worktree turns, manual order, then request details."""
    row = item["row"] if isinstance(item, dict) and "row" in item else item

    def value(source, key, default=None):
        try:
            return source[key]
        except (KeyError, IndexError, TypeError):
            return default

    boosted = value(item, "boosted", value(row, "priority", 0))
    served_round = value(row, "served_round", 0)
    position = value(item, "position", value(row, "order_position", 0))
    credit = value(item, "credit", value(row, "credit", 0))
    sequence = value(item, "sequence", value(row, "sequence", 0))
    identifier = value(row, "id", value(item, "id", ""))
    return (-int(bool(boosted)), ordering_served_round(served_round),
            int(position), -float(credit), int(sequence), str(identifier))


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
    available.sort(key=request_order_key)
    return available[0]


def admission_decision(request, *, target_cores, projected_cores, light_ready,
                       pressure, sample_fresh, now, last_heavy_grant,
                       heavy_wait_seconds, active_heavy_cores,
                       active_managed_cores=None, active_aged_escape_count=0,
                       active_light_count=0):
    """Return (allowed, reason, estimated_cores, drain_requested)."""
    if not sample_fresh:
        return False, "host sample stale or unavailable", 0.0, False
    memory_reason = memory_admission_reason(request["size"], pressure,
                                            active_light_count=active_light_count)
    if memory_reason is not None:
        return False, memory_reason, 0.0, False
    estimate = request_estimate(request, target_cores, light_ready=light_ready)
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
