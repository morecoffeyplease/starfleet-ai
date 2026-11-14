"""Persist one versioned handoff freeze without native side effects."""

from dataclasses import dataclass
import copy
import re
import time
import uuid

import access_observer
import successor_intents


HANDOFF_PROTOCOL = successor_intents.HANDOFF_PROTOCOL
HANDOFF_ID = re.compile(r"[0-9a-f]{32}\Z")
UNKNOWN = {"status": "unknown", "value": None, "evidence": None}
UNKNOWN_BINDING = {"status": "unknown", "hook_generation": None, "evidence": None}
OBSERVATIONS = (
    "predecessor_model",
    "predecessor_effort",
    "successor_model",
    "successor_effort",
    "effective_permissions",
)


class HandoffError(ValueError):
    pass


@dataclass(frozen=True)
class _ConfirmedTransferReceipt:
    handoff_id: str
    intent_id: str
    intent_digest: str
    receipt_id: str


def _find_handoff(store, handoff_id):
    if not isinstance(handoff_id, str) or HANDOFF_ID.fullmatch(handoff_id) is None:
        raise HandoffError("Invalid handoff ID.")
    handoff = store["handoffs"].get(handoff_id)
    if handoff is None:
        raise HandoffError("Unknown handoff.")
    return handoff


def _handoff_for_intent(store, intent_id):
    _key, _index, intent = successor_intents._find_intent(store, intent_id)
    if intent is None:
        raise HandoffError("Unknown successor intent.")
    handoff_id = intent.get("handoff_id")
    return _find_handoff(store, handoff_id) if handoff_id else None


def _validate_revalidated_choice(intent, revalidated_scope, revalidated_choice):
    if revalidated_scope != intent["scope"]:
        raise HandoffError("The registered seat or canonical worktree changed; no handoff was frozen.")
    choice = intent["choice"]
    if choice["kind"] == "preserve_observed_predecessor":
        if revalidated_choice is not None:
            raise HandoffError("Model Preferences returned an unexpected choice for preserve-observed.")
        return
    if not isinstance(revalidated_choice, dict):
        raise HandoffError("Model Preferences could not revalidate the configured candidate.")
    if revalidated_choice != choice["snapshot"]:
        raise HandoffError("The configured candidate changed; the original intent remains unchanged.")


def _new_handoff(intent, handoff_id, frozen_at):
    snapshot = copy.deepcopy(intent)
    snapshot.pop("handoff_id", None)
    handoff_generation = uuid.uuid4().hex
    return {
        "schema_version": 1,
        "protocol": HANDOFF_PROTOCOL,
        "id": handoff_id,
        "handoff_generation": handoff_generation,
        "intent_id": intent["id"],
        "state": "frozen",
        "frozen_at": frozen_at,
        "intent_digest": successor_intents.digest(snapshot),
        "intent_snapshot": snapshot,
        "scope": copy.deepcopy(intent["scope"]),
        "choice": copy.deepcopy(intent["choice"]),
        "native_predecessor_snapshot": None,
        "native_generation": copy.deepcopy(UNKNOWN),
        "native_predecessor_binding": copy.deepcopy(UNKNOWN_BINDING),
        "native_successor_binding": copy.deepcopy(UNKNOWN_BINDING),
        "observations": {name: copy.deepcopy(UNKNOWN) for name in OBSERVATIONS},
        "permission_requirement": {"kind": "match_predecessor_effective", "status": "unknown"},
        "readiness": {"status": "blocked", "reason": "native_observations_unavailable"},
        "access_observations": access_observer.empty_access_observations(),
        "launch": {"status": "not_started"},
        "transfer": {"status": "not_applicable"},
        "predecessor_cleanup": {"status": "not_applicable"},
    }


def existing_handoff_for_intent(state_dir, intent_id):
    store = successor_intents.read_store(state_dir)
    handoff = _handoff_for_intent(store, intent_id)
    return copy.deepcopy(handoff) if handoff is not None else None


