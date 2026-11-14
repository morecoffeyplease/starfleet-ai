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
        "native_successor_generation": copy.deepcopy(UNKNOWN),
        "native_successor_binding": copy.deepcopy(UNKNOWN_BINDING),
        "native_successor_snapshot": None,
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


def _choice_model_effort(handoff):
    choice = handoff.get("choice")
    if isinstance(choice, dict) and choice.get("kind") == "model_preferences_candidate":
        snapshot = choice.get("snapshot")
        if isinstance(snapshot, dict):
            return snapshot.get("model"), snapshot.get("effort")
    predecessor = handoff.get("native_predecessor_snapshot")
    settings = predecessor.get("settings") if isinstance(predecessor, dict) else None
    if isinstance(settings, dict):
        return settings.get("model"), settings.get("effort")
    return None, None


def _recorded_predecessor_access(handoff):
    access = handoff.get("access_observations")
    if not isinstance(access, dict) or access.get("pending") is not None:
        return None
    history = access.get("predecessor")
    if not isinstance(history, list) or not history:
        return None
    return history[-1]


def _public_handoff(handoff):
    result = copy.deepcopy(handoff)
    result["phase_readiness"] = phase_readiness(result)
    return result


def candidate_opening_checks(handoff, current_snapshot, fresh_inputs, git_identity, packet_check):
    """Assess only the evidence needed to open the candidate, not transfer parity."""
    baseline = handoff.get("native_predecessor_snapshot")
    access = _recorded_predecessor_access(handoff)
    if isinstance(baseline, dict) and isinstance(current_snapshot, dict):
        comparisons = access_observer.compare_native_snapshots(handoff["scope"], baseline, current_snapshot)
        owner = comparisons["owner_binding"]["status"]
        settings = comparisons["native_settings_match"]["status"]
    else:
        owner = settings = "unknown"
    recorded = access.get("checks", {}) if isinstance(access, dict) else {}
    recorded_request = access.get("request") if isinstance(access, dict) else None
    input_checks = fresh_inputs if isinstance(fresh_inputs, dict) else {}
    has_context = bool(recorded_request and recorded_request.get("context_files"))
    has_guidance = bool(recorded_request and recorded_request.get("guidance_files"))
    expected_worktree = handoff.get("scope", {}).get("worktree")
    checks = {
        "frozen_intent": "ready" if handoff.get("state") == "frozen" and handoff.get("intent_digest") else "blocked",
        "predecessor_owner_binding": owner,
        "predecessor_settings": settings,
        "context_access": (input_checks.get("context_access", {}).get("status", "unknown")
                           if has_context else "unknown"),
        "guidance_access": (input_checks.get("guidance_access", {}).get("status", "unknown")
                            if has_guidance else "unknown"),
        "context_unchanged": ("ready" if has_context
                               and input_checks.get("context_access") == recorded.get("context_access") else "blocked"),
        "guidance_unchanged": ("ready" if has_guidance
                                and input_checks.get("guidance_access") == recorded.get("guidance_access") else "blocked"),
        "git_metadata_access": ("ready" if git_identity == expected_worktree else "blocked"),
        "packet_access": packet_check.get("status", "unknown") if isinstance(packet_check, dict) else "unknown",
    }
    return {"status": _phase_status(checks), "checks": checks}


def begin_candidate_opening(state_dir, handoff_id, attempt):
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        launch = handoff.get("launch")
        if isinstance(launch, dict) and launch.get("status") != "not_started":
            previous = launch.get("attempt")
            if isinstance(previous, dict) and previous.get("request_digest") == attempt.get("request_digest"):
                return {"handoff": _public_handoff(handoff), "reused": True}
            raise HandoffError("A candidate opening was already attempted; inspect its exact outcome before any other action.")
        if handoff.get("state") != "frozen" or launch != {"status": "not_started"}:
            raise HandoffError("Only an unopened frozen handoff can begin candidate opening.")
        expected_snapshot = attempt.get("preflight", {}).get("predecessor_snapshot_digest")
        if expected_snapshot != successor_intents.digest(handoff.get("native_predecessor_snapshot")):
            raise HandoffError("The predecessor snapshot changed during opening preflight; no candidate was opened.")
        expected_access = attempt.get("preflight", {}).get("access_observation_id")
        latest_access = _recorded_predecessor_access(handoff)
        if latest_access is None or latest_access.get("id") != expected_access:
            raise HandoffError("The scoped access evidence changed during opening preflight; no candidate was opened.")
        updated = copy.deepcopy(handoff)
        updated["launch"] = {"status": "opening_unconfirmed", "attempt": copy.deepcopy(attempt),
                              "returned_surface": None}
        store["handoffs"][handoff_id] = updated
        successor_intents._validate_store(store)
        successor_intents.write_store(root, store)
        return {"handoff": _public_handoff(updated), "reused": False}


