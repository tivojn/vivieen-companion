"""Her hands on the owner's Apple Mail - through the Mac, where Mail
actually lives.

iOS offers NO mailbox API at all (an app may only present a compose
sheet), so the phone road is closed by Apple, not by us. The Mac's
Mail.app is fully scriptable instead, and the brain already answers
from the Mac in every coupled conversation - so mail becomes a
Mac-side tool family: the model decides, osascript executes, and the
result rides back into the turn. From the phone this works whenever
the Mac is in reach; solo says honestly that mail needs the Mac.

First use pops macOS's one-time Automation consent ("control Mail").

Every argument is quoted through _quote() before it touches a script:
an address or subject with a double quote in it must land in Mail as
text, never as AppleScript.
"""
import json
import subprocess

TIMEOUT_S = 60


def _quote(value):
    """A Python string as a safe AppleScript string literal."""
    text = str(value or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(script):
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Mail did not answer in {TIMEOUT_S}s - it may be launching, "
            "syncing a large mailbox, or waiting on the one-time "
            "Automation consent dialog on the Mac's screen")
    if done.returncode:
        error = (done.stderr or "").strip()
        if "-1743" in error or "not allowed" in error.lower():
            raise RuntimeError(
                "macOS blocked Mail automation - System Settings > Privacy "
                "& Security > Automation > allow Vivieen (or the terminal "
                "that launched it) to control Mail, then ask again")
        raise RuntimeError(error[:200] or "Mail did not answer")
    return done.stdout.rstrip("\n")


# One record per line, unit-separated: message content can hold anything
# EXCEPT control characters, so US (31) fields / RS (30) lines survive
# subjects with commas, quotes, and embedded newlines. AppleScript has no
# \\x escapes - the separators are built with `character id`.
_SEP = ('set us to character id 31\n'
        'set rs to character id 30\n')
_ROW = ('(id of m as string) & us & (subject of m as string) & us & '
        '(sender of m as string) & us & (date received of m as string) & '
        'us & (read status of m as string)')


def _rows(raw):
    out = []
    for line in raw.split("\x1e"):
        parts = line.split("\x1f")
        if len(parts) == 5:
            out.append({"id": parts[0], "subject": parts[1],
                        "from": parts[2], "date": parts[3],
                        "read": parts[4] == "true"})
    return out


def _listing(selector, count):
    return _run(
        _SEP +
        'tell application "Mail"\n'
        f'  set found to {selector}\n'
        '  set total to count of found\n'
        f'  if total > {count} then set found to items 1 thru {count} '
        'of found\n'
        '  set out to ""\n'
        '  repeat with m in found\n'
        f'    set out to out & {_ROW} & rs\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')


def run(tool, args):
    """One mail tool -> a JSON-able dict. Raises with a nameable reason."""
    count = min(max(int(args.get("count") or 10), 1), 25)

    if tool == "mail_list":
        selector = ("(messages of inbox whose read status is false)"
                    if args.get("unread") else "messages of inbox")
        return {"messages": _rows(_listing(selector, count))}

    if tool == "mail_search":
        query = str(args.get("query") or "")
        if not query:
            raise ValueError("mail_search needs a query")
        selector = ("(messages of inbox whose subject contains "
                    f"{_quote(query)} or sender contains {_quote(query)})")
        return {"messages": _rows(_listing(selector, count))}

    if tool == "mail_read":
        mid = str(args.get("id") or "")
        if not mid.isdigit():
            raise ValueError("mail_read needs a message id from a list")
        raw = _run(
            _SEP +
            'tell application "Mail"\n'
            f'  set m to first message of inbox whose id is {int(mid)}\n'
            '  set read status of m to true\n'
            f'  return {_ROW} & rs & (content of m as string)\n'
            'end tell')
        head, _, body = raw.partition("\x1e")
        row = _rows(head + "\x1e")
        return {"message": row[0] if row else {}, "body": body[:4000]}

    if tool == "mail_send":
        to = str(args.get("to") or "")
        subject = str(args.get("subject") or "")
        body = str(args.get("body") or "")
        if not to or "@" not in to:
            raise ValueError("mail_send needs a real address in 'to'")
        if not subject and not body:
            raise ValueError("mail_send needs a subject or a body")
        recipients = "".join(
            '  tell m to make new to recipient at end of to recipients '
            f'with properties {{address:{_quote(a.strip())}}}\n'
            for a in to.split(",") if a.strip())
        _run(
            'tell application "Mail"\n'
            '  set m to make new outgoing message with properties '
            f'{{subject:{_quote(subject)}, content:{_quote(body)}, '
            'visible:false}\n'
            + recipients +
            '  send m\n'
            'end tell')
        return {"sent": True, "to": to, "subject": subject}

    if tool == "mail_delete":
        mid = str(args.get("id") or "")
        if not mid.isdigit():
            raise ValueError("mail_delete needs a message id from a list")
        subject = _run(
            'tell application "Mail"\n'
            f'  set m to first message of inbox whose id is {int(mid)}\n'
            '  set kept to subject of m\n'
            '  delete m\n'
            '  return kept\n'
            'end tell')
        return {"deleted": True, "subject": subject,
                "note": "moved to Trash, not erased"}

    if tool == "mail_unread_count":
        # The direct property, not a `whose` scan - counting a big inbox
        # message-by-message over Apple events measures in minutes.
        raw = _run('tell application "Mail" to return '
                   '(unread count of inbox) as string')
        return {"unread": int(raw or 0)}

    raise ValueError(f"no such mail tool: {tool}")


TOOLS_PROMPT = (
    "\n\nYou can also work the owner's Apple Mail through this Mac:\n"
    '<<viv:mail mail_list {"count":10,"unread":false}>>\n'
    '<<viv:mail mail_search {"query":"invoice","count":10}>>\n'
    '<<viv:mail mail_read {"id":"12345"}>>\n'
    '<<viv:mail mail_send {"to":"a@b.com","subject":"...","body":"..."}>>\n'
    '<<viv:mail mail_delete {"id":"12345"}>>\n'
    '<<viv:mail mail_unread_count {}>>\n'
    "Ids come from list/search results. Use these only when the owner "
    "asks about their email, and never SEND or DELETE unless the owner "
    "explicitly asked for that exact action this conversation - when in "
    "doubt, show what you would send and ask first. One directive per "
    "reply, on its own line, ending the reply; the result comes back "
    "to you.")


def _self_test():
    assert _quote('a"b\\c') == '"a\\"b\\\\c"'


_self_test()
