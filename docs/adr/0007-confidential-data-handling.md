# ADR-007: Handling confidential policy content in a public repository

**Status:** accepted

## Context
The submission must be a public GitHub repository; the policy documents are confidential.

## Decision
- PDFs live in `data/private_policies/`, which is git-ignored. Docker mounts them **read-only at
  runtime** and never bakes them into the image.
- Rule values transcribed from the policies (entitlements, limits, references) live in
  `data/private_policies/config/*.yaml`. Public `*.example.yaml` files hold placeholders.
- Evaluation cases that assert policy facts live in `evals/private/`, which is git-ignored.
- Tests use synthetic PDFs generated inside the test.
- CI fails if any file under the private folders, or a `.env`, is ever committed.

## Consequences
Reviewers can read all the code but cannot run the policy-dependent parts without the documents.
The demo video and the public eval results show the behaviour.
