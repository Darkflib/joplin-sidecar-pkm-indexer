"""Turning a title candidate into a proposed title (docs/enrichment.md §5, §11.1).

Two paths, and the cheap one comes first.

**Slug.** A bookmark's URL usually carries its own title
(``/install-fail2ban-to-protect-ssh-on-centos`` → *Install Fail2ban to Protect
SSH on CentOS*). Offline, deterministic, free — and it is the only path that
works on a **dead** link, which measurement says is roughly half of a
seven-year-old bookmark set. Four of those domains no longer resolve at all, so
for those notes the URL is the only title source that will ever exist.

**Model.** Everything with prose to work from. Output is post-processed rather
than trusted: measured on a real host, the 1B/1.5B models ignored "no quotes" in
3 of 5 cases, and models like to prefix "Title:".

Nothing here writes to Joplin, and nothing decides *whether* to generate —
candidate selection is :mod:`title_candidates`, and skipping the ungeneratable
is the worker's job.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# Bumped when a change to the prompt or the cleaning rules should invalidate
# cached output. It is part of the suggestion identity, so a bump regenerates
# rather than colliding.
PROMPT_VERSION = 1

PROMPT_TEMPLATE = """Give this note a short, specific title.

Rules: 3 to 8 words. No quotes, no trailing full stop. Describe the actual
subject, not the document type. Reply with the title only.

