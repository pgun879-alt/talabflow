# Security policy

## Status of this project

`talabflow` is an open-source portfolio project. It has **no production deployment, no users and
no clients**, and it is maintained by one person in his spare time. Only the `main` branch is
supported; there are no releases and no backports.

That context matters for your expectations, not for the handling: a real flaw in the
authentication, authorisation or export code is worth reporting, and will be fixed and described
honestly in the README rather than quietly patched.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository: the **Security** tab →
*Report a vulnerability*. That keeps the report private until a fix exists, and needs no email
address from either of us.

For anything that is not sensitive — a hardening suggestion, a question about the threat model, a
documentation error — open a normal issue instead.

Please include the version (commit SHA), the configuration involved, and the smallest sequence of
requests that shows the problem. A failing test is the most useful form a report can take.

**Expect best-effort, unpaid handling.** There is no SLA and no bug bounty. I will acknowledge a
report when I see it, and tell you plainly if I do not intend to fix something.

## In scope

The security model is described in the README's *Security* section. Reports that would change it
are in scope, in particular:

- **Authentication bypass or token forgery** — accepting a token with an unexpected `alg`, a
  missing or unverified claim, an expired token, or a token for a staff account that has since
  been deleted or deactivated.
- **Privilege escalation between roles** — a lower-privileged staff role reaching an endpoint or a
  field reserved for a higher one.
- **Cross-customer data exposure** — any path that returns one customer's order, phone number or
  message history to another customer, or to an unauthenticated caller.
- **Injection** — SQL injection through any input, or a spreadsheet formula that survives the
  export neutralisation in `exports.py` and executes when the file is opened.
- **Secret disclosure** — a bot token, JWT signing key or password hash appearing in a log line,
  an API response, an error page or an exported file.
- **Denial of service through an unbounded input** — a request, conversation step or export that
  consumes memory or database space without the documented limits applying.

## Deliberate decisions that are not vulnerabilities

Please read these before reporting, so neither of us wastes an afternoon:

- **Delivery is at-least-once, not exactly-once.** A notification can be delivered twice if a
  worker dies after sending but before recording the result. This is documented in the README's
  *Delivery guarantees* section, it is the standard property of a transactional outbox, and it is
  a design choice rather than a defect.
- **SQLite is the default, and it is single-node.** Concurrency is handled with a serialised
  writer and `PRAGMA busy_timeout`. PostgreSQL is supported by the schema and migrations but has
  never been run; the README says so. "SQLite does not scale across machines" is a known
  limitation, not a vulnerability.
- **Everything in `.env.example` is a placeholder.** Finding `replace-me` values there is the
  intended state. A credential-shaped string in that file is not a leaked credential.
- **The operator supplies the bot token.** There is no shipped token and no shipped account.
- **`scripts/` and the demo are offline by design.** They talk to no external service. That is a
  feature of the demo, not an oversight to be fixed.

## What is not covered

Issues in Python, FastAPI, SQLAlchemy, Alembic or Telegram themselves — report those upstream.
Vulnerabilities that require an attacker to already control the server, the database file or the
environment variables are out of scope, since every secret this project has lives there.