def freeze_handoff(state_dir, intent_id, revalidated_scope, revalidated_choice, *, clock=None):
    now = time.time if clock is None else clock
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        key, index, intent = successor_intents._find_intent(store, intent_id)
        if intent is None:
            raise HandoffError("Unknown successor intent.")
        existing = _handoff_for_intent(store, intent_id)
        if existing is not None:
            return {"handoff": copy.deepcopy(existing), "reused": True}
        if intent["state"] != "configured":
            raise HandoffError("Only a configured successor intent can be frozen.")
        _validate_revalidated_choice(intent, revalidated_scope, revalidated_choice)
        if len(store["handoffs"]) >= successor_intents.MAX_TOTAL_HANDOFFS:
            raise HandoffError("Successor handoff state reached its record limit.")
        handoff_id = uuid.uuid4().hex
        handoff = _new_handoff(intent, handoff_id, now())
        store["scopes"][key][index] = {**intent, "handoff_id": handoff_id}
        store["handoffs"][handoff_id] = handoff
        successor_intents.write_store(root, store)
        return {"handoff": copy.deepcopy(handoff), "reused": False}


def _snapshot_identity(snapshot):
    identity = copy.deepcopy(snapshot)
    identity.pop("observed_at", None)
    return successor_intents.digest(identity)


def record_predecessor_observation(state_dir, handoff_id, snapshot):
    if not isinstance(snapshot, dict):
        raise HandoffError("The native predecessor observation is malformed.")
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        if handoff["state"] != "frozen" or handoff.get("launch") != {"status": "not_started"}:
            raise HandoffError("A native predecessor observation requires a frozen handoff before launch.")
        existing = handoff.get("native_predecessor_snapshot")
        if existing is not None:
            if _snapshot_identity(existing) == _snapshot_identity(snapshot):
                return {"handoff": copy.deepcopy(handoff), "reused": True}
            raise HandoffError("A different predecessor observation is already recorded.")
        updated = copy.deepcopy(handoff)
        snapshot = copy.deepcopy(snapshot)
        snapshot_digest = successor_intents.digest(snapshot)
        process = snapshot.get("process_identity")
        turn = snapshot.get("turn")
        settings = snapshot.get("settings")
        if not isinstance(process, dict) or not isinstance(turn, dict) or not isinstance(settings, dict):
            raise HandoffError("The native predecessor observation is malformed.")
        updated["native_predecessor_snapshot"] = snapshot
        updated["native_generation"] = {
            "status": "observed",
            "value": copy.deepcopy(process),
            "evidence": {
                "source": "ps",
                "snapshot_digest": snapshot_digest,
                "observed_at": snapshot.get("observed_at"),
            },
        }
        updated["native_predecessor_binding"] = {
            "status": "observed",
            "hook_generation": snapshot.get("hook_generation"),
            "evidence": {
                "source": "cmux_codex_hook_store_v1",
                "snapshot_digest": snapshot_digest,
            },
        }
        setting_evidence = {
            "source": "open_codex_rollout",
            "snapshot_digest": snapshot_digest,
            "turn_id": turn.get("id"),
            "recorded_at": turn.get("recorded_at"),
        }
        updated["observations"]["predecessor_model"] = {
            "status": "observed", "value": settings.get("model"),
            "evidence": copy.deepcopy(setting_evidence),
        }
        updated["observations"]["predecessor_effort"] = {
            "status": "observed", "value": settings.get("effort"),
            "evidence": copy.deepcopy(setting_evidence),
        }
        updated["readiness"] = {"status": "blocked", "reason": "effective_access_unobserved"}
        store["handoffs"][handoff_id] = updated
        successor_intents._validate_store(store)
        successor_intents.write_store(root, store)
        return {"handoff": copy.deepcopy(updated), "reused": False}


def show_handoff(state_dir, handoff_id):
    store = successor_intents.read_store(state_dir)
    handoff = copy.deepcopy(_find_handoff(store, handoff_id))
    if "access_observations" not in handoff:
        handoff["access_observations"] = access_observer.empty_access_observations()
    handoff["phase_readiness"] = phase_readiness(handoff)
    return {"schema_version": 1, "protocol": HANDOFF_PROTOCOL, "handoff": handoff}