def record_candidate_opened(state_dir, handoff_id, request_digest, surface_id, workspace_id):
    if not isinstance(surface_id, str) or not isinstance(workspace_id, str):
        raise HandoffError("CMUX returned an incomplete surface identity; the opening remains unconfirmed.")
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        launch = handoff.get("launch", {})
        attempt = launch.get("attempt") if isinstance(launch, dict) else None
        if (launch.get("status") != "opening_unconfirmed" or not isinstance(attempt, dict)
                or attempt.get("request_digest") != request_digest):
            raise HandoffError("The recorded opening attempt changed before its return was recorded.")
        if surface_id.upper() == handoff["scope"]["seat"]["panel_id"].upper():
            raise HandoffError("CMUX returned the predecessor surface; the opening remains unconfirmed.")
        updated = copy.deepcopy(handoff)
        updated["launch"] = {
            "status": "opened_unobserved",
            "attempt": copy.deepcopy(attempt),
            "returned_surface": {"surface_id": surface_id, "workspace_id": workspace_id},
        }
        store["handoffs"][handoff_id] = updated
        successor_intents._validate_store(store)
        successor_intents.write_store(root, store)
        return {"handoff": _public_handoff(updated), "reused": False}


def record_successor_observation(state_dir, handoff_id, snapshot):
    if not isinstance(snapshot, dict):
        raise HandoffError("The native successor observation is malformed.")
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        launch = handoff.get("launch", {})
        if launch.get("status") == "successor_observed":
            existing = handoff.get("native_successor_snapshot")
            if isinstance(existing, dict) and _snapshot_identity(existing) == _snapshot_identity(snapshot):
                return {"handoff": _public_handoff(handoff), "reused": True}
            raise HandoffError("A different successor observation is already recorded.")
        if launch.get("status") != "opened_unobserved":
            raise HandoffError("Successor observation requires one recorded CMUX surface return.")
        returned = launch.get("returned_surface")
        subject = snapshot.get("subject")
        if (not isinstance(returned, dict) or not isinstance(subject, dict)
                or subject.get("panel_id", "").upper() != returned.get("surface_id", "").upper()
                or subject.get("workspace_id", "").upper() != returned.get("workspace_id", "").upper()
                or subject.get("worktree") != handoff["scope"]["worktree"]
                or subject.get("harness") != "codex"
                or subject.get("session_id") == handoff["scope"]["seat"]["session_id"]
                or returned.get("surface_id", "").upper() == handoff["scope"]["seat"]["panel_id"].upper()):
            raise HandoffError("The observed native session is not the exact newly returned successor surface.")
        predecessor = handoff.get("native_predecessor_snapshot")
        process = snapshot.get("process_identity")
        predecessor_process = predecessor.get("process_identity") if isinstance(predecessor, dict) else None
        predecessor_generation = handoff.get("native_predecessor_binding", {}).get("hook_generation")
        if (not isinstance(process, dict) or process == predecessor_process
                or snapshot.get("hook_generation") == predecessor_generation):
            raise HandoffError("The successor process and hook generation must be distinct from the predecessor.")
        updated = copy.deepcopy(handoff)
        snapshot = copy.deepcopy(snapshot)
        snapshot_digest = successor_intents.digest(snapshot)
        process = snapshot["process_identity"]
        turn = snapshot["turn"]
        settings = snapshot["settings"]
        evidence = {
            "source": "open_codex_rollout",
            "snapshot_digest": snapshot_digest,
            "turn_id": turn["id"],
            "recorded_at": turn["recorded_at"],
        }
        updated["native_successor_snapshot"] = snapshot
        updated["native_successor_generation"] = {
            "status": "observed", "value": copy.deepcopy(process),
            "evidence": {"source": "ps", "snapshot_digest": snapshot_digest,
                         "observed_at": snapshot["observed_at"]},
        }
        updated["native_successor_binding"] = {
            "status": "observed", "hook_generation": snapshot["hook_generation"],
            "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest},
        }
        updated["observations"]["successor_model"] = {
            "status": "observed", "value": settings["model"], "evidence": copy.deepcopy(evidence),
        }
        updated["observations"]["successor_effort"] = {
            "status": "observed", "value": settings["effort"], "evidence": copy.deepcopy(evidence),
        }
        updated["launch"] = {
            "status": "successor_observed", "attempt": copy.deepcopy(launch["attempt"]),
            "returned_surface": copy.deepcopy(returned),
        }
        store["handoffs"][handoff_id] = updated
        successor_intents._validate_store(store)
        successor_intents.write_store(root, store)
        return {"handoff": _public_handoff(updated), "reused": False}


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


