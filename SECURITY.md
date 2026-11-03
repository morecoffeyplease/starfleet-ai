# Security policy

## Report a vulnerability

After the repository becomes public, use GitHub's private vulnerability reporting form.
The maintainer transition checklist requires enabling the form and verifying its availability immediately after the authorized visibility change and before any public announcement or onboarding, following [GitHub's repository setup instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).
The private staging repository does not claim that this route is currently available.

Do not report vulnerability details in public issues or pull requests.
Include the affected component and revision, a minimal reproduction, the expected security boundary, and the possible impact.
Use synthetic credentials and sessions where possible.
Do not send active credentials or test against another person's installation.

Maintainers will coordinate assessment and disclosure through GitHub's private report.
There is no guaranteed response time, bounty program, or emergency security service.

## Supported versions

Security attention focuses on the latest published source release.
Older releases do not have a backport commitment.
Known affected revisions and available mitigations will be described when a fix is released.

## Trust boundaries

Starfleet tools run locally with the permissions available to the user and agent harness.
They do not create an operating-system security boundary around an agent.
Native CMUX trust prompts, exact session ownership, and human-input holds remain in force.
Discord messages pass through Discord, and enabled components may retain local session or conversation evidence.
Review each component's storage and retention documentation before sharing logs or removing state.