def existing_access_observation(state_dir, handoff_id, snapshot, request):
    store = successor_intents.read_store(state_dir)
    handoff = copy.deepcopy(_find_handoff(store, handoff_id))
    access = handoff.get("access_observations", access_observer.empty_access_observations())
    if access.get("pending") is not None:
        pending = next(item for item in access["predecessor"] if item["id"] == access["pending"])
        if pending["request_digest"] != access_observer.request_digest(request):
            raise HandoffError("An access probe has incomplete cleanup; repeat its exact request to reconcile it.")
        return None
    operation_key = access_observer.operation_key(request, snapshot)
    existing = next((item for item in access.get("predecessor", [])
                     if item.get("operation_key") == operation_key), None)
    if existing is None:
        return None
    if "access_observations" not in handoff:
        handoff["access_observations"] = access_observer.empty_access_observations()
    handoff["phase_readiness"] = phase_readiness(handoff)
    return {"handoff": handoff, "observation": copy.deepcopy(existing), "reused": True}


def _update_access_summary(observation):
    checks, cleanup = access_observer.summarize_write_targets(observation["write_plan"])
    if observation["request"]["probe_write"] and observation["write_plan"]:
        observation["checks"]["write_access"] = checks
        observation["cleanup"] = {"status": cleanup, "targets": [
            {"index": target["index"], "scopes": target["scopes"],
             "status": target["cleanup_status"], "reason": target["reason"]}
            for target in observation["write_plan"]
        ]}
    else:
        observation["cleanup"] = {"status": "not_requested", "targets": []}
    incomplete = any(target["cleanup_status"] != "verified" for target in observation["write_plan"])
    observation["state"] = "cleanup_incomplete" if incomplete else "complete"
    return incomplete


def record_access_observation(state_dir, handoff_id, snapshot, request, inputs, git_check, origin_check,
                              worktree_identity, *, observed_at, identity_resolver, retry=False):
    if not isinstance(snapshot, dict):
        raise HandoffError("The exact native access observation is malformed.")
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        if handoff["state"] != "frozen" or handoff.get("launch") != {"status": "not_started"}:
            raise HandoffError("Scoped access observation requires a frozen handoff before launch.")
        if worktree_identity != handoff["scope"]["worktree"]:
            raise HandoffError("Scoped access observation requires the exact frozen canonical worktree identity.")
        baseline = handoff.get("native_predecessor_snapshot")
        if not isinstance(baseline, dict):
            raise HandoffError("Record the exact predecessor settings snapshot before observing access.")
        access = handoff.setdefault("access_observations", access_observer.empty_access_observations())
        access_observer.validate_access_history(access, handoff)
        comparisons = access_observer.compare_native_snapshots(handoff["scope"], baseline, snapshot)
        pending_id = access.get("pending")
        if pending_id is not None:
            observation = next(item for item in access["predecessor"] if item["id"] == pending_id)
            if observation["request_digest"] != access_observer.request_digest(request):
                raise HandoffError("An access probe has incomplete cleanup; repeat its exact request to reconcile it.")
            if (comparisons["owner_binding"]["status"] != "ready"
                    or comparisons["native_settings_match"]["status"] != "ready"):
                raise HandoffError("The pending probe's exact native owner or settings no longer match; cleanup is retained.")
            identity = access_observer.operation_identity(snapshot)
            previous = observation["revalidations"][-1]["operation_identity"] if observation["revalidations"] else observation["native_settings_ref"]["operation_identity"]
            if identity != previous:
                if len(observation["revalidations"]) >= 8:
                    raise HandoffError("The pending access observation reached its revalidation limit.")
                observation["revalidations"].append({
                    "operation_identity": identity,
                    "snapshot_digest": successor_intents.digest(snapshot),
                    "native_snapshot": copy.deepcopy(snapshot),
                    "turn_id": snapshot["turn"]["id"],
                    "turn_recorded_at": snapshot["turn"]["recorded_at"],
                    "observed_at": snapshot["observed_at"],
                })
                successor_intents.write_store(root, store)
            retry_result = True
        else:
            observation = access_observer.make_observation(
                handoff, snapshot, request, inputs, git_check, origin_check,
                observed_at=observed_at, retry=retry,
            )
            existing = next((item for item in access["predecessor"]
                             if item["operation_key"] == observation["operation_key"]), None)
            if existing is not None:
                public_handoff = copy.deepcopy(handoff)
                public_handoff["phase_readiness"] = phase_readiness(public_handoff)
                return {"handoff": public_handoff, "observation": copy.deepcopy(existing), "reused": True}
            if len(access["predecessor"]) >= access_observer.MAX_ACCESS_HISTORY:
                raise HandoffError("Scoped access observation history reached its record limit.")
            if request["probe_write"] and (comparisons["owner_binding"]["status"] == "ready"
                                            and comparisons["native_settings_match"]["status"] == "ready"):
                observation["write_plan"] = access_observer.make_write_plan(worktree_identity, observation["id"])
                active_targets = any(target["stage"] != "failed" for target in observation["write_plan"])
                if active_targets:
                    observation["cleanup"] = {"status": "incomplete", "targets": []}
                    observation["state"] = "cleanup_incomplete"
                    access["pending"] = observation["id"]
            access["predecessor"].append(observation)
            if access.get("pending") is None:
                observation["cleanup"] = {"status": "verified", "targets": []} if observation["write_plan"] else observation["cleanup"]
                _update_access_summary(observation)
            successor_intents.write_store(root, store)
            retry_result = False

        if access.get("pending") == observation["id"]:
            targets = observation["write_plan"]
            for index, target in enumerate(list(targets)):
                if target["stage"] in ("complete", "failed"):
                    continue

                def persist_target(updated, target_index=index):
                    observation["write_plan"][target_index] = copy.deepcopy(updated)
                    _update_access_summary(observation)
                    if observation["state"] == "complete":
                        access["pending"] = None
                    else:
                        access["pending"] = observation["id"]
                    successor_intents.write_store(root, store)

                updated = access_observer.probe_target(
                    target, handoff["scope"], identity_resolver, persist_target,
                )
                persist_target(updated)
            incomplete = _update_access_summary(observation)
            if incomplete:
                access["pending"] = observation["id"]
            else:
                access["pending"] = None
            successor_intents._validate_store(store)
            successor_intents.write_store(root, store)
        public_handoff = copy.deepcopy(handoff)
        public_handoff["phase_readiness"] = phase_readiness(public_handoff)
        return {"handoff": public_handoff, "observation": copy.deepcopy(observation),
                "reused": retry_result}


