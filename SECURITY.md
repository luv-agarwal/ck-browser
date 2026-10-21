# Security policy

## Supported versions

Only the `main` branch is supported. Fixes land there.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's Security tab and click
"Report a vulnerability". Please do not open a public issue for a security problem.

For a non-sensitive issue, such as hardening advice or a documentation gap, a normal GitHub issue
is fine.

## What counts

- Anything that lets a page or a step file read or write beyond what the README documents.
- Leakage of the Jev key (`TYPESAFE_API_KEY`) into logs, `--out` or anywhere else.
- A way around the protections of the debugging port or the Chrome profile.

## Threat model

- What is sent to Jev, and what is masked, is listed in the README section "The Jev key".
- The debugging port and the profile permissions are described in the README section "Its own
  Chrome". The port has no authentication: while that Chrome runs, a local process that finds the
  port can drive it.
- A step file has the power of a script. Run only pages and step files you trust.
- `--out` holds the page text of every decision in `jev-trace.jsonl`. It is created private (0700).
- The extension bridge accepts only the configured extension ID.
