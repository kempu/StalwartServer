#!/usr/bin/env python3
"""The search probe: what a server under test actually finds.

Seeds a fixed, fictional corpus into a throwaway user account, then asks the
server the questions the patches in ../patches exist to answer and compares each answer
with the set it should be. Run through run.sh, which starts the harness and picks the
image; see README.md ("The search harness").

  probe.py seed                  create the probe account, import the corpus, wait for the index
  probe.py check --expect MODE   run every probe; exit non-zero if any answer differs
  probe.py reindex               queue a store-wide reindex and wait until its rows are searchable

MODE is `stock` (upstream's image: the answers the patches change are the broken ones),
`patched-stale` (our image over rows indexed by stock: must answer exactly as stock did,
so a rollout never loses a match before its reindex), or `patched` (our image after the
reindex: the answers are right).

Standard library only, and every name, address and word in it is invented.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('HARNESS_HTTP_PORT', '18080')}"
DOMAIN = "harness.example.org"
USER = f"probe@{DOMAIN}"

def basic(user, secret):
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()

# The recovery admin creates the account and queues the reindex. Its own mailbox is NOT used:
# it is a synthetic account (id u32::MAX) that Email/import accepts and every query refuses
# with serverUnavailable, which is the first thing this harness ran into.
ADMIN_AUTH = basic("admin", os.environ["HARNESS_ADMIN_PASSWORD"])
USER_AUTH = basic(USER, os.environ["HARNESS_USER_PASSWORD"])
MAIL = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
CAL = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
ADMIN = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
MARKER = "harnessmarker"

# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

MESSAGES = {
    "M1": ("Alex Chen <alex.chen@example.com>", "Sam Rivera <sam.rivera@example.org>",
           "Quarterly invoice", "Please find the quarterly invoice attached. Pay with EveryPay."),
    "M2": ("Every Pay Billing <billing@every-pay.example>", "alex.chen@example.com",
           "[Ticket ID: 4711] Payment link", "Your bank link (Every-Pay) for invoice 1."),
    "M3": ("Support Desk <support@example.com>", "alex.chen@example.com",
           "Re: [Acme-Support] Password reset", "Following up on your request."),
    "M4": ("Jordan Lee <jordan@example.net>", "alex.chen@example.com",
           "Check-in reminder", "Hotel check-in is at 3pm."),
    "M5": ("Casey Morgan <casey@sub.example.com>", "alex.chen@example.com",
           "Offsite agenda", "Agenda for the offsite."),
}

EVENTS = {
    "V1": {
        "title": "Planning session",
        "participants": {
            "p1": {"@type": "Participant", "name": "Charles Hayes",
                   "calendarAddress": "mailto:charles.hayes@example.com",
                   "roles": {"attendee": True}, "participationStatus": "needs-action"},
            "p2": {"@type": "Participant", "name": "Alex Chen",
                   "calendarAddress": "mailto:alex.chen@example.com",
                   "roles": {"owner": True, "attendee": True}, "participationStatus": "accepted"},
        },
    },
    "V2": {
        "title": "Design review",
        "locations": {"l1": {"@type": "Location", "name": "Conference Rooms"}},
    },
}

# ---------------------------------------------------------------------------
# The probes: (name, kind, filter, {mode: expected keys})
#   `same` = every mode expects this set. A probe without a `patched-stale` entry
#   expects there what it expects under `stock` — a rollout loses nothing before the
#   reindex, and gains nothing either, because the gains are in the index.
# ---------------------------------------------------------------------------

def same(keys):
    return {"stock": keys, "patched": keys}

PROBES = [
    # Addresses are one token to Postgres: only a whole address matches (patch 0001).
    ("from: a domain", "mail", {"from": "example.com"},
        {"stock": set(), "patched": {"M1", "M3", "M5"}}),
    ("from: a hyphenated domain", "mail", {"from": "every-pay.example"},
        {"stock": set(), "patched": {"M2"}}),
    ("from: a subdomain", "mail", {"from": "sub.example.com"},
        {"stock": set(), "patched": {"M5"}}),
    ("to: a domain", "mail", {"to": "example.org"},
        {"stock": set(), "patched": {"M1"}}),
    ("from: a whole address", "mail", {"from": "alex.chen@example.com"}, same({"M1"})),
    ("from: a display-name word", "mail", {"from": "chen"}, same({"M1"})),
    # The subject is indexed as its thread name, so a leading [tag] is invisible (patch 0002).
    ("subject: a word inside [Ticket …]", "mail", {"subject": "ticket"},
        {"stock": set(), "patched": {"M2"}}),
    ("subject: a word inside Re: [Acme-Support]", "mail", {"subject": "acme"},
        {"stock": set(), "patched": {"M3"}}),
    ("text: a word that is only in a tag", "mail", {"text": "ticket"},
        {"stock": set(), "patched": {"M2"}}),
    ("subject: a word after the tag", "mail", {"subject": "password"}, same({"M3"})),
    # Unchanged by any patch; recorded so a change upstream is noticed. The client adds the
    # joined-up spelling itself (MailiApp lib/searchWords.ts).
    ("text: a hyphenated word as typed", "mail", {"text": "every-pay"}, same({"M2"})),
    ("text: its joined-up spelling", "mail", {"text": "everypay"}, same({"M1"})),
    # Calendar: organizer/attendee/location are indexed without a language.
    ("calendar text: an attendee name", "cal", {"text": "charles"}, same({"V1"})),
    ("calendar attendee: a name", "cal", {"attendee": "charles"}, same({"V1"})),
    ("calendar text: a location word", "cal", {"text": "rooms"}, same({"V2"})),
    ("calendar location: a word", "cal", {"location": "rooms"}, same({"V2"})),
    ("calendar attendee: a domain", "cal", {"attendee": "example.com"},
        {"stock": set(), "patched": {"V1"}}),
]

# Sorting by subject must stay by thread name — the subject patch appends, never prepends.
SORT_EXPECTED = ["M4", "M5", "M3", "M2", "M1"]

# ---------------------------------------------------------------------------
# JMAP plumbing
# ---------------------------------------------------------------------------

def http(method, url, body=None, ctype="application/json", auth=None):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": auth or USER_AUTH, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} -> HTTP {e.code}: {e.read()[:500]!r}")

def session():
    return http("GET", f"{BASE}/jmap/session")

def call(using, *calls, auth=None):
    out = http("POST", f"{BASE}/jmap", json.dumps({"using": using, "methodCalls": list(calls)}).encode(),
               auth=auth)
    for name, args, cid in out["methodResponses"]:
        if name == "error":
            raise SystemExit(f"JMAP error in {cid}: {json.dumps(args)}")
        for bad in ("notCreated", "notUpdated", "notDestroyed"):
            if args.get(bad):
                raise SystemExit(f"JMAP {bad} in {cid}: {json.dumps(args[bad])}")
    return {cid: args for _, args, cid in out["methodResponses"]}

def account(sess, cap="urn:ietf:params:jmap:mail"):
    return sess["primaryAccounts"][cap]

def query(kind, acc, flt, sort=None):
    method = "Email/query" if kind == "mail" else "CalendarEvent/query"
    args = {"accountId": acc, "filter": flt, "limit": 100}
    if kind == "mail":
        args["collapseThreads"] = False
    if sort:
        args["sort"] = sort
    return call(MAIL if kind == "mail" else CAL, [method, args, "q"])["q"]["ids"]

def keyed(kind, acc):
    """id -> corpus key, recovered from the marker every seeded record carries."""
    if kind == "mail":
        ids = query("mail", acc, {"text": MARKER})
        got = call(MAIL, ["Email/get", {"accountId": acc, "ids": ids, "properties": ["subject"]}, "g"])["g"]["list"]
        by_subject = {v[2]: k for k, v in MESSAGES.items()}
        return {e["id"]: by_subject[e["subject"]] for e in got}
    ids = query("cal", acc, {"description": MARKER})
    got = call(CAL, ["CalendarEvent/get", {"accountId": acc, "ids": ids, "properties": ["title"]}, "g"])["g"]["list"]
    by_title = {v["title"]: k for k, v in EVENTS.items()}
    return {e["id"]: by_title[e["title"]] for e in got}

def wait_for(what, predicate, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise SystemExit(f"timed out after {timeout}s waiting for {what}")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def create_account():
    domain = call(ADMIN, ["x:Domain/set", {"create": {"d": {"name": DOMAIN}}}, "d"], auth=ADMIN_AUTH)["d"]["created"]["d"]["id"]
    # Registry lists travel as JSON objects keyed "0", "1", …
    call(ADMIN, ["x:Account/set", {"create": {"a": {
        "@type": "User", "name": USER.split("@")[0], "domainId": domain,
        "credentials": {"0": {"@type": "Password", "secret": os.environ["HARNESS_USER_PASSWORD"]}},
    }}}, "a"], auth=ADMIN_AUTH)

def seed():
    create_account()
    sess = session()
    acc = account(sess)
    inbox = next(m["id"] for m in call(MAIL, ["Mailbox/get", {"accountId": acc, "properties": ["role"]}, "m"])["m"]["list"]
                 if m.get("role") == "inbox")
    upload = sess["uploadUrl"].replace("{accountId}", acc)
    upload = BASE + upload[upload.index("/jmap/"):] if "/jmap/" in upload else upload
    imports = {}
    for key, (frm, to, subject, body) in MESSAGES.items():
        raw = (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n"
               f"Message-ID: <{key.lower()}@harness.example>\r\nDate: Tue, 01 Sep 2026 10:00:00 +0000\r\n"
               f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n{MARKER}\r\n").encode()
        blob = http("POST", upload, raw, "message/rfc822")["blobId"]
        imports[key] = {"blobId": blob, "mailboxIds": {inbox: True}, "keywords": {"$seen": True}}
    call(MAIL, ["Email/import", {"accountId": acc, "emails": imports}, "i"])

    cal_acc = account(sess, "urn:ietf:params:jmap:calendars")
    calendar = call(CAL, ["Calendar/get", {"accountId": cal_acc, "properties": ["id"]}, "c"])["c"]["list"][0]["id"]
    create = {}
    for key, ev in EVENTS.items():
        create[key] = {"@type": "Event", "calendarIds": {calendar: True}, "start": "2026-12-01T10:00:00",
                       "duration": "PT1H", "timeZone": "Etc/UTC", "description": MARKER, **ev}
    call(CAL, ["CalendarEvent/set", {"accountId": cal_acc, "create": create}, "e"])

    wait_for("the mail index", lambda: len(query("mail", acc, {"text": MARKER})) == len(MESSAGES))
    wait_for("the calendar index", lambda: len(query("cal", cal_acc, {"description": MARKER})) == len(EVENTS))
    print(f"seeded {len(MESSAGES)} messages and {len(EVENTS)} events; all indexed")

def check(mode, quiet=False):
    sess = session()
    accs = {"mail": account(sess), "cal": account(sess, "urn:ietf:params:jmap:calendars")}
    names = {k: keyed(k, a) for k, a in accs.items()}
    failures = 0
    for name, kind, flt, expect in PROBES:
        want = expect.get(mode, expect["stock"]) if mode == "patched-stale" else expect[mode]
        got = {names[kind].get(i, f"?{i}") for i in query(kind, accs[kind], flt)}
        ok = got == want
        failures += not ok
        if not quiet:
            print(f"{'PASS' if ok else 'FAIL'}  {name:<44} want {sorted(want) or '[]'}  got {sorted(got) or '[]'}")
    order = [names["mail"][i] for i in query("mail", accs["mail"], {"text": MARKER},
                                               [{"property": "subject", "isAscending": True}])]
    ok = order == SORT_EXPECTED
    failures += not ok
    if not quiet:
        print(f"{'PASS' if ok else 'FAIL'}  {'sort by subject keeps thread-name order':<44} want {SORT_EXPECTED}  got {order}")
        print(f"\n{mode}: {len(PROBES) + 1 - failures} of {len(PROBES) + 1} as expected")
    return failures

def reindex():
    call(ADMIN, ["x:Task/set", {"create": {"r": {"@type": "StoreMaintenance",
                                                 "maintenanceType": "reindexAccounts"}}}, "t"], auth=ADMIN_AUTH)
    # The task queue is never empty (Stalwart keeps its recurring maintenance tasks queued), so
    # "the queue drained" is not a signal, and neither is the first rewritten row: the reindex
    # works through the store row by row, and the first visible one says nothing about the rest.
    # So wait until every patched answer holds, polling quietly; `check` then prints them once.
    # A patch that never produces them fails here, by timeout, naming what it waited for.
    wait_for("every patched answer to hold after the reindex", lambda: check("patched", quiet=True) == 0, 600)
    print("reindex queued, and every row it rewrote is searchable")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        seed()
    elif cmd == "check" and len(sys.argv) == 4 and sys.argv[2] == "--expect" \
            and sys.argv[3] in ("stock", "patched-stale", "patched"):
        sys.exit(1 if check(sys.argv[3]) else 0)
    elif cmd == "reindex":
        reindex()
    else:
        raise SystemExit(__doc__)