def _check_status(observation, name):
    if observation is None:
        return "unknown"
    check = observation.get("checks", {}).get(name)
    return check.get("status", "unknown") if isinstance(check, dict) else "unknown"


def _phase_status(checks, *, unknown_means_blocked=False):
    states = [check["status"] if isinstance(check, dict) else check for check in checks.values()]
    if "blocked" in states:
        return "blocked"
    if unknown_means_blocked and any(state in ("unknown", "not_assessed") for state in states):
        return "blocked"
    if any(state in ("unknown", "not_assessed") for state in states):
        return "unknown"
    return "ready"


def phase_readiness(handoff):
    access = handoff.get("access_observations", access_observer.empty_access_observations())
    history = access.get("predecessor", []) if isinstance(access, dict) else []
    latest = history[-1] if history else None
    launch_checks = {
        "frozen_intent": "ready" if handoff.get("intent_digest") else "blocked",
        "predecessor_owner_binding": _check_status(latest, "owner_binding"),
        "predecessor_settings": _check_status(latest, "native_settings_match"),
        "context_access": _check_status(latest, "context_access"),
        "guidance_access": _check_status(latest, "guidance_access"),
        "git_metadata_access": _check_status(latest, "git_metadata_access"),
        "write_access": _check_status(latest, "write_access"),
        "network_access": _check_status(latest, "git_origin_access"),
        "native_trust": _check_status(latest, "native_trust"),
        "effective_permissions": _check_status(latest, "effective_permissions"),
    }
    transfer_checks = {
        "launch_complete": "ready" if handoff.get("launch", {}).get("status") == "confirmed" else "unknown",
        "successor_binding": "ready" if handoff.get("native_successor_binding", {}).get("status") == "observed" else "unknown",
        "successor_settings": "ready" if all(
            handoff.get("observations", {}).get(name, {}).get("status") == "observed"
            for name in ("successor_model", "successor_effort")
        ) else "unknown",
        "successor_access": "ready" if access.get("successor") else "unknown",
        "effective_permissions": handoff.get("observations", {}).get("effective_permissions", {}).get("status", "unknown"),
        "native_trust": _check_status(latest, "native_trust"),
        "receiving": "unknown",
        "custody_acceptance": handoff.get("custody_acceptance", {}).get("status", "unknown"),
    }
    return {
        "launch": {"status": _phase_status(launch_checks), "checks": launch_checks},
        "transfer": {"status": _phase_status(transfer_checks, unknown_means_blocked=True),
                     "checks": transfer_checks},
    }


