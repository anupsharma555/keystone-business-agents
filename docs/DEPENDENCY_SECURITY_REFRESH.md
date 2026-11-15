# November 2026 dependency security refresh

This review updates 16 existing pins in the development and orchestration
constraints. It adds no distribution. The candidate resolves to 126
distributions for both Python 3.11 on Linux and Python 3.13 on macOS. A fresh
audit of those exact pins reports no known vulnerabilities in its feed. Two
LangGraph advisories were checked against upstream notices as well, including
one SQLite checkpoint advisory that the audit feed did not report. The isolated installation and offline validation are recorded below. They do not
establish live-provider compatibility or upgrade the running operator environment.

## Technical index

- [Decision and package review](#decision-and-package-review)
- [Security evidence and limits](#security-evidence-and-limits)
- [Compatibility and validation](#compatibility-and-validation)
- [Rollback](#rollback)

## Decision and package review

The selected approach refreshes existing packages within the project's
`.[dev,orchestration]` dependency set. Keeping the old pins would retain
upstream affected versions. Adding a new package would not address those
versions and would increase the installation and compatibility surface. Each
row below is an existing dependency; “updated” means the constraint changed,
not that an operator environment was upgraded.

| Package | Purpose | Decision and reason |
| --- | --- | --- |
| `aiohttp` 3.14.3 → 3.14.4 | Async HTTP transport | Updated to the upstream security patch; the direct minimum is 3.14.4. [Advisory](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-5pv8-vvhc-h7jv). |
| `certifi` 2026.2.25 → 2026.10.18 | TLS root certificate bundle | Updated to the release that removes compromised emSign roots. The [upstream advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) lists 2026.10.18 as patched. |
| `filelock` 3.29.0 → 4.2.1 | Filesystem locking for installed tooling | Updated to the patched release in the [upstream advisory](https://github.com/tox-dev/filelock/security/advisories/GHSA-cwqr-hh33-gh44). This is a major-version change and needs clean-environment compatibility testing. |
| `langgraph` 1.2.11 → 1.2.14 | Orchestration graph engine | Updated to the [patched core release](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-3fx2-cqw9-xr3c); the orchestration minimum is 1.2.14. |
| `langgraph-sdk` 0.4.4 → 0.4.6 | LangGraph SDK dependency | Updated with core to the [patched SDK release](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-3fx2-cqw9-xr3c). |
| `langgraph-checkpoint-sqlite` 3.1.1 → 3.1.2 | Local graph checkpoint storage | Updated to close the [SQLite namespace advisory](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-6hmw-9432-67qq); the optional orchestration minimum is 3.1.2. |
| `langgraph-checkpoint` 4.1.1 → 4.3.0 | Checkpoint base contract | Updated because the patched SQLite package requires 4.3.0, per the [same upstream advisory](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-6hmw-9432-67qq). |
| `mcp` 1.28.1 → 1.30.0 | Model Context Protocol dependency | Updated within the retained Agents SDK's declared `>=1.19,<3` range; no new integration is enabled. [SDK dependency source](https://github.com/openai/openai-agents-python/blob/v0.22.2/pyproject.toml). |
| `msgpack` 1.2.1 → 1.2.3 | Compact binary serialization | Updated existing transitive pin in the security resolution; no application serialization format changes. [Release](https://pypi.org/project/msgpack/1.2.3/). |
| `multidict` 6.7.1 → 6.9.1 | Multi-value HTTP mappings | Updated existing HTTP-stack pin in the security resolution. [Release](https://pypi.org/project/multidict/6.9.1/). |
| `pyasn1` 0.6.4 → 0.6.5 | ASN.1 parsing in authentication dependencies | Updated existing transitive pin in the security resolution. [Release](https://pypi.org/project/pyasn1/0.6.5/). |
| `PyJWT` 2.13.0 → 2.15.1 | JSON Web Token parsing | Updated beyond the affected range in the [upstream advisory](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-gvp8-978c-rx2q); direct minimum is 2.15.1. The advisory does not name a patched version, so the audit result and [upstream defensive-copy code](https://github.com/jpadilla/pyjwt/blob/2.15.0/jwt/api_jwt.py) are separate evidence. |
| `pypdf` 6.16.1 → 6.21.4 | PDF parsing | Updated to the [upstream fixed release](https://github.com/py-pdf/pypdf/security/advisories/GHSA-mfr5-77xh-9j7h); direct minimum is 6.21.4. |
| `setuptools` 84.0.0 → 84.1.0 | Build and development packaging | Updated the existing build-tool pin and both build/dev minimums. [Release](https://pypi.org/project/setuptools/84.1.0/). |
| `urllib3` 2.7.0 → 2.8.0 | HTTP connection and response handling | Updated to the [upstream fixed release](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g); direct minimum is 2.8.0. |
| `yarl` 1.23.0 → 1.25.1 | URL handling in the async HTTP stack | Updated because the aiohttp resolution requires the newer existing pin. [Release](https://pypi.org/project/yarl/1.25.1/). |

The core SDK family remains `openai-agents==0.22.2` and `openai==3.12.0`.
The selected graph packages remain in the existing major-version family.
No alternative framework or replacement HTTP/PDF library was selected: this
is a security refresh of the installed architecture, and changing families
would add migration risk without addressing a demonstrated need here.

## Security evidence and limits

The prior CI advisory scan reported findings in the old pinned set. Fresh
resolver reports for Linux Python 3.11 and macOS Python 3.13 each contain
exactly 126 distributions, with no package added or removed. An audit of the
final 126 exact pins reports zero known vulnerabilities. The direct minimums
in `pyproject.toml` prevent a fresh unconstrained installation from choosing
the reviewed affected versions of aiohttp, PyJWT, pypdf, urllib3, LangGraph,
SQLite checkpoints, and setuptools.

The audit feed does not cover every upstream advisory at the same time. In
particular, the [LangGraph SQLite advisory](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-6hmw-9432-67qq)
required a separate primary-source check and the additional checkpoint pair
above. A zero-finding audit is a time-bound statement about that feed and
those exact pins; it is not proof of complete upstream coverage or application
security. The [PyJWT advisory](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-gvp8-978c-rx2q)
also lacks fixed-version metadata even though the candidate clears the fresh
audit and upstream source contains the defensive-copy change.

## Compatibility and validation

The two CI jobs now have 30-minute limits. Their test policy, permissions, and
live-mode settings are unchanged. Resolver and advisory evidence is complete
for the pinned set. A separate validation environment contains exactly the 126
pins plus the editable project. `pip check` and its installed-package audit
passed; the latter reports zero known findings in the current feed. The focused
graph, SDK, source and dependency gate passed 396 tests, and the full offline
suite passed 8,370 tests with 75 skipped. Ruff and diff checks passed. GitHub CI
is the separate final publication gate; live-provider behavior is not claimed.

`filelock` crosses a major version, including an on-disk lock protocol change
for its `SoftReadWriteLock`. KBA has no direct `filelock` import in the reviewed
source, and the isolated compatibility gate covers transitive tooling without
upgrading an existing operator environment. The optional `extraction` extra is outside this 126-package
candidate and retains its separate review requirement.

Updating source constraints does not upgrade an already installed operator
runtime. Package installation and tests should use a separate environment;
no live model or provider call is part of this validation.

## Rollback

Restore the previous `pyproject.toml` floors and `constraints/ci.txt` manifest
from version control, then build a fresh environment from those versions.
Do not downgrade a shared or active interpreter in place.
