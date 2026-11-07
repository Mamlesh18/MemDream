# Security policy

MemDream stores what people tell AI agents about themselves, so security bugs
here can expose personal data. Thank you for reporting them responsibly.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub:
**[Report a vulnerability](https://github.com/Mamlesh18/MemDream/security/advisories/new)**
(the *Security* tab → *Report a vulnerability*).

Include what you can of:

- what the problem is and what an attacker could do with it;
- steps or a minimal script to reproduce it;
- the MemDream version or commit, and the storage / vector backend involved.

We aim to acknowledge a report within 7 days and to agree a disclosure timeline
with you. Fixes are released before details are made public, and reporters are
credited unless they prefer not to be.

## Supported versions

MemDream is alpha software. Security fixes go into the latest release and
`main`; older releases are not patched. Pin a release tag, and upgrade when a
security fix is announced.

| Version | Supported |
|---|---|
| latest release | yes |
| older releases | no |

## What counts

Especially relevant for this project:

- **Cross-user access** — any way for one scope (`user_id`, `agent_id`,
  `client_id`, `namespace`) to read, change or influence another's memories.
- **Erasure that is not** — data that survives `delete()` or `purge()`.
- **Identity** — recovering a phone number, or computing another user's id.
- **Model-controlled actions** — a model response or a stored memory causing
  MemDream to touch memories it was not shown, bypass validation, or retire
  protected facts.
- **The optional HTTP service** — authentication bypass, or an API key bound to
  one client reaching another client's data.

Out of scope: an agent *believing* something false it was told (that is what
memory is), denial of service by sending a very large volume of requests to a
service you run, and vulnerabilities in dependencies that MemDream does not
expose — report those upstream.

## Running MemDream safely

- Set `DREAM_IDENTITY_SALT` to a long random secret if you identify users by
  phone number, and never change it.
- If you run the HTTP service anywhere other than localhost, set
  `DREAM_API_KEYS` (ideally bound to clients) and put it behind TLS.
- Keep model and database credentials in the environment or a secret store,
  never in code or prompts.
- See the production checklist in [`docs/self-hosting.md`](docs/self-hosting.md).
