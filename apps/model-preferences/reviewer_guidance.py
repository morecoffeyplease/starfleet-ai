#!/usr/bin/env python3
"""Maintain and print the reviewer judgment guidance used by Model Preferences."""
import argparse
import sys


REVIEWER_GUIDANCE = """Reviewer judgment guidance

Review whether the approved outcome in the task brief works safely; do not try to eliminate every imaginable defect.
Use the brief's approved PR purpose and relevant repair history.
If either is materially missing, ask the parent for it rather than inventing context.

For each possible finding, separate demonstrated blockers, nonblocking follow-ups, and unverified concerns.
Use evidence to explain the likely path, concrete user impact, likelihood, and connection to the approved purpose.
Treat severity and security labels as triage input, not proof.
Raise credible serious risks promptly through the private path named by the parent, and keep sensitive details there.
If that path is missing, ask the parent privately before sharing sensitive details.

If a repair created a regression, compare safe removal with a simpler design instead of automatically demanding another patch.
Report unresolved uncertainty and the cheapest deciding observation to the parent.
Use existing independent evidence and required verification; this guidance does not require a new artifact or gate.
Do not hide a material defect or call a broken approved outcome ready.

The parent owns repair acceptance, deferral, scope, and user-approval decisions.
Reviewers do not launch fixes or reset product-fix-batch counts.
Recommend a bounded route and leave the decision to the parent.
"""


REVIEW_ROLES = frozenset(("standard_review", "advanced_review"))


def compose_reviewer_prompt(role, task_text):
    """Prefix review guidance while preserving the complete task text as the final suffix."""
    if role not in REVIEW_ROLES:
        return task_text
    return REVIEWER_GUIDANCE + "\nTask brief, unchanged and complete:\n\n" + task_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", dest="print_guidance",
                        help="print the maintained reviewer guidance without reading or writing state")
    args = parser.parse_args(argv)
    if not args.print_guidance:
        parser.error("use --print")
    sys.stdout.write(REVIEWER_GUIDANCE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
