# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub:
[Report a vulnerability](https://github.com/git-cgallagher/ansible-inventory-system/security/advisories/new)
(the **Security** tab, then **Report a vulnerability**). Don't open a public issue for a security problem.

You can expect an acknowledgement within a week. This is a one-person project, so fixes are best effort.

## Supported versions

Only the latest release gets fixes. Until 1.0 the project is pre-release. Since phase 1 every
API route needs a bearer token (hashed at rest) and local passwords are scrypt-hashed, but the
server has no TLS of its own and no rate limiting: put it behind a TLS-terminating reverse proxy
and do not expose it beyond a trusted network.

## Scope

The server is a FastAPI application that stores inventory in PostgreSQL and serves it to the
`git_cgallagher.inventory.db` inventory plugin over HTTP. It is designed never to hold secret
values in plaintext: vault-encrypted strings are stored opaque and decrypted only by Ansible on
the controller. Tokens are shown once at creation and stored only as a SHA-256 hash. The example
`deploy/compose.yml` uses development-only placeholder credentials.
