# Validated dependency constraints

`ci.txt` pins the resolved development and orchestration dependency versions. It
does not install optional packages by itself and is not a wheel-hash lock.

Use the repository interpreter when maintaining an existing environment:

```bash
.venv/bin/python -m pip install --upgrade pip setuptools wheel -c constraints/ci.txt
.venv/bin/python -m pip install --no-build-isolation -e ".[dev,orchestration]" -c constraints/ci.txt
.venv/bin/python -m pip check
```

Build isolation is disabled only after the pinned build tools are installed. For
a minimal installation, use `.[dev]` instead. CI verifies that this installation
does not contain LangGraph, then separately runs the full suite with the graph and
SQLite checkpoint packages installed. The existing Python 3.11 job is retained;
the compiled-graph job uses Python 3.13, matching the local compatibility runtime.

The SDK 0.22.2 / OpenAI 3.12.0 / LangGraph 1.2.11 candidate was resolved and tested
in an isolated environment. Existing dependency versions were retained where the
new packages' requirements permitted them. The SDK's stricter tool-call identity
validation required unique IDs in fake-model fixtures. Model defaults were not
changed. Remote CI and live provider compatibility are separate validation layers.

The September 10 security review updated 16 existing pins and retained the same
126-package development, orchestration, and build-tool dependency set. A fresh
PyPI advisory audit of that complete set reported no known vulnerabilities. The
direct requirements also require the reviewed fixed versions of aiohttp, pypdf,
PyJWT, python-multipart, and Starlette. A clean advisory result concerns known
package advisories; it does not establish application or live-provider security.
The checkpoint update includes the upstream
[checkpoint deserialization fix](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-fjqc-hq36-qh5p).

The September 18 integration audit identified two newer AnyIO advisories in
4.13.0 (CVE-2026-63374 and CVE-2026-64847). Only the AnyIO pin moves to
4.14.2 for the upstream fixes; the remaining pins are unchanged. The proposed
complete pinned set passes the advisory audit. Compatibility is validated in
an isolated test environment; existing operator environments are not upgraded
by a source merge. See the [upstream 4.14.2 changes](https://github.com/agronholm/anyio/releases/tag/4.14.2).

The optional `extraction` extra is outside this validated package set. Its
cryptography bound now allows the security-fixed 50.x series, which is compatible
with [pyOpenSSL 26.4](https://pypi.org/project/pyOpenSSL/26.4.0/)'s declared
`>=49,<51` requirement. The previous `<49` bound
excluded the upstream [PKCS#7 decryption fix](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5).
[Crawl4AI 0.9.x](https://pypi.org/project/Crawl4AI/0.9.1/) also brings NLTK and an
unclecode-litellm dependency; these are not
required by KBA's ordinary SDK or LangGraph runtime. NLTK currently has an
[unfixed model-path advisory](https://github.com/nltk/nltk/security/advisories/GHSA-8mgp-746c-j5xp).
Do not treat installation of that optional extra as covered by the clean core
audit or the SDK compatibility gate. Optional extraction needs its own resolver,
advisory, and offline runtime review before use. Cryptography 49+ also removed
Intel macOS binary wheels; see the [upstream compatibility notes](https://cryptography.io/en/latest/changelog/).

To refresh constraints, resolve the intended package change in a separate virtual
environment, retaining the previous constraints for unrelated dependencies. Save a
pip installation report with `--dry-run --ignore-installed --only-binary=:all:
--report`, then generate sorted `Name==Version` lines from each installation's
`metadata.name` and `metadata.version`. Install that exact list into a clean
environment and run `pip check`, focused compatibility tests, and the full no-live
suite before accepting it. Review resolver-required transitive changes explicitly.

Rollback consists of restoring the previous manifest and constraints and creating
a fresh environment from them. Do not downgrade or upgrade a shared interpreter
while another validation run is using it.