def existing_access_observation(state_dir, handoff_id, snapshot, request, *, actor="predecessor"):
    store = successor_intents.read_store(state_dir)
    handoff = copy.deepcopy(_find_handoff(store, handoff_id))
    access = handoff.get("access_observations", access_observer.empty_access_observations())
    pending_entry = access_observer.pending_access_entry(access)
    if pending_entry is not None:
        pending_actor, pending = pending_entry
        if pending_actor != actor:
            raise HandoffError("Only the exact actor that created pending access cleanup can reconcile it.")
        if pending["request_digest"] != access_observer.request_digest(request):
            raise HandoffError("An access probe has incomplete cleanup; repeat its exact request to reconcile it.")
        return None
    observations = access.get(actor)
    if not isinstance(observations, list):
        raise HandoffError("The scoped access actor is unsupported.")
    operation_key = access_observer.operation_key(request, snapshot)
    existing = next((item for item in observations
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
                              worktree_identity, *, observed_at, identity_resolver, actor="predecessor", retry=False):
    if not isinstance(snapshot, dict):
        raise HandoffError("The exact native access observation is malformed.")
    with successor_intents.mutation_lock(state_dir) as root:
        store = successor_intents.read_store(root)
        handoff = _find_handoff(store, handoff_id)
        if handoff["state"] != "frozen":
            raise HandoffError("Scoped access observation requires the frozen handoff state.")
        launch_status = handoff.get("launch", {}).get("status")
        if actor == "predecessor" and launch_status != "not_started":
            raise HandoffError("Predecessor scoped access observation requires a frozen handoff before launch.")
        if actor == "successor" and launch_status != "successor_observed":
            raise HandoffError("Successor scoped access observation requires the exact observed successor.")
        if worktree_identity != handoff["scope"]["worktree"]:
            raise HandoffError("Scoped access observation requires the exact frozen canonical worktree identity.")
        try:
            actor_scope, baseline = access_observer.access_baseline(handoff, actor)
            if actor == "successor":
                access_observer.validate_successor_request(handoff, request)
        except access_observer.AccessObservationError as exc:
            raise HandoffError(str(exc)) from exc
        access = handoff.setdefault("access_observations", access_observer.empty_access_observations())
        access_observer.validate_access_history(access, handoff)
        comparisons = access_observer.compare_native_snapshots(actor_scope, baseline, snapshot)
        observations = access.get(actor)
        if not isinstance(observations, list):
            raise HandoffError("The scoped access actor is unsupported.")
        if actor == "successor" and (comparisons["owner_binding"]["status"] != "ready"
                                      or comparisons["native_settings_match"]["status"] != "ready"):
            raise HandoffError("Successor access requires the exact recorded successor process, generation, and settings.")
        pending_entry = access_observer.pending_access_entry(access)
        pending_id = access.get("pending")
        if pending_entry is not None:
            pending_actor, observation = pending_entry
            if pending_actor != actor:
                raise HandoffError("Only the exact actor that created pending access cleanup can reconcile it.")
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
                observed_at=observed_at, actor=actor, retry=retry,
            )
            existing = next((item for item in observations
                             if item["operation_key"] == observation["operation_key"]), None)
            if existing is not None:
                public_handoff = copy.deepcopy(handoff)
                public_handoff["phase_readiness"] = phase_readiness(public_handoff)
                return {"handoff": public_handoff, "observation": copy.deepcopy(existing), "reused": True}
            if len(observations) >= access_observer.MAX_ACCESS_HISTORY:
                raise HandoffError("Scoped access observation history reached its record limit.")
            if request["probe_write"] and (comparisons["owner_binding"]["status"] == "ready"
                                            and comparisons["native_settings_match"]["status"] == "ready"):
                observation["write_plan"] = access_observer.make_write_plan(worktree_identity, observation["id"])
                active_targets = any(target["stage"] != "failed" for target in observation["write_plan"])
                if active_targets:
                    observation["cleanup"] = {"status": "incomplete", "targets": []}
                    observation["state"] = "cleanup_incomplete"
                    access["pending"] = observation["id"]
            observations.append(observation)
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


def _successor_access_status(observation):
    if not isinstance(observation, dict):
        return "unknown"
    checks = observation.get("checks", {})
    required = {
        "owner_binding": checks.get("owner_binding"),
        "native_settings_match": checks.get("native_settings_match"),
        "context_access": checks.get("context_access"),
        "guidance_access": checks.get("guidance_access"),
        "git_metadata_access": checks.get("git_metadata_access"),
    }
    request = observation.get("request", {})
    if request.get("probe_write"):
        required["write_access"] = checks.get("write_access")
    if request.get("probe_origin"):
        required["git_origin_access"] = checks.get("git_origin_access")
    return _phase_status(required)


