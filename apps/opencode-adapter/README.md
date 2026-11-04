# OpenCode session adapter

This component is a read-only Python standard-library client for inspecting one OpenCode v2 session.
It is a narrow foundation for later Model Preferences, Pattern Buffer, and Hailing Frequencies integration.

## Supported contract

The first contract accepts only an API server that reports version 2.2.5.
Its adapter contract identifier is opencode-v2.2.5-session-inspection-1.
It reads the official v2 routes for server info, an exact session, active session state, pending permissions, and pending forms.
The OpenCode [v2.2.5 tagged protocol source](https://github.com/anomalyco/opencode/tree/v2.2.5/packages/protocol/src/groups) defines the inspected routes and response fields.
The OpenCode [v2.2.5 session schema](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/schema/src/session.ts) and [location schema](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/schema/src/location.ts) define the session identity projection.
The OpenCode [v2 server guide](https://opencode.ai/v2/docs/server) describes the HTTP server and its API.
The OpenCode [v2 web guide](https://opencode.ai/v2/docs/cli/web) documents Basic authentication and the fixed v2 username opencode.

The inspected routes are GET /api/info, GET /api/session/{sessionID}, GET /api/session/active, GET /api/session/{sessionID}/permission, and GET /api/session/{sessionID}/form.
The client sends Basic authentication on every authenticated request and rejects endpoints that do not first challenge unauthenticated access.
It accepts only an explicit HTTP URL with a numeric loopback IP address and port.
It rejects hostnames, non-loopback addresses, HTTPS URLs, redirects, and URL-embedded credentials.
It ignores environment proxy settings so authenticated requests go directly to the loopback address.
The per-operation socket timeout is capped at five seconds and successful response bodies are limited to 128 KiB.
The server version, usable process PID, session ID, session location, active state, permission list, and form list are validated before a clear inspection is returned.
Any mismatch, missing required value, unsupported version, unknown state, HTTP error, or malformed schema produces a hold.

This adapter does not send messages, change session state, answer questions, approve permissions, start services, or write configuration.
It has no installer, persistent configuration, or user-facing session command.
The printable instructions are in [guide.md](guide.md).

## Identity fields

Callers must provide the actual OpenCode session ID, expected API server PID, actual session directory, worktree directory, CMUX workspace UUID, CMUX surface UUID, and parent task ID as separate fields.
The OpenCode response must return the same ses_ session ID, and its location directory must resolve to both the supplied session directory and worktree directory.
The public session endpoint omits OpenCode location workspaceID, so this adapter does not claim to observe that value.
The optional OpenCode parent session ID remains separate from the CMUX workspace UUID, CMUX surface UUID, and parent task ID.
The API `location.directory` is the session's project working directory, not OpenCode's internal session-storage directory.
The internal storage path is not exposed by the inspected public session route.
The adapter never uses a title, folder name, current shell directory, or model label to infer identity.

The caller must obtain the expected API server PID from evidence linked to the exact CMUX surface.
This client compares that explicit PID with GET /api/info before and after session inspection.
The client validates the CMUX UUID formats but does not connect to CMUX to authenticate their mapping.
The parent task ID is caller-supplied and is not verified by OpenCode.

## State result

The result disposition is clear only when the exact session is returned from the expected authenticated API process, the session is not archived, it is absent from the active-session map, and both pending lists are empty.
In v2.2.5 the active-session map contains `{ "type": "running" }` entries, and any such entry returns hold.
An unknown active-state shape, pending permission request, pending form, archived session, or unsupported response returns hold.
OpenCode documents sessions absent from the active-session response as inactive, which this adapter reports as `inactive`.
The inspected API does not expose native composer drafts, so clear means only that these API-visible checks passed.
The inspected routes are separate reads and do not provide an atomic state snapshot, so session state can change between or immediately after requests.
Callers must keep any future send path disabled until a separate native draft guard exists.

## Instance and compatibility limits

The v2.2.5 `ServerInfo` schema permits PID `0` when the runtime has no OS process identity, and this adapter holds because it cannot bind that instance to an expected process.
The API's server info response exposes a process PID and version but does not expose a stable server startup ID.
The observed PID detects many wrong-process cases and is checked at the beginning and end of one inspection.
It does not prove that a CMUX surface is attached to that API server, prevent PID reuse, or rule out a server restart that reuses the same PID.
The caller-supplied CMUX-to-PID association remains an external prerequisite.
The HTTP timeout applies to each blocking socket operation, not a total deadline across all inspection requests.

No OpenCode executable or installed source bundle was available in the assigned worktree environment.
The schema contract was checked against official source tagged v2.2.5 and is gated to the exact reported version.
The native OpenCode 2.2.5 routes and behavior have not been exercised here.
Any server version other than 2.2.5 remains unsupported until its schema is reviewed and tested separately.

## Focused synthetic verification

Run the bounded fake-server tests from the repository root:

    python3 -m unittest discover -s apps/opencode-adapter/tests -p 'test_*.py' -v

The tests use only a local synthetic HTTP server and temporary directories.
They cover exact session and directory matching, PID mismatch, running and unknown-state holds, pending permission and form holds, version and schema refusal, Basic authentication, timeout and response bounds, non-loopback refusal, and absence of non-GET requests.
