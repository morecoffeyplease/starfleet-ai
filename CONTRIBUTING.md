# Contributing

Thanks for helping improve Starfleet AI.
Start with the root [README](README.md) and the affected component's README and guide.

## Before making a change

Small bug fixes and documentation improvements can be proposed directly.
For a new integration, dependency, permission, service, storage format, or substantial workflow change, open an issue to describe the user problem before beginning substantial work.
An issue discussion does not promise that a change will be accepted or scheduled.

Use portable examples and synthetic data.
Do not require a contributor's personal files, private repositories, model preferences, Discord server, or credentials.

## Reproducing bugs

Describe the actual user path and reproduce it before changing code when the required environment is available.
For native session or delivery behavior, use the relevant native path when available.
If that path cannot be run, identify the exact behavior that remains unverified.
A unit test validates its modeled boundary and does not prove that a live agent received a message.
After a fix, repeat the same path and add a regression check where it protects a meaningful behavior.

## Verification

Run the canonical aggregate verification on the final candidate:

```sh
python3 scripts/verify.py
```

Run `git diff --check` for whitespace errors.
The Python-only subset is `python3 scripts/verify.py --python-only`.
The full runner installs JavaScript dependencies from the checked-in lockfile and installs a local Chromium browser for Starfleet Markup.
On Linux, install any operating-system libraries required by Playwright before running the browser suite.

For a component-specific change, the aggregate command remains the expected local verification.
GitHub Actions runs each Python component check in an isolated hosted job on Ubuntu and macOS with Python 3.10 and 3.14, with an overall parallel cap of four, plus the Node 22 browser suite on Ubuntu.
The existing required Python checks pass only when the complete component matrix and its preparation succeed.
Hosted CI uses synthetic boundaries and does not contact live models, CMUX sessions, Discord accounts, or paid services.
Manual native pilots remain separate evidence.

## Pull requests

Keep a pull request focused on one user problem.
Describe the final behavior, why it matters, the implementation, verification, risks, and intentional gaps.
Include the exact candidate revision and command results.
Separate configured state, attempted submission, and observed delivery.
For visible changes, include screenshots of the expected state.
State any unverified behavior plainly.

AI-assisted contributions are welcome.
Contributors are responsible for understanding the change and protecting private data.
Full prompts, complete agent transcripts, account information, and paid-model credentials are not required.
No contributor license agreement or developer sign-off trailer is required.
Contributions must be work the contributor is entitled to share under the MIT License.

Do not hand-edit generated files.
Change their source or use the supported generator.
