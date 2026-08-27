# UPSTREAM.md — provenance & attribution

## Original upstream

This repository is based on **RanzMods/token-harbor**.

- Upstream author: RanzMods
- Original repository: https://github.com/RanzMods/token-harbor
- Based on upstream commit: [109254d9](https://github.com/RanzMods/token-harbor/tree/109254d9)
- License: inherited from upstream (see `LICENSE`)

## Local changes

The following changes were applied on top of the upstream commit:

1. **Hardcoded secrets removed** — `ROUTER_AUTH_TOKEN`, `DEFAULT_PASSWORD`, and `BYCF_SECRET` were replaced with environment variable lookups (`ROUTER_AUTH_TOKEN`, `GROK_DEFAULT_PASSWORD`, `BYCF_SECRET`) that fail closed (empty string) when unset.
2. **`dup.zip` excluded** — this archive duplicated secret-bearing source code and was removed from the publication tree.
3. **`.gitignore` added** — patterns to prevent runtime credentials, browser profiles, databases, and output files from being committed.

## Upstream remote

```bash
git remote add upstream https://github.com/RanzMods/token-harbor.git
```

## Security

This repository should contain zero live credentials, API keys, or passwords. All secrets must be provided through environment variables or ignored config files. Refer to `.env.example` if one is present.