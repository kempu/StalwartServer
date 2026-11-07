#!/usr/bin/env python3
"""The push probe: which deliveries a push subscription's emailPush filter lets through.

The mail app narrows its JMAP PushSubscription with an `emailPush` filter so that a message
a Sieve rule or the spam filter files into Archive, Junk, Trash, Sent or Drafts does not
wake the phone at all, and the push relay wakes on the `EmailPush` object the
server posts instead of a StateChange. Both rest on claims about this server that are
measured here, never read from source:

  1. Without emailPush, a delivery into Archive posts a StateChange carrying
     EmailDelivery. (The baseline: the filter is what silences it, not something else.)
  2. emailPush can be set by UPDATING a live subscription (the app refreshes the folder
     ids on every renewal, and installs that already hold a subscription get it that way).
  3. With `inMailboxOtherThan` [archive, junk, trash, sent, drafts], a delivery the
     account's Sieve script files into any of them posts NOTHING, and a delivery into the
     Inbox or a folder the user created posts an EmailPush.
  4. With no properties, which is what the app asks for, the post is exactly
     {"@type": "EmailPush", "accountId": <the account>, "emails": [{}], "state": <state>}
     — the relay's wire contract.
  5. emailPush is accepted in a CREATE beside `types`, which is how a new
     registration sends it.

Every negative is paired with a positive seen in the same run: a delivery that posts
nothing proves nothing on its own. Run through `run.sh push IMAGE`.

Standard library only, and every name and address in it is invented.
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe as p  # noqa: E402  (the search harness's JMAP plumbing and account)

CATCHER = f"http://127.0.0.1:{os.environ.get('HARNESS_CATCHER_PORT', '18081')}/posts"
SMTP_PORT = int(os.environ.get("HARNESS_SMTP_PORT", "12525"))
CORE = ["urn:ietf:params:jmap:core"]
SIEVE = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:sieve"]
SENDER = "alex.chen@sender.harness.example.org"
FILTERED_ROLES = ["archive", "junk", "trash", "sent", "drafts"]
# How long a delivery is given to post before its silence counts. Generous: the server
# throttles and batches pushes, and a late post after the window would be misattributed.
QUIET = 20

FAILURES = []


def posts():
    with urllib.request.urlopen(CATCHER, timeout=10) as r:
        return json.loads(r.read())


def expect(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        FAILURES.append(what)


def deliver(subject):
    raw = (f"From: Alex Chen <{SENDER}>\r\nTo: {p.USER}\r\nSubject: {subject}\r\n"
           f"Message-ID: <{uuid.uuid4().hex}@sender.harness.example.org>\r\n"
           f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\n"
           "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           "A message the push harness delivered.\r\n").encode()
    with smtplib.SMTP("127.0.0.1", SMTP_PORT, timeout=60) as s:
        s.sendmail(SENDER, [p.USER], raw)


def after(subject, wait_for_post):
    """Deliver one message and return the posts it produced. With wait_for_post, return as
    soon as one arrives (then a short settle for stragglers); otherwise wait out QUIET."""
    before = len(posts())
    deliver(subject)
    deadline = time.time() + QUIET
    while time.time() < deadline:
        new = posts()[before:]
        if new and wait_for_post:
            time.sleep(3)
            return posts()[before:]
        time.sleep(1)
    return posts()[before:]


def where(acc, subject):
    """Which mailbox roles/names the delivered message landed in — proof the Sieve rule
    did what the case claims, so a silent post is about the filter, not a misfiling."""
    def found():
        q = p.call(p.MAIL, ["Email/query", {"accountId": acc, "filter": {"subject": subject}}, "q"])["q"]["ids"]
        return q
    p.wait_for(f"'{subject}' to be indexed", lambda: len(found()) == 1, timeout=60)
    got = p.call(p.MAIL, ["Email/get", {"accountId": acc, "ids": found(), "properties": ["mailboxIds"]}, "g"])["g"]["list"][0]
    return set(got["mailboxIds"])


def main():
    p.create_account()
    sess = p.session()
    acc = p.account(sess)

    # A new account has no Archive; a migrated one usually does. Made here with its role, as
    # a client or a migration would.
    p.call(p.MAIL, ["Mailbox/set", {"accountId": acc, "create": {"a": {"name": "Archive", "role": "archive"}}}, "a"])
    boxes = p.call(p.MAIL, ["Mailbox/get", {"accountId": acc, "properties": ["name", "role"]}, "m"])["m"]["list"]
    by_role = {b["role"]: b for b in boxes if b.get("role")}
    missing = [r for r in ["inbox"] + FILTERED_ROLES if r not in by_role]
    if missing:
        raise SystemExit(f"the account has no mailbox with role(s) {missing}: {json.dumps(boxes)}")
    projects = p.call(p.MAIL, ["Mailbox/set", {"accountId": acc, "create": {"p": {"name": "Projects"}}}, "s"])["s"]["created"]["p"]["id"]

    # The account's own Sieve script decides where each case lands, the way a user's rule
    # does in production. `$notjunk` keeps the Inbox cases out of Junk whatever the spam
    # filter scores them (patch 0003), so a Junk landing is only ever the [junk] case.
    script = 'require ["fileinto", "imap4flags"];\n'
    for tag, role in [("archive", "archive"), ("junk", "junk"), ("trash", "trash"), ("sent", "sent"), ("drafts", "drafts")]:
        script += f'if header :contains "subject" "[{tag}]" {{ fileinto "{by_role[role]["name"]}"; stop; }}\n'
    script += 'if header :contains "subject" "[projects]" { fileinto "Projects"; stop; }\naddflag "$notjunk";\n'
    upload = sess["uploadUrl"].replace("{accountId}", acc)
    upload = p.BASE + upload[upload.index("/jmap/"):]
    blob = p.http("POST", upload, script.encode(), "application/sieve")["blobId"]
    p.call(SIEVE, ["SieveScript/set", {"accountId": acc, "create": {"s": {"name": "push-harness", "blobId": blob}},
                                       "onSuccessActivateScript": "#s"}, "v"])

    expires = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = p.call(CORE, ["PushSubscription/set", {"create": {"sub": {
        "deviceClientId": "push-harness", "url": "https://push.harness.test/push/h1",
        "types": ["EmailDelivery"], "expires": expires}}}, "c"])["c"]["created"]["sub"]["id"]
    p.wait_for("the PushVerification post", lambda: any(x["body"].get("@type") == "PushVerification" for x in posts()), 60)
    code = next(x["body"]["verificationCode"] for x in posts() if x["body"].get("@type") == "PushVerification")
    p.call(CORE, ["PushSubscription/set", {"update": {created: {"verificationCode": code}}}, "u"])

    # 1. Baseline: no emailPush, a delivery into Archive posts a StateChange.
    got = after("baseline [archive]", True)
    expect(where(acc, "baseline [archive]") == {by_role["archive"]["id"]}, "baseline: the message landed in Archive")
    expect(any(x["body"].get("@type") == "StateChange"
               and "EmailDelivery" in x["body"].get("changed", {}).get(acc, {}) for x in got),
           f"baseline: Archive delivery without emailPush posts a StateChange with EmailDelivery ({json.dumps([x['body'] for x in got])})")

    # 2. emailPush set by UPDATE, with subjects so each post can be attributed.
    excluded = [by_role[r]["id"] for r in FILTERED_ROLES]
    def set_email_push(properties):
        p.call(CORE, ["PushSubscription/set", {"update": {created: {"emailPush": {
            acc: {"filter": {"inMailboxOtherThan": excluded}, "properties": properties}}}}}, "e"])
    set_email_push(["subject"])
    # emailPush is not among PushSubscription/get's default properties: it has to be named.
    read = p.call(CORE, ["PushSubscription/get", {"ids": [created], "properties": ["id", "emailPush"]}, "g"])["g"]["list"][0]
    expect(read.get("emailPush", {}).get(acc, {}).get("filter") == {"inMailboxOtherThan": excluded},
           f"update: emailPush reads back as written ({json.dumps(read.get('emailPush'))})")

    # 3. Positive controls first and last, the filtered roles in between.
    cases = [("inbox one [inbox]", "inbox", True), ("custom [projects]", None, True)]
    cases += [(f"filed [{r}]", r, False) for r in FILTERED_ROLES]
    cases += [("inbox two [inbox]", "inbox", True)]
    for subject, role, should_post in cases:
        got = after(subject, should_post)
        landed = where(acc, subject)
        expect(landed == ({by_role[role]["id"]} if role else {projects}), f"'{subject}' landed where the rule says")
        pushes = [x["body"] for x in got if x["body"].get("@type") == "EmailPush"]
        others = [x["body"] for x in got if x["body"].get("@type") != "EmailPush"]
        if should_post:
            expect(len(pushes) == 1 and [e.get("subject") for e in pushes[0].get("emails", [])] == [subject],
                   f"'{subject}' posts one EmailPush naming it ({json.dumps(pushes)})")
        else:
            expect(not pushes, f"'{subject}' posts no EmailPush ({json.dumps(pushes)})")
        expect(not others, f"'{subject}' posts no StateChange beside it ({json.dumps(others)})")

    # 4. The shape the app asks for: no properties.
    set_email_push([])
    got = after("shape [inbox]", True)
    bodies = [x["body"] for x in got]
    expect(len(bodies) == 1 and bodies[0].get("@type") == "EmailPush" and bodies[0].get("accountId") == acc
           and bodies[0].get("emails") == [{}] and isinstance(bodies[0].get("state"), str),
           f"no properties: the post is {{@type: EmailPush, accountId, emails: [{{}}], state}} ({json.dumps(bodies)})")

    # 5. A new registration sends the filter IN the create, beside the types.
    second = p.call(CORE, ["PushSubscription/set", {"create": {"s2": {
        "deviceClientId": "push-harness-2", "url": "https://push.harness.test/push/h2",
        "types": ["EmailDelivery"], "expires": expires,
        "emailPush": {acc: {"filter": {"inMailboxOtherThan": excluded}, "properties": []}}}}}, "c2"])["c2"]["created"]["s2"]["id"]
    read = p.call(CORE, ["PushSubscription/get", {"ids": [second], "properties": ["id", "types", "emailPush"]}, "g"])["g"]["list"][0]
    expect(read.get("types") == ["EmailDelivery"]
           and read.get("emailPush", {}).get(acc, {}).get("filter") == {"inMailboxOtherThan": excluded},
           f"create: emailPush is accepted beside types ({json.dumps(read)})")

    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} push probe(s) failed")
    print("push probe: every case answered as expected")


if __name__ == "__main__":
    main()