Note:
{body}"""

# Long notes cost prompt-evaluation time and add little: the subject is almost
# always near the top, with the conclusion at the end. Keeping both ends bounds
# the cost without losing the shape of the note.
BODY_HEAD_CHARS = 1500
BODY_TAIL_CHARS = 500

MAX_TITLE_WORDS = 14
MAX_TITLE_CHARS = 120

_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),  # smart double quotes
    ("\u2018", "\u2019"),  # smart single quotes
    ("`", "`"),
)
_LEADING_LABEL_RE = re.compile(
    "^(title|suggested title|note title)\\s*[:\\-\u2013]\\s*", re.IGNORECASE
)
_TRAILING_PUNCT = ".!;:,"

# --- slug path -------------------------------------------------------------

# Path segments that carry no meaning of their own.
_NOISE_SEGMENTS = frozenset(
    {
        "index",
        "default",
        "home",
        "page",
        "pages",
        "article",
        "articles",
        "post",
        "posts",
        "news",
        "blog",
        "content",
        "view",
        "amp",
        "en",
        "en-us",
        "en-gb",
        "uk",
        "us",
        "www",
    }
)
# Query parameters that carry a human-readable subject. §11.6: a surviving
# share.google link resolves to a search URL whose `q=` *is* the title.
_TITLE_QUERY_KEYS = ("q", "query", "title", "search", "s")

# Lower-cased inside a title unless they lead it.
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "vs",
        "via",
        "with",
    }
)
# Tokens that should stay upper-case; without this "ssh" becomes "Ssh".
_ACRONYMS = frozenset(
    {
        "ssh",
        "api",
        "aws",
        "gcp",
        "php",
        "css",
        "html",
        "sql",
        "pdf",
        "cpu",
        "gpu",
        "ui",
        "ux",
        "cli",
        "http",
        "https",
        "dns",
        "tls",
        "ssl",
        "vpn",
        "os",
        "io",
        "id",
        "json",
        "yaml",
        "xml",
        "rss",
        "ide",
        "vm",
        "vms",
        "ram",
        "hdd",
        "ssd",
        "usb",
        "ip",
        "nas",
        "lan",
        "wan",
        "rest",
        "grpc",
        "jwt",
        "oidc",
        "rhel",
        "gnu",
        "arm",
        "x86",
    }
)
_ID_LIKE_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{12,}$")
_HEX_LIKE_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
_EXTENSION_RE = re.compile(r"\.(html?|php|aspx?|jsp|shtml?|cgi|json|xml|txt)$", re.IGNORECASE)
# A URL whose query carries a credential must not be parsed for a title: the
# useful words are not there anyway, and it keeps secrets out of suggestion rows.
_CREDENTIAL_QUERY_RE = re.compile(
    r"[?&](key|token|api_?key|secret|password|access_token|auth)=", re.IGNORECASE
)
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _looks_like_id(token: str) -> bool:
    return bool(_ID_LIKE_RE.match(token) or _HEX_LIKE_RE.match(token))


def _titlecase(words: list[str]) -> str:
    out: list[str] = []
    for i, word in enumerate(words):
        low = word.lower()
        if low in _ACRONYMS:
            out.append(low.upper())
        elif low.endswith("s") and low[:-1] in _ACRONYMS:
            out.append(low[:-1].upper() + "s")  # "apis" -> "APIs", not "APIS"
        elif not word.islower():
            out.append(word)  # already CamelCase or ALLCAPS: leave it alone
        elif i > 0 and low in _STOP_WORDS:
            out.append(low)
        else:
            out.append(low[:1].upper() + low[1:])
    return " ".join(out)


def _words_from(text: str) -> list[str]:
    parts = [p for p in re.split(r"[-_+.\s]+", unquote(text)) if p]
    return [p for p in parts if not _looks_like_id(p) and not p.isdigit()]


def best_url(title: str, body: str = "") -> str | None:
    """The fullest form of a bookmark's URL.

    Joplin truncates an auto-derived title at 80 characters, so a URL-titled note
    very often holds a *cut* URL in its title and the whole one in its body.
    Parsing the title directly produced fragments — "…How to Maintain Intense
    Motiva", "…Tri Audio and S" — across a real vault. Prefer the body's copy
    whenever it extends the title's.
    """
    from_title = title.strip() if _URL_IN_TEXT_RE.match(title.strip()) else None
    match = _URL_IN_TEXT_RE.search(body or "")
    from_body = match.group(0).rstrip(").,") if match else None
    if from_title and from_body:
        return (
            from_body
            if from_body.startswith(from_title[:40]) and len(from_body) >= len(from_title)
            else from_title
        )
    return from_body or from_title


def slug_title(url: str) -> str | None:
    """Derive a title from a URL's path or query, or None if it carries none.

    Returns None rather than guessing: an opaque id (``share.google/crvtpycs…``)
    has no title in it, and inventing one would be worse than leaving the note
    alone for a human.
    """
    url = url.strip()
    if _CREDENTIAL_QUERY_RE.search(url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None

    # A search-style URL states its subject in the query string (§11.6).
    query = parse_qs(parsed.query)
    for key in _TITLE_QUERY_KEYS:
        for raw in query.get(key, []):
            words = _words_from(raw)
            if len(words) >= 2:
                return _titlecase(words)[:MAX_TITLE_CHARS]

    segments = [s for s in unquote(parsed.path).split("/") if s]
    for segment in reversed(segments):
        cleaned = _EXTENSION_RE.sub("", segment)
        if cleaned.lower() in _NOISE_SEGMENTS:
            continue
        words = _words_from(cleaned)
        if len(words) >= 2:
            return _titlecase(words)[:MAX_TITLE_CHARS]
    return None


# --- model path ------------------------------------------------------------


def truncate_body(body: str) -> str:
    """Head plus tail, so a long note costs bounded prompt evaluation."""
    body = body.strip()
    if len(body) <= BODY_HEAD_CHARS + BODY_TAIL_CHARS:
        return body
    return f"{body[:BODY_HEAD_CHARS]}\n…\n{body[-BODY_TAIL_CHARS:]}"


def build_prompt(body: str) -> str:
    return PROMPT_TEMPLATE.format(body=truncate_body(body))


def clean_title(raw: str, *, current_title: str = "") -> str | None:
    """Normalise a model's answer, or None if it is unusable.

    The instruction "no quotes" was ignored in 3 of 5 measured cases by the
    smaller models, so this is enforcement rather than tidying. Returning None
    for an unusable answer keeps a bad suggestion out of the review queue
    entirely, which matters more than salvaging it.
    """
    title = raw.strip()
    if not title:
        return None
    # Models sometimes answer over several lines; the title is the first.
    title = title.split("\n", 1)[0].strip()
    title = _LEADING_LABEL_RE.sub("", title).strip()
    for opening, closing in _QUOTE_PAIRS:
        if len(title) >= 2 and title.startswith(opening) and title.endswith(closing):
            title = title[1:-1].strip()
            break
    title = title.rstrip(_TRAILING_PUNCT).strip()
    title = re.sub(r"\s+", " ", title)

    if not title or len(title) > MAX_TITLE_CHARS:
        return None
    if len(title.split()) > MAX_TITLE_WORDS:
        return None
    if current_title and title.casefold() == current_title.strip().casefold():
        return None  # proposing the title it already has is noise
    return title


# --- routing ---------------------------------------------------------------


def propose_title(*, issue: str, title: str, body: str) -> tuple[str | None, str]:
    """Try the offline path; return ``(title_or_None, source)``.

    ``source`` is ``"slug"`` when the URL answered, otherwise ``"model"`` to say
    the caller must ask the model. Kept separate from the model call so the
    decision is testable without a network, and so the slug path keeps working
    for the dead links a fetch could never help with.
    """
    if issue == "url":
        url = best_url(title, body)
        if url:
            derived = slug_title(url)
            if derived:
                cleaned = clean_title(derived, current_title=title)
                if cleaned:
                    return cleaned, "slug"
    return None, "model"
