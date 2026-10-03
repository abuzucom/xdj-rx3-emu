# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main` | Yes |
| `master` (upstream mirror) | No |

## Scope

Scope includes code in this repository. Scope includes configuration in this
repository. Scope includes third-party vulnerabilities when a selected version
causes the defect. Scope includes third-party vulnerabilities when a selected
pin causes the defect. Scope includes third-party vulnerabilities when
configuration causes the defect. Scope includes third-party vulnerabilities
when permissions cause the defect. Scope includes third-party vulnerabilities
when an integration causes the defect. Route purely upstream defects to the
upstream maintainers.

Scope excludes social engineering. Scope excludes physical attacks.

## Reporting a Vulnerability

Report vulnerabilities through GitHub private vulnerability reporting. Open
the Security tab. Select Report a vulnerability. Never open a public issue for
a vulnerability report.

Response timing: acknowledgment within 7 days.

Remediation target: a fix or mitigation within 90 days.

## Disclosure Policy

Coordinate disclosure with the maintainers. Keep the report private before a
disclosure event. A shipped fix defines one disclosure event. Passage of
90 days after the report defines another disclosure event. Use the earlier
event.

## Agent policy security

`AGENTS.md` is the canonical policy. Supporting policy documents live under
`docs/agent-policy/`. The loader reads local files only. It rejects missing,
malformed, non-ASCII, oversized, symlinked, special, absolute, and escaping
paths. It assembles deterministic content and fails closed.

Repository hooks provide defense in depth. Repository writers can modify those
hooks.
Use an external harness, filesystem isolation, or server-side controls for
tamper resistance.

Hosted GitHub operations use the trusted wrapper. A managed Codex loopback
proxy at `127.0.0.1:9` does not prove GitHub CLI failure. Agents cannot modify
Git Credential Manager or refresh GitHub tokens through a browser.

Controlled adopters must preserve license-required attribution and source
metadata. Each uncontrolled mirror remains responsible for its own legal
compliance. This repository cannot enforce or verify third-party practices.

Every repository change requires a versioned SemVer changelog entry. Adopting
repositories must not use `[Unreleased]`.
