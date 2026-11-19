# Stalwart Server, patched build

[Stalwart](https://github.com/stalwartlabs/stalwart) at an exact upstream release, with a small
set of patches applied, built into the container image this repository's owner runs. It is
published because the server is AGPL-3.0: anyone who uses it over the network is entitled to
its complete source, and this repository — the upstream pin, every patch and the build — is that
source.

Nothing else lives here. There is no configuration, no deployment and no data.

## What is patched, and why

The first two patches are about search on a **PostgreSQL** data store, where Stalwart answers
every full-text query with Postgres's own `tsvector` search. Each was measured against upstream's
image before it was written, with the harness below. The third is about where spam is delivered,
and the fourth about which mail is said to have an attachment. The fifth and sixth are about
authentication: an OIDC directory that outlives its identity provider being down, and errors that
never log a credential. Each patch's commit message says what it changes.

| Patch | Upstream behaviour | Patched |
| --- | --- | --- |
| [`0001`](patches/0001-postgres-split-unstemmed-fields.patch) | Postgres keeps an e-mail address, a host name and a hyphenated word as one token, so an address field matched only its whole value: searching a sender's domain, subdomain or hyphenated domain — or a calendar attendee's domain — found nothing. | Fields indexed without a language are split on punctuation when indexed and when queried, which is what Stalwart's internal search engine does for them. The query also matches the unsplit form, so rows indexed before the patch keep matching until a reindex rewrites them. |
| [`0002`](patches/0002-subject-index-keeps-tag-words.patch) | The subject was indexed without its leading `[tags]` and reply prefixes, so a word inside `[Ticket ID: 4711]` could not be found, and a word only in the tag not at all. | The subject is indexed as its thread name followed by what was removed. Sorting by subject is unchanged. |
| [`0003`](patches/0003-ingest-keeps-notjunk-in-inbox.patch) | A message the spam filter classified as spam is re-routed to Junk whenever it is headed for the Inbox, and a Sieve script cannot prevent it: `fileinto "INBOX"` is the same destination as an implicit keep. A sender an organisation trusts still lands in Junk. | A message the account's own Sieve script marked `$notjunk` stays in the Inbox. Every other spam message bound for the Inbox still goes to Junk. A sender cannot set a keyword; only the account's script can. |
| [`0004`](patches/0004-has-attachment-skips-embedded-pictures.patch) | `hasAttachment` was true for every non-text part, so a message whose only extra part is a picture its HTML shows through a Content-ID (a signature logo in a `multipart/related`) said it had an attachment, and appeared under the `hasAttachment` filter. | A part with a Content-ID, not marked `attachment`, that the HTML body refers to as `cid:` is part of the body, as RFC 8621 allows. A real attachment, a picture the HTML does not show, and an attached message still count. Ingest and Email/parse decide it in one place. |
| [`0005`](patches/0005-oidc-directory-retries-discovery.patch) | An OIDC directory fetched its identity provider's discovery document and keys once, when it was built. If the provider could not be reached then (it was down, or started after the server), the directory was left out and never retried: every Bearer token fell through to the internal decoder and was rejected until a restart, and Basic logins went to the internal directory instead. | The directory is always built. While its provider cannot be reached it rejects every token as an authentication error (not a failed login, so no ban counts it), and retries discovery when a token arrives, at most once per 10 to 30 seconds and by one caller at a time, without making anyone wait for an attempt in flight. Once discovery succeeds it verifies tokens exactly as before. `/api/discover` answers 503 for such a domain rather than the server's own metadata. |
| [`0006`](patches/0006-auth-errors-omit-credentials.patch) | An authentication error logged the credential it rejected: a whole Bearer or refresh token that did not decode, the value of an `Authorization` header with an unknown scheme or an undecodable Basic header, a stored password (plaintext for an LDAP or SQL directory that stores them so) or hash that could not be verified, and a TOTP URI with its secret. | Each error says what failed without the value. The trace-level raw protocol events, which record the wire by design, are unchanged. |

`0001` and `0002` change what is stored in the index, so a deployment has to run a store-wide
reindex once (`0003` to `0006` do not touch the index). The image says which index it writes: the label
`io.github.kempu.stalwart-server.search-index-format` (from `SEARCH_INDEX_FORMAT` in
`upstream.lock`) changes exactly when the index does, so a deployment can compare it with what
its database was last reindexed to and reindex only then. The harness's `rollout` mode rehearses
that reindex.

`0004` changes what delivery decides, not the index. Whether a message has an attachment is
decided once, when it is delivered or imported, and stored with it; Email/get returns that, and a
reindex rebuilds the index's `hasAttachment` from it rather than from the message. So mail
delivered before the patched image keeps its old answer, a reindex included (the harness's
`rollout` mode measures exactly that), and only Email/parse, which reads the message itself,
answers afresh.

`0005` and `0006` carry their own tests, run in the patched tree (`src/` after
`scripts/ci-prepare.sh`): `cargo test -p directory` builds OIDC directories against a stand-in
identity provider on 127.0.0.1 that is down, failing, misconfigured or serving a key made for the
test, and checks that a stored secret never reaches an error; `cargo test -p common --lib
auth::oauth::token` checks the same for a token that does not decode.

## Building

`upstream.lock` pins everything: the upstream tag and commit, the build and runtime images by
digest, the features, the release name. `scripts/build-image.sh` is the whole build. It runs
`scripts/ci-prepare.sh`, which runs the publish gate, clones that commit, refuses it if it is not
the pinned one, applies `patches/` in order and copies the `Dockerfile` into the patched tree
(the build context). Then it runs one `docker buildx build` whose every argument comes from
`upstream.lock`, and refuses the image unless the server binary inside it is x86-64:

```sh
bash scripts/build-image.sh                  # stalwart-server:<RELEASE>, in local docker
bash scripts/build-image.sh --push IMAGE     # IMAGE:<RELEASE> pushed, and its digest printed
```

The image is linux/amd64 whatever the host: the compile stages run on the build platform and
cross-compile to x86_64, so an arm64 machine compiles natively and emulates only the runtime
stage's few `RUN` steps.

The Dockerfile is upstream's own, with four differences:

- **Every image is pinned by digest**; upstream's floats on `:latest`.
- **Only `CARGO_FEATURES` is compiled** (`postgres s3`), and **not `enterprise`**: that code is
  under the Stalwart Enterprise License, not the AGPL this build is published under. Without a
  licence key the upstream image runs that code switched off, so for a deployment without one the
  behaviour is the same. The one difference visible from outside is `/api/account` reporting the
  edition as `oss` rather than `community`.
- **Thin LTO and 16 codegen units**, instead of fat LTO with one unit, which needs several times
  the memory and time.
- **OpenTelemetry comes from its own repository.** Upstream's `Cargo.lock` takes it from upstream's
  fork, `github.com/stalwartlabs/opentelemetry-rust`, which is no longer public, so a build from
  scratch could not fetch it. The commit the lock pins is still served by
  `github.com/open-telemetry/opentelemetry-rust`, the repository the fork was made from, and the
  build fetches it there (a git `insteadOf`). The lock file is unchanged and cargo checks the
  checkout is the pinned commit, so the source is the same.

**Memory.** Measured on v0.16.21-kempu.1: the build peaks at about 4.1 GB, and nearly all of it
is the last step, linking the server with thin LTO across every crate, which needs about 4 GB
whatever `BUILD_JOBS` is. Under a 3.8 GB cap that link is killed with SIGKILL at the end of the
build, and so it is inside any BuildKit daemon with a fixed memory limit below that. The largest
single crate, `registry` (about 87,000 lines of generated code), peaks at 1.45 GB. Give the
builder 6 GB.

The image carries `/usr/share/doc/stalwart/SOURCE`, naming this repository and the exact
revision it was built from.

## The search harness

`harness/` runs one Stalwart on one PostgreSQL 16, seeds a fixed, invented corpus (mail and
calendar events) into a throwaway account, and checks what each search finds against what it
should find:

```sh
harness/run.sh stock                       # upstream's image: records the broken answers
harness/run.sh patched IMAGE               # a patched image from an empty database
harness/run.sh rollout IMAGE               # stock indexes the data, the patched image then
                                           # answers from those rows, a reindex, then again
```

It also imports a few messages with parts (a signature logo shown by its Content-ID in the two
shapes mail programs send it, an attached document, a picture without a Content-ID, one the HTML
does not show, a shown one marked `attachment`, an attached message, a photo in a
`multipart/mixed` body) and asks each surface that answers `hasAttachment`: Email/get, the
`hasAttachment` filter of Email/query, and Email/parse.

`rollout` is the rollout itself: the patched server must answer exactly as stock did over rows
stock indexed (nothing lost before the reindex), and answer correctly after it, except that what
delivery decided (`hasAttachment`) stays as stock decided it. Run it on every
upstream update before moving `upstream.lock`; a probe whose stock answer changes is upstream
fixing (or breaking) something, and says which.

### The push harness

```sh
harness/run.sh push IMAGE                  # what a push subscription's emailPush filter lets through
```

The mail app narrows its push subscription with an `emailPush` filter so that mail a Sieve rule
or the spam filter files into Archive, Junk, Trash, Sent or Drafts never wakes a phone, and the
push relay wakes on the `EmailPush` object the server then posts. `push` measures every claim that
rests on: without the filter an Archive delivery posts a `StateChange`; the filter can be set on
a live subscription; with `inMailboxOtherThan` those five folders post nothing while the Inbox
and a user's own folder each post one `EmailPush`; and with no properties the post is exactly
`{"@type": "EmailPush", "accountId", "emails": [{}], "state"}`. Mail is delivered over SMTP
because only the delivery path raises the push. A stand-in for the relay records the posts
behind a certificate from a CA made for the run. Run it with the image being deployed, and on
every upstream update.

## Hygiene

Everything here is public, including every commit's author line. `scripts/check-hygiene.sh`
runs before every build and refuses one when a commit is not authored under a GitHub no-reply
address, when anything carries a session link, or when a patch or the harness adds an e-mail
address outside the reserved example domains. The build pipeline adds a private list of names
that must never appear here.

## License

AGPL-3.0-only, as upstream ([`LICENSE`](LICENSE)). Stalwart is © Stalwart Labs LLC.
