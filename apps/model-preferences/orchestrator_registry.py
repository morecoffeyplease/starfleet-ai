"""Versioned local registry for orchestrator owners and handoff receipts."""

import copy
import hashlib
import json
import re


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_PROTOCOL = "starfleet.model-preferences.orchestrator-registry/v1"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_PROTOCOL = "starfleet.model-preferences.handoff-receipt/v1"
HANDOFF_ID = re.compile(r"[0-9a-f]{32}\Z")
OWNER_FIELDS = ("workspace", "surface", "session", "harness")
REGISTRY_FIELDS = {"schema_version", "protocol", "recipients", "handoff_receipts"}
RECEIPT_FIELDS = {
    "schema_version", "protocol", "handoff_id", "name", "state", "previous_owner", "next_owner",
    "previous_owner_digest", "next_owner_digest", "request_digest",
}


class RegistryError(ValueError):
    pass


def digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_recipients(value):
    if not isinstance(value, dict):
        raise RegistryError("Orchestrator registry recipients must be an object.")
    recipients = copy.deepcopy(value)
    for name, recipient in recipients.items():
        if (not isinstance(name, str) or not name.strip() or not isinstance(recipient, dict)
                or not all(isinstance(recipient.get(field), str) and recipient[field]
                           for field in OWNER_FIELDS)):
            raise RegistryError("Orchestrator registry contains an invalid recipient.")
    return recipients


def _validate_handoff_id(handoff_id):
    if not isinstance(handoff_id, str) or HANDOFF_ID.fullmatch(handoff_id) is None:
        raise RegistryError("Invalid handoff receipt ID.")


def _receipt_request(receipt):
    return {
        "protocol": RECEIPT_PROTOCOL,
        "handoff_id": receipt["handoff_id"],
        "name": receipt["name"],
        "previous_owner_digest": receipt["previous_owner_digest"],
        "next_owner_digest": receipt["next_owner_digest"],
    }


def _validate_receipts(value, recipients):
    if not isinstance(value, dict):
        raise RegistryError("Orchestrator handoff receipts must be an object.")
    receipts = copy.deepcopy(value)
    for handoff_id, receipt in receipts.items():
        if (not isinstance(handoff_id, str) or HANDOFF_ID.fullmatch(handoff_id) is None
                or not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
                or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
                or receipt.get("protocol") != RECEIPT_PROTOCOL
                or receipt.get("handoff_id") != handoff_id
                or not isinstance(receipt.get("name"), str) or not receipt["name"].strip()
                or receipt.get("state") not in ("pending", "consumed")):
            raise RegistryError("Orchestrator handoff receipt is malformed.")
        previous = validate_recipients({receipt["name"]: receipt.get("previous_owner")})[receipt["name"]]
        successor = validate_recipients({receipt["name"]: receipt.get("next_owner")})[receipt["name"]]
        previous_digest = digest(previous)
        next_digest = digest(successor)
        if (receipt.get("previous_owner_digest") != previous_digest
                or receipt.get("next_owner_digest") != next_digest
                or receipt.get("request_digest") != digest(_receipt_request(receipt))
                or previous == successor):
            raise RegistryError("Orchestrator handoff receipt digest is malformed.")
        if receipt["state"] == "pending" and recipients.get(receipt["name"]) != successor:
            raise RegistryError("Pending orchestrator handoff receipt does not match the registered owner.")
    return receipts


def empty_registry():
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "protocol": REGISTRY_PROTOCOL,
        "recipients": {},
        "handoff_receipts": {},
    }


def decode_document(document):
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise RegistryError("Orchestrator registry must be a JSON object.")
    protocol = document.get("protocol")
    versioned_marker = (isinstance(protocol, str)
                        and protocol.startswith("starfleet.model-preferences.orchestrator-registry/"))
    malformed_marker = ("schema_version" in document
                        and ("recipients" in document or "handoff_receipts" in document))
    if versioned_marker or malformed_marker:
        if (set(document) != REGISTRY_FIELDS
                or document.get("schema_version") != REGISTRY_SCHEMA_VERSION
                or protocol != REGISTRY_PROTOCOL):
            raise RegistryError("Orchestrator registry has an unsupported version or protocol.")
        recipients = validate_recipients(document.get("recipients"))
        receipts = _validate_receipts(document.get("handoff_receipts"), recipients)
        return {"recipients": recipients, "handoff_receipts": receipts}
    return {"recipients": validate_recipients(document), "handoff_receipts": {}}