def _successor_content_match(handoff, observation, check_name):
    if not isinstance(observation, dict):
        return "unknown"
    try:
        opening = access_observer.opening_access_observation(handoff)
    except access_observer.AccessObservationError:
        return "unknown"
    original = opening.get("checks", {}).get(check_name, {})
    successor = observation.get("checks", {}).get(check_name, {})
    if original.get("status") == "blocked" or successor.get("status") == "blocked":
        return "blocked"
    if original.get("status") != "ready" or successor.get("status") != "ready":
        return "unknown"
    original_items = original.get("items")
    successor_items = successor.get("items")
    if not isinstance(original_items, list) or not isinstance(successor_items, list):
        return "unknown"
    fields = ("path", "bytes_read", "sha256")
    original_content = {item.get("path"): tuple(item.get(name) for name in fields[1:])
                        for item in original_items if isinstance(item, dict) and item.get("status") == "ready"}
    successor_content = {item.get("path"): tuple(item.get(name) for name in fields[1:])
                         for item in successor_items if isinstance(item, dict) and item.get("status") == "ready"}
    if len(original_content) != len(original_items) or len(successor_content) != len(successor_items):
        return "unknown"
    return "ready" if original_content == successor_content else "blocked"


def phase_readiness(handoff):
    access = handoff.get("access_observations", access_observer.empty_access_observations())
    history = access.get("predecessor", []) if isinstance(access, dict) else []
    latest = history[-1] if history else None
    successor_history = access.get("successor", []) if isinstance(access, dict) else []
    latest_successor = successor_history[-1] if successor_history else None
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
        "successor_choice_match": _successor_choice_status(handoff),
        "successor_policy_match": _successor_policy_status(handoff),
        "successor_access": _successor_access_status(latest_successor),
        "successor_context_matches_opening": _successor_content_match(
            handoff, latest_successor, "context_access",
        ),
        "successor_guidance_matches_opening": _successor_content_match(
            handoff, latest_successor, "guidance_access",
        ),
        "effective_permissions": handoff.get("observations", {}).get("effective_permissions", {}).get("status", "unknown"),
        "native_trust": _check_status(latest, "native_trust"),
        "receiving": "unknown",
        "custody_acceptance": handoff.get("custody_acceptance", {}).get("status", "unknown"),
    }
    launch = handoff.get("launch", {})
    attempt = launch.get("attempt") if isinstance(launch, dict) else None
    if isinstance(attempt, dict) and isinstance(attempt.get("preflight"), dict):
        candidate_checks = copy.deepcopy(attempt["preflight"].get("checks", {}))
        candidate_status = attempt["preflight"].get("status", "unknown")
    else:
        candidate_checks = {
            "frozen_intent": "ready" if handoff.get("intent_digest") else "blocked",
            "predecessor_owner_binding": _check_status(latest, "owner_binding"),
            "predecessor_settings": _check_status(latest, "native_settings_match"),
            "context_access": _check_status(latest, "context_access"),
            "guidance_access": _check_status(latest, "guidance_access"),
            "context_unchanged": "unknown",
            "guidance_unchanged": "unknown",
            "git_metadata_access": _check_status(latest, "git_metadata_access"),
            "packet_access": "unknown",
        }
        candidate_status = _phase_status(candidate_checks)
    return {
        "launch": {"status": _phase_status(launch_checks), "checks": launch_checks},
        "candidate_opening": {"status": candidate_status, "checks": candidate_checks},
        "transfer": {"status": _phase_status(transfer_checks, unknown_means_blocked=True),
                     "checks": transfer_checks},
    }


def _successor_choice_status(handoff):
    observed = handoff.get("observations", {})
    model = observed.get("successor_model", {})
    effort = observed.get("successor_effort", {})
    if model.get("status") != "observed" or effort.get("status") != "observed":
        return "unknown"
    expected_model, expected_effort = _choice_model_effort(handoff)
    if expected_model is None or expected_effort is None:
        return "unknown"
    return "ready" if (model.get("value") == expected_model and effort.get("value") == expected_effort) else "blocked"


def _successor_policy_status(handoff):
    predecessor = handoff.get("native_predecessor_snapshot")
    successor = handoff.get("native_successor_snapshot")
    if not isinstance(predecessor, dict) or not isinstance(successor, dict):
        return "unknown"
    expected = predecessor.get("settings", {})
    observed = successor.get("settings", {})
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return "unknown"
    return "ready" if all(expected.get(name) == observed.get(name)
                           for name in ("approval_policy", "sandbox_policy")) else "blocked"


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
    if phases["transfer"]["checks"]["successor_choice_match"] == "blocked":
        blockers.append("successor_choice_mismatch")
    if phases["transfer"]["checks"]["successor_policy_match"] == "blocked":
        blockers.append("successor_policy_mismatch")
    launch_status = handoff.get("launch", {}).get("status")
    if launch_status == "opening_unconfirmed":
        blockers.append("candidate_opening_unconfirmed")
    elif launch_status == "opened_unobserved":
        blockers.append("successor_native_observation_unavailable")
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
