#!/usr/bin/env python3
"""Trusted bot identities for branch-name exemptions (AGENTS.md Rule 14).

Trust comes only from GitHub pull request event metadata. GitHub operates
first-party bots, so their slugs cannot be re-registered and their login alone
identifies them. A third-party App slug can be re-registered after deletion, so
a third-party bot also needs its immutable numeric account ID in the event.

A bot noreply commit identity is valid only in that bot's own pull request.
Any other pull request that carries one is impersonating the bot.

Changing this registry requires the `gate-change-approved` label.
"""
import json
import re
from pathlib import Path
from typing import NamedTuple

MAX_EVENT_BYTES = 1024 * 1024
BOT_NOREPLY_SUFFIX = "[bot]@users.noreply.github.com"
BOT_NOREPLY_PATTERN = re.compile(
    r"\A(?P<account_id>[0-9]+)\+(?P<login>[a-z0-9-]+\[bot\])"
    r"@users\.noreply\.github\.com\Z"
)


class TrustedBot(NamedTuple):
    """One bot allowed to skip branch naming on its own branch prefix."""

    login: str
    branch_prefix: str
    account_id: int | None  # None only for GitHub first-party bots


BRANCH_EXEMPT_BOTS = {
    bot.login: bot
    for bot in (
        TrustedBot("dependabot[bot]", "dependabot/", None),
        TrustedBot("code-coverage-agent[bot]", "code-coverage-agent/", 295130552),
    )
}
EXEMPT_BRANCH_PREFIXES = tuple(bot.branch_prefix for bot in BRANCH_EXEMPT_BOTS.values())


class EventAuthor(NamedTuple):
    """Pull request author fields taken from the GitHub event payload."""

    login: str
    account_id: int
    head_ref: str


class EventPayloadError(ValueError):
    """The pull request event file cannot supply author metadata."""


def match_exempt_bot(author: str, branch: str) -> TrustedBot | None:
    """Return the registered bot when `author` owns `branch` by prefix."""
    bot = BRANCH_EXEMPT_BOTS.get(author)
    if bot is None or not branch.startswith(bot.branch_prefix):
        return None
    return bot


def event_verifies(bot: TrustedBot, branch: str, event: EventAuthor) -> bool:
    """Return whether the event proves the bot ID and the checked branch."""
    return (event.account_id, event.head_ref) == (bot.account_id, branch)


def _read_event(path: Path) -> dict:
    """Return the bounded JSON object stored at `path`."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES + 1)
    except OSError as error:
        raise EventPayloadError(f"event file unreadable ({type(error).__name__})") from error
    if len(raw) > MAX_EVENT_BYTES:
        raise EventPayloadError(f"event file exceeds {MAX_EVENT_BYTES} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EventPayloadError(f"event file unreadable ({type(error).__name__})") from error
    if not isinstance(payload, dict):
        raise EventPayloadError("event payload is not an object")
    return payload


def load_event_author(path: str) -> EventAuthor | None:
    """Return the pull request author, or None when no event path exists.

    Error messages name the failing field only. Payload values can hold pull
    request text or personal data, so they never reach a message.
    """
    if not path:
        return None
    pull_request = _read_event(Path(path)).get("pull_request")
    if not isinstance(pull_request, dict):
        raise EventPayloadError("event has no pull_request object")
    user = pull_request.get("user")
    head = pull_request.get("head")
    if not isinstance(user, dict) or not isinstance(head, dict):
        raise EventPayloadError("pull_request lacks user or head")
    login = user.get("login")
    head_ref = head.get("ref")
    if not isinstance(login, str) or not isinstance(head_ref, str):
        raise EventPayloadError("pull_request user.login or head.ref is not text")
    account_id = user.get("id")
    # bool subclasses int, so an exact type check rejects `true`.
    if type(account_id) is not int:
        raise EventPayloadError("pull_request user.id is not an integer")
    return EventAuthor(login, account_id, head_ref)


class LazyEventAuthor:
    """Load the event on first use only, and keep a load error for reporting."""

    def __init__(self, path: str):
        self._path = path
        self._loaded = False
        self._author = None
        self.error = None

    def get(self) -> EventAuthor | None:
        """Return the cached event author, loading it on the first call."""
        if not self._loaded:
            self._loaded = True
            try:
                self._author = load_event_author(self._path)
            except EventPayloadError as error:
                # Callers report `error`; a failed load never grants an exemption.
                self.error = error
        return self._author


def _expected_bot_id(author: str, events: LazyEventAuthor) -> int | None:
    """Return the account ID that the pull request author must carry."""
    event = events.get()
    if event is not None:
        return event.account_id
    bot = BRANCH_EXEMPT_BOTS.get(author)
    return None if bot is None else bot.account_id


def _bot_email_problem(email: str, author: str, events: LazyEventAuthor) -> str | None:
    """Return why a bot-suffixed email is invalid for this pull request."""
    match = BOT_NOREPLY_PATTERN.fullmatch(email)
    if match is None:
        return f"'{email}' names a bot without the numbered <id>+<login>[bot] form"
    login = match.group("login")
    if login != author.lower():
        return (f"'{email}' names bot {login}, but the pull request author is "
                f"{author}; commit under your own noreply identity")
    expected = _expected_bot_id(author, events)
    if expected is not None and int(match.group("account_id")) != expected:
        return f"'{email}' account ID does not match pull request author {author}"
    return None


def bot_commit_violations(identities: list[dict], author: str,
                          events: LazyEventAuthor) -> list[str]:
    """Reject bot noreply commit identities that do not belong to the author."""
    verdicts: dict[str, str | None] = {}
    violations = []
    for identity in identities:
        for role in ("author", "committer"):
            email = identity[f"{role}_email"].strip().lower()
            if not email.endswith(BOT_NOREPLY_SUFFIX):
                continue
            if email not in verdicts:
                verdicts[email] = _bot_email_problem(email, author, events)
            problem = verdicts[email]
            if problem:
                violations.append(f"{identity['label']}: {role} email {problem}")
    return violations
