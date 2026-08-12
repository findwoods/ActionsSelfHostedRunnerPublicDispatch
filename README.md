# ActionsSelfHostedRunner public dispatch

This repository is the single minimal public distribution for owner-controlled
public repositories that cannot call the private control repository's composite
Action.

It contains only:

- the repository-local dispatch Action;
- task schema/preflight checks;
- first-use `ipad-queue` bootstrap from the exact target commit;
- one-shot task-ID freshness checks.

It does not contain iPad credentials, GitHub App keys, a PAT, control-repository
access, or cross-repository reads/writes. The Action binds every API mutation to
the caller's `GITHUB_REPOSITORY` and uses only that workflow's
`GITHUB_TOKEN`.

Public callers must use `contents: write`, check out their own repository, and
pin this Action to a full commit SHA. Do not use `@main`, `@latest`, or a
moving branch. Pull-request events are rejected because the iPad worker is not a
sandbox for untrusted fork code.
