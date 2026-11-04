# OpenCode session adapter guide

This guide describes a read-only adapter slice for OpenCode 2.2.5.
It does not enroll a session or authorize a message send.

## Required identities

Collect the actual OpenCode ses_ session ID from OpenCode's native session view or an approved session listing.
Collect the loopback API URL and Basic password from the process that owns the intended OpenCode session.
Collect the API server PID and verify separately that it belongs to the OpenCode process attached to the exact CMUX surface.
Collect the actual session directory and worktree directory from the intended session and task setup.
The adapter compares both to the public session `location.directory`, which is the project working directory and not OpenCode's internal session-storage directory.
Collect the CMUX workspace UUID, CMUX surface UUID, and parent task ID from their own authoritative sources.
Keep each identifier in its own field.
Do not substitute a tab title, workspace title, project folder, shell directory, or another session's identity.

## Supported API checks

The adapter accepts only a numeric loopback address such as 127.0.0.1 or ::1 with an explicit port.
It rejects hostnames, remote addresses, redirects, and API servers that do not challenge unauthenticated access.
It ignores environment proxy settings so credentials are sent directly to the loopback API.
OpenCode v2 Basic authentication uses the username opencode.
The password is supplied in memory by the embedding caller and is not printed or stored by this adapter.
Only server version 2.2.5 is accepted by this first contract.

The inspection reads GET /api/info and the exact GET /api/session/{sessionID} route.
It compares the returned session ID and location directory with the caller's expected values.
It then reads GET /api/session/active and the exact session's inbox, pending permission, and form lists.
An active `running` entry, archived session, pending inbox item, pending permission, pending form, mismatched identity, unknown state, unsupported schema, authentication error, timeout, and oversized response all produce a hold.
In v2.2.5, a session absent from the active map is inactive.
The API call set contains only GET requests.
The socket timeout applies to each blocking operation, not a total inspection deadline.

## Calling from a future component

The following Python example shows the adapter boundary.
The variable resolved_password must come from the embedding component's approved secret source.

    from opencode_adapter import OpenCodeSessionAdapter, SessionBinding

    adapter = OpenCodeSessionAdapter(
        "http://127.0.0.1:4096",
        password=resolved_password,
    )
    binding = SessionBinding(
        session_id="ses_REPLACE_WITH_OBSERVED_ID",
        expected_server_pid=12345,
        session_directory="/absolute/path/to/worktree",
        worktree_directory="/absolute/path/to/worktree",
        cmux_workspace_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        cmux_surface_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        parent_task_id="task:REPLACE_WITH_PARENT_TASK_ID",
    )
    inspection = adapter.inspect(binding)

Treat disposition clear as an observation that only the listed API-visible checks passed.
The OpenCode API does not expose native composer drafts, so a future send path needs a separate draft guard.
The API checks are separate reads rather than one atomic snapshot, and session state can change between or immediately after requests.

## Binding gaps

The current API reports a server PID and version but no stable startup identifier.
The adapter compares the expected PID before and after inspection, but PID reuse and CMUX attachment still require an external binding check.
The adapter does not ask CMUX for workspace or surface data and does not verify parent task ownership.
The public OpenCode session response strips its location workspace ID, so the adapter does not report an OpenCode workspace ID.
Those caller-supplied values are kept distinct from the OpenCode session, session directory, worktree directory, and API process.
The API-visible clear result is not proof that a native session received or read anything.

Read the [component README](README.md) for the exact API contract and supported synthetic checks.