def read_recipients(document):
    return copy.deepcopy(decode_document(document)["recipients"])


def encode_registry(recipients, handoff_receipts=None):
    recipient_map = validate_recipients(recipients)
    receipt_map = _validate_receipts({} if handoff_receipts is None else handoff_receipts, recipient_map)
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "protocol": REGISTRY_PROTOCOL,
        "recipients": recipient_map,
        "handoff_receipts": receipt_map,
    }


def replace_recipients(document, recipients):
    registry = decode_document(document)
    recipient_map = validate_recipients(recipients)
    for receipt in registry["handoff_receipts"].values():
        if (receipt["state"] == "pending"
                and recipient_map.get(receipt["name"]) != receipt["next_owner"]):
            raise RegistryError("An unconsumed handoff receipt blocks changing this orchestrator owner.")
    return encode_registry(recipient_map, registry["handoff_receipts"])


def _new_receipt(handoff_id, name, previous_owner, next_owner):
    _validate_handoff_id(handoff_id)
    previous = validate_recipients({name: previous_owner})[name]
    successor = validate_recipients({name: next_owner})[name]
    if previous == successor:
        raise RegistryError("A handoff receipt requires a changed owner.")
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "protocol": RECEIPT_PROTOCOL,
        "handoff_id": handoff_id,
        "name": name,
        "state": "pending",
        "previous_owner": previous,
        "next_owner": successor,
        "previous_owner_digest": digest(previous),
        "next_owner_digest": digest(successor),
    }
    receipt["request_digest"] = digest(_receipt_request(receipt))
    return receipt


def _commit_owner_receipt(document, handoff_id, name, expected_owner, next_owner):
    registry = decode_document(document)
    receipt = _new_receipt(handoff_id, name, expected_owner, next_owner)
    existing = registry["handoff_receipts"].get(handoff_id)
    if existing is not None:
        if existing["request_digest"] != receipt["request_digest"]:
            raise RegistryError("This handoff ID already records a different owner change.")
        return encode_registry(registry["recipients"], registry["handoff_receipts"]), existing, True
    if any(item["state"] == "pending" and item["name"] == name
           for item in registry["handoff_receipts"].values()):
        raise RegistryError("An unconsumed handoff receipt already exists for this orchestrator.")
    if registry["recipients"].get(name) != receipt["previous_owner"]:
        raise RegistryError("Registered owner changed; no handoff receipt was recorded.")
    recipients = copy.deepcopy(registry["recipients"])
    recipients[name] = receipt["next_owner"]
    receipts = copy.deepcopy(registry["handoff_receipts"])
    receipts[handoff_id] = receipt
    return encode_registry(recipients, receipts), receipt, False


def _acknowledge_receipt(document, handoff_id, request_digest):
    _validate_handoff_id(handoff_id)
    registry = decode_document(document)
    receipt = registry["handoff_receipts"].get(handoff_id)
    if receipt is None:
        raise RegistryError("Handoff receipt is missing; inspect the owner state and do not reapply the change.")
    if receipt["request_digest"] != request_digest:
        raise RegistryError("Handoff receipt does not match the expected owner change.")
    if receipt["state"] == "consumed":
        return encode_registry(registry["recipients"], registry["handoff_receipts"]), receipt, True
    receipts = copy.deepcopy(registry["handoff_receipts"])
    consumed = {**receipt, "state": "consumed"}
    receipts[handoff_id] = consumed
    return encode_registry(registry["recipients"], receipts), consumed, False


def lookup_receipt(document, handoff_id):
    _validate_handoff_id(handoff_id)
    return copy.deepcopy(decode_document(document)["handoff_receipts"].get(handoff_id))
