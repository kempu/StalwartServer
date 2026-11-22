#!/usr/bin/env python3
"""The sharing probe: whether a person a calendar or address book is shared with can choose
whether it is subscribed, and what it reads as before they have chosen.

A client decides from `isSubscribed` alone whether a calendar, address book or mailbox someone
shared is in that person's app, so "Remove" is an unsubscribe and "Add" a subscribe. That rests
on claims about this server that are measured here, never read from source:

  1. Subscribing and unsubscribing a shared mailbox, calendar or address book works for a
     person who may only read it ("Can view"). Upstream allows it for a mailbox and refuses it
     for a calendar or an address book unless the person may also modify the collection
     itself, so a view-only share could never be removed (patch 0007).
  2. Every other change to a collection still needs that right: a rename, a colour, a change
     to who it is shared with, and a subscription sent together with any of them.
  3. A calendar somebody shares with you reads as NOT subscribed until you subscribe it, as a
     shared mailbox and a shared address book already do. Upstream answers with the owner's
     own setting, so every calendar a person is given, including one added later to an account
     they had removed, joins their app on its own (patch 0007).
  4. One person's choice is theirs alone: it does not change the owner's, nor another
     person's, and it survives the owner sharing the calendar with someone else.
  5. The owner's own collections keep reading as they did.

Every refusal is paired with the same change succeeding for someone who may make it, so a
refusal measures the rule, not a broken request. Run through `run.sh sharing EXPECT [IMAGE]`,
EXPECT being `stock` or `patched`.

Standard library only, and every name and address in it is invented.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe as p  # noqa: E402  (the search harness's JMAP plumbing)

CAL = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]
BOOK = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:contacts"]
MAIL = p.MAIL
USING = {"Calendar": CAL, "AddressBook": BOOK, "Mailbox": MAIL}

PEOPLE = ["owner", "viewer", "editor"]
READ = {"Calendar": {"mayReadFreeBusy": True, "mayReadItems": True},
        "AddressBook": {"mayRead": True},
        "Mailbox": {"mayReadItems": True}}
WRITE = {"Calendar": {"mayReadFreeBusy": True, "mayReadItems": True, "mayWriteAll": True},
         "AddressBook": {"mayRead": True, "mayWrite": True},
         "Mailbox": {"mayReadItems": True, "mayAddItems": True, "mayRemoveItems": True,
                     "mayRename": True, "maySetSeen": True, "maySetKeywords": True}}

FAILURES = []


def expect(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        FAILURES.append(what)


def auth(who):
    return p.basic(f"{who}@{p.DOMAIN}", os.environ["HARNESS_USER_PASSWORD"])


def create_people():
    domain = p.call(p.ADMIN, ["x:Domain/set", {"create": {"d": {"name": p.DOMAIN}}}, "d"],
                    auth=p.ADMIN_AUTH)["d"]["created"]["d"]["id"]
    for who in PEOPLE:
        p.call(p.ADMIN, ["x:Account/set", {"create": {"a": {
            "@type": "User", "name": who, "domainId": domain,
            "credentials": {"0": {"@type": "Password", "secret": os.environ["HARNESS_USER_PASSWORD"]}},
        }}}, "a"], auth=p.ADMIN_AUTH)


def own_account(who):
    sess = p.http("GET", f"{p.BASE}/jmap/session", auth=auth(who))
    return sess["primaryAccounts"]["urn:ietf:params:jmap:mail"]


def raw(who, using, method, args):
    """One call, returned whole: notUpdated is an answer here, not an error."""
    out = p.http("POST", f"{p.BASE}/jmap", json.dumps(
        {"using": using, "methodCalls": [[method, args, "c"]]}).encode(), auth=auth(who))
    name, body, _ = out["methodResponses"][0]
    if name == "error":
        raise SystemExit(f"{who} {method}: JMAP error {json.dumps(body)}")
    return body


def get(who, kind, acc, cid, prop="isSubscribed"):
    got = raw(who, USING[kind], f"{kind}/get", {"accountId": acc, "ids": [cid], "properties": [prop]})
    if not got["list"]:
        raise SystemExit(f"{who} cannot see {kind} {cid} in {acc}: {json.dumps(got)}")
    return got["list"][0].get(prop)


def update(who, kind, acc, cid, patch):
    """True when the server took the update, else the SetError type."""
    got = raw(who, USING[kind], f"{kind}/set", {"accountId": acc, "update": {cid: patch}})
    if cid in (got.get("updated") or {}):
        return True
    return (got.get("notUpdated") or {}).get(cid, {}).get("type", json.dumps(got))


def share(kind, acc, cid, grants):
    patch = {f"shareWith/{pid}": rights for pid, rights in grants.items()}
    took = update("owner", kind, acc, cid, patch)
    if took is not True:
        raise SystemExit(f"the owner could not share {kind} {cid}: {took}")


def main(mode):
    create_people()
    ids = {who: own_account(who) for who in PEOPLE}
    owner = ids["owner"]

    cal = raw("owner", CAL, "Calendar/get", {"accountId": owner, "properties": ["id", "name"]})["list"][0]["id"]
    book = raw("owner", BOOK, "AddressBook/get", {"accountId": owner, "properties": ["id"]})["list"][0]["id"]
    box = p.call(MAIL, ["Mailbox/set", {"accountId": owner, "create": {"b": {"name": "Projects"}}}, "b"],
                 auth=auth("owner"))["b"]["created"]["b"]["id"]
    owner_cal_before = get("owner", "Calendar", owner, cal)
    collections = {"Calendar": cal, "AddressBook": book, "Mailbox": box}
    for kind, cid in collections.items():
        share(kind, owner, cid, {ids["viewer"]: READ[kind], ids["editor"]: WRITE[kind]})

    patched = mode == "patched"
    # 3. Before anyone has chosen.
    expect(get("viewer", "Calendar", owner, cal) is (not patched),
           f"a calendar shared with a viewer reads as {'NOT ' if patched else ''}subscribed before they choose")
    expect(get("viewer", "AddressBook", owner, book) is False,
           "an address book shared with a viewer reads as not subscribed before they choose")
    expect(get("viewer", "Mailbox", owner, box) is False,
           "a mailbox shared with a viewer reads as not subscribed before they choose")

    # 1. A viewer subscribes and unsubscribes each type; the mailbox is the baseline.
    for kind, cid in collections.items():
        want = True if (kind == "Mailbox" or patched) else "forbidden"
        for on in (True, False):
            took = update("viewer", kind, owner, cid, {"isSubscribed": on})
            expect(took == want, f"a viewer {'subscribes' if on else 'unsubscribes'} a shared {kind}: "
                                 f"want {want}, got {took}")
            if took is True:
                expect(get("viewer", kind, owner, cid) is on, f"  and it then reads {on}")
    # The editor may modify the collection, so upstream lets them choose too.
    for kind in ("Calendar", "AddressBook"):
        took = update("editor", kind, owner, collections[kind], {"isSubscribed": False})
        expect(took is True, f"an editor unsubscribes a shared {kind}: got {took}")

    # 2. Everything else still needs the right to modify the collection.
    for kind, cid in collections.items():
        name_prop = {"name": "Renamed by a viewer"}
        took = update("viewer", kind, owner, cid, name_prop)
        expect(took == "forbidden", f"a viewer renames a shared {kind}: want forbidden, got {took}")
        took = update("viewer", kind, owner, cid, {f"shareWith/{ids['viewer']}": WRITE[kind]})
        expect(took == "forbidden", f"a viewer widens their own grant on a shared {kind}: want forbidden, got {took}")
        took = update("viewer", kind, owner, cid, {"isSubscribed": True, **name_prop})
        expect(took == "forbidden", f"a viewer sends a subscription with a rename on a shared {kind}: "
                                    f"want forbidden, got {took}")
    took = update("viewer", "Calendar", owner, cal, {"isSubscribed": True, "color": "#000000"})
    expect(took == "forbidden", f"a viewer sends a subscription with a colour on a shared Calendar: want forbidden, got {took}")
    took = update("editor", "Calendar", owner, cal, {"name": "Team calendar"})
    expect(took is True, f"an editor renames the shared Calendar (the same change, allowed): got {took}")

    # 4. One person's choice is theirs alone.
    if patched:
        expect(update("viewer", "Calendar", owner, cal, {"isSubscribed": False}) is True, "the viewer removes the calendar")
        expect(update("editor", "Calendar", owner, cal, {"isSubscribed": True}) is True, "the editor adds it")
        expect(get("viewer", "Calendar", owner, cal) is False, "  the viewer's stays off")
        expect(get("editor", "Calendar", owner, cal) is True, "  the editor's is on")
    expect(get("owner", "Calendar", owner, cal) is owner_cal_before is True,
           "the owner's own calendar still reads subscribed for the owner")
    expect(get("owner", "AddressBook", owner, book) is True, "the owner's own address book still reads subscribed")

    # The owner shares the calendar with someone else: the viewer's choice survives.
    if patched:
        share("Calendar", owner, cal, {ids["editor"]: READ["Calendar"]})
        expect(get("viewer", "Calendar", owner, cal) is False,
               "the viewer's removal survives the owner changing who the calendar is shared with")

    # 3. A calendar the owner adds later, to an account the viewer had removed.
    later = p.call(CAL, ["Calendar/set", {"accountId": owner, "create": {"c": {"name": "Added later", "isSubscribed": True}}}, "c"],
                   auth=auth("owner"))["c"]["created"]["c"]["id"]
    share("Calendar", owner, later, {ids["viewer"]: READ["Calendar"]})
    owner_later = get("owner", "Calendar", owner, later)
    expect(get("viewer", "Calendar", owner, later) is (not patched and owner_later),
           f"a calendar shared later reads as {'NOT ' if patched else 'the owner reads it: '}subscribed for the viewer "
           f"(owner reads {owner_later})")

    print(f"\nsharing ({mode}): {'all as expected' if not FAILURES else f'{len(FAILURES)} differ'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("stock", "patched"):
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1]))