def reconcile_handoff(state_dir, handoff_id):
    result = show_handoff(state_dir, handoff_id)
    handoff = result["handoff"]
    phases = handoff["phase_readiness"]
    blockers = []
    if handoff["native_generation"]["status"] != "observed":
        blockers.append("native_generation_unavailable")
    if any(handoff["observations"][name]["status"] != "observed"
           for name in ("predecessor_model", "predecessor_effort")):
        blockers.append("predecessor_model_effort_unobserved")
    if (handoff["native_successor_binding"]["status"] != "observed"
            or any(handoff["observations"][name]["status"] != "observed"
                   for name in ("successor_model", "successor_effort"))):
        blockers.append("successor_model_effort_unobserved")
    if handoff["observations"]["effective_permissions"]["status"] != "observed":
        blockers.append("effective_permissions_unobserved")
    if phases["launch"]["status"] != "ready":
        blockers.append("launch_readiness_" + phases["launch"]["status"])
    if phases["transfer"]["status"] != "ready":
        blockers.append("transfer_readiness_" + phases["transfer"]["status"])
    return {
        **result,
        "reconciliation": {
            "status": "blocked",
            "state_consistent": handoff["state"] == "frozen",
            "native_queries_performed": False,
            "writes_performed": False,
            "phase_readiness": copy.deepcopy(phases),
            "blockers": blockers,
        },
    }


def _begin_opening(handoff):
    current = copy.deepcopy(handoff)
    state = current.get("state")
    launch = current.get("launch", {})
    if state == "frozen" and launch.get("status") == "not_started":
        if current.get("readiness", {}).get("status") != "ready":
            raise HandoffError("Native readiness must be established before an opening attempt.")
        current["state"] = "opening"
        current["launch"] = {"status": "opening"}
        return current, True
    if state in ("opening", "opening_unconfirmed"):
        return current, False
    raise HandoffError("This handoff cannot begin another opening attempt.")


def _mark_opening_uncertain(handoff):
    current = copy.deepcopy(handoff)
    if current.get("state") == "opening_unconfirmed":
        return current
    if current.get("state") != "opening":
        raise HandoffError("Only an opening attempt can become unconfirmed.")
    current["state"] = "opening_unconfirmed"
    current["launch"] = {"status": "opening_unconfirmed"}
    return current


def _consume_confirmed_transfer(intent, handoff, receipt):
    current_intent = copy.deepcopy(intent)
    current_handoff = copy.deepcopy(handoff)
    if (not isinstance(receipt, _ConfirmedTransferReceipt)
            or receipt.handoff_id != handoff.get("id")
            or receipt.intent_id != intent.get("id")
            or receipt.intent_digest != handoff.get("intent_digest")
            or not receipt.receipt_id):
        raise HandoffError("The transfer receipt does not match this frozen intent and handoff.")
    if current_handoff.get("state") == "transferred" and current_intent.get("lifecycle_state") == "consumed":
        if (current_handoff.get("transfer_receipt_id") == receipt.receipt_id
                and current_intent.get("transfer_receipt_id") == receipt.receipt_id):
            return current_intent, current_handoff, True
        raise HandoffError("A different transfer receipt already consumed this intent.")
    if current_handoff.get("state") != "transfer_prepared":
        raise HandoffError("A transfer receipt cannot consume an unprepared handoff.")
    current_handoff["state"] = "transferred"
    current_handoff["transfer_receipt_id"] = receipt.receipt_id
    current_intent["lifecycle_state"] = "consumed"
    current_intent["transfer_receipt_id"] = receipt.receipt_id
    return current_intent, current_handoff, False
