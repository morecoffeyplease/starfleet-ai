# Maintainer guide

This guide covers repository settings, routine verification, and the transition from a private staging snapshot to public source distribution.
The checklist does not authorize a visibility change, repository rename, history rewrite, package release, or service change.

## Routine verification

Run the canonical aggregate local verification on the final candidate:

```sh
python3 scripts/verify.py
```

GitHub-hosted CI is the routine verification path.
Require all four Python matrix results: **Python verification (ubuntu-latest, Python 3.10)**, **Python verification (ubuntu-latest, Python 3.14)**, **Python verification (macos-latest, Python 3.10)**, and **Python verification (macos-latest, Python 3.14)**.
Also require **Starfleet Markup** and **Source scan**.
The `verify` workflow runs for every pull request and push to `main` without path filters, so unaffected components still produce their required check results.
Do not make a check required until the workflow is enabled and its exact status name has been observed on a candidate run.

Keep the workflow read-only, pinned to full action commit SHAs, limited to GitHub-hosted runners, and free of live service credentials.
The source scan uses the official Gitleaks v8.30.1 Linux image pinned by digest.
It runs a synthetic nonfunctional `generic-api-key` positive control, then scans the checked-out source and all Git refs with the default rules.
No custom ignore rules or broad allowlists are added.
The scan does not certify software security or replace a publication review.

## Private-to-public source transition checklist

Complete each item against the exact candidate that is intended for publication.
Keep audit exports, private working paths, and unpublished historical material outside the public repository.

- [ ] Freeze the private staging candidate and record its exact revision in private release evidence.
- [ ] Audit all files in the intended source snapshot and the complete Git history that would be published.
- [ ] Review retained GitHub issues, pull requests, comments, and workflow logs for private information that will become public.
- [ ] Resolve each audit finding with a specific edit or an explicit disclosure decision.
- [ ] Repeat the source and history audit after the last candidate edit.
- [ ] Confirm ownership and required notices for the MIT-licensed source.
- [ ] Preserve the existing repository history in private retention before any repository transition.
- [ ] Reconcile development changes made after the staging snapshot and account for active work before selecting the final public source.
- [ ] Immediately after the authorized visibility transition, follow [GitHub's repository setup instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) to enable private vulnerability reporting and verify the setting and report form are available before any public announcement or onboarding.
- [ ] Create the `bug`, `enhancement`, `question`, and `conduct` labels before enabling the issue forms, because GitHub does not create missing issue-form labels.
- [ ] Confirm issue forms display the public-issue warning and apply the `conduct` label to community concerns.
- [ ] Confirm workflow permissions, hosted runners, action pins, and exact required check names on the final candidate.
- [ ] Confirm no package-registry release or support-service promise is implied by source publication.
- [ ] Coordinate with active owners and users of existing clones before reusing the canonical project URL.
- [ ] Provide explicit instructions for updating old clone remotes only after the repository transition is authorized.
- [ ] Obtain explicit approval for repository visibility, URL reuse, and any change to existing remotes.
- [ ] After authorization, repoint existing development clones to the retained private repository and verify their fetch and push remotes before reusing the canonical URL.
- [ ] Keep the final URL swap held until approval is recorded and the old-clone remote updates are complete, so a private-history push cannot target the new public repository.
- [ ] Record the final candidate, audit outcome, canonical verification result, and remaining unproven behavior in private release evidence.

Do not delete or rewrite existing history to resolve an audit finding without a separate explicit decision.
Do not publish the staging repository or modify the original repository as part of routine documentation or CI work.

## Repository settings to confirm

- Immediately after the authorized visibility transition, follow GitHub's repository setup instructions to enable private vulnerability reporting and verify the report form before public announcement or onboarding.
- Create the `conduct` issue label and verify the public Community concern issue form applies it.
- Keep blank issues enabled so reports that do not fit a form remain possible.
- Require the stable verification workflow checks only after confirming they run for all pull requests.
- Require approval before running workflows from all outside collaborators during the initial release.
- Keep the default workflow token read-only and do not grant pull-request write access to the secret scan.
- Do not enable a self-hosted runner for routine verification.
- Keep repository-level human review separate from any independent-model review requirement.
