#!/usr/bin/env python3
"""TikTok giveaway finder.

Runs once a day, fetches recent TikTok posts through the Apify TikTok Scraper,
keeps the giveaways someone living in Germany can enter, estimates how many
people compete for each prize and sends the cheapest ones via Telegram and/or
e-mail. Entering the giveaways is left to the user - this script never
comments, follows or likes anything.

    python giveaway_finder.py                      # live run (needs APIFY_TOKEN)
    python giveaway_finder.py --dry-run            # print the list, send nothing
    python giveaway_finder.py --sample items.json  # use a saved Apify export
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_DIR = BASE_DIR / "state"
REPORT_PATH = BASE_DIR / "latest_report.md"

BERLIN = ZoneInfo("Europe/Berlin")
SEEN_RETENTION_DAYS = 60
TELEGRAM_CHUNK_LIMIT = 3900  # Telegram's hard limit is 4096
CAPTION_LENGTH = 90
APIFY_RUN_TIMEOUT = timedelta(minutes=20)

DEFAULT_CONFIG = {
    "apify_actor": "clockworks/tiktok-scraper",
    "hashtags": ["gewinnspiel", "verlosung", "gewinnspielalarm"],
    "search_queries": ["gewinnspiel", "verlosung", "giveaway deutschland"],
    "results_per_seed": 20,
    "proxy_country": "DE",
    "allowed_countries": ["DE"],
    # {"videoSearchSorting": "LATEST"} would be better, but as of October 2026 the actor
    # returns no posts with it (or with videoSearchDateFilter).
    "apify_input_extra": {},
    "max_cost_per_run_usd": 0.5,
    "monthly_budget_usd": None,
    "max_age_days": 14,
    "assumed_duration_days": 7,
    "comment_decay_hours": 36,
    "percentile_cutoff": 30,
    "hard_max_per_prize": None,
    "history_days": 30,
    "min_history_for_percentile": 10,
    "min_author_followers": 200,
    "max_list_length": 30,
    "only_new": False,
}

STAT_KEYS = ("fetched", "not_giveaway", "ended", "wrong_region", "too_old", "small_account", "active")

MISSING_TOKEN_MESSAGE = (
    "APIFY_TOKEN is not set. Find your token in the Apify Console under "
    "Settings > API & Integrations (https://console.apify.com/settings/integrations) "
    "and store it as the APIFY_TOKEN secret (GitHub: Settings > Secrets and variables > Actions) "
    "or export it in your shell. Use --sample FILE to test without Apify."
)


# --------------------------------------------------------------------------- #
# Text patterns
# --------------------------------------------------------------------------- #

GIVEAWAY_RE = re.compile(
    r"gewinnspiel|verlos|give[\s-]?away|zu gewinnen|gewinne|gewinnt|verschenk|\bwin\b",
    re.IGNORECASE,
)
ACTION_RE = re.compile(
    r"kommentier|komment|comment|\bfolg|follow|abonnier|markier|tagge|\btag\b|verlink|like"
    r"|\bteil(?:e|en|t)\b|share|teilnahme|teilnehmen|mitmachen|mach mit|\benter\b",
    re.IGNORECASE,
)
# Past tense only: "Winner announced Friday" is a future announcement, not an ended giveaway.
ENDED_RE = re.compile(
    r"beendet|ist vorbei"
    r"|gewinner(?:in)?\s+(?:steht|stehen)\s+fest"
    r"|gewinner(?:in)?\s+(?:ist|sind|wurde|wurden)\b"
    r"|winners?\s+(?:was|were|has been|have been)\b"
    r"|giveaway\s+(?:is\s+)?(?:closed|over|ended)"
    r"|abgelaufen|wurde\s+ausgelost|congrats\s+to|congratulations\s+to|herzlichen\s+glückwunsch\s+an\b",
    re.IGNORECASE,
)

SCAM_FLAGS = [
    (re.compile(r"link\s+in\s+(?:der\s+|meiner\s+)?bio", re.I), "asks you to click a bio link"),
    # Only when *you* are asked to write. "Der Gewinner wird per DM benachrichtigt" is in almost
    # every legitimate giveaway, and "dm" is also a drugstore chain ("dm-Gutschein").
    (re.compile(r"schreib(?:t|e)?\s+(?:mir|uns)\b|meldet?\s+(?:dich|euch)\s+(?:per|via|bei)\b"
                r"|(?:schick|send)(?:e|t)?\s+(?:mir|uns)\s+(?:eine?\s+)?(?:dm|(?:direkt|privat)?nachricht)"
                r"|(?:\bdm|direktnachricht|privatnachricht)\s+an\s+(?:mich|uns)\b|\bdm\s+(?:me|us)\b"
                r"|send\s+(?:me|us)\s+an?\s+(?:dm|message)", re.I), "asks you to DM"),
    (re.compile(r"whatsapp|telegram", re.I), "moves you to WhatsApp/Telegram"),
    (
        re.compile(r"versandkosten|\b(?:be)?zahl\w*(?:\s+\S+){0,3}?\s+versand|gebühr|\bfees?\b", re.I),
        "mentions fees/shipping costs",
    ),
    (re.compile(r"bitcoin|krypto|crypto|gift\s?cards?|gutscheincode", re.I), "crypto / gift-card wording"),
]

WEEKDAYS = (
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|wed|thu|fri|sat|sun|mo|di|mi|do|fr|sa|so"
)
MONTHS = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
          "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
DEADLINE_RE = re.compile(
    r"\b(?:läuft\s+bis|bis|endet|gewinnspielende|aktionsende|ende|teilnahmeschluss|einsendeschluss"
    r"|deadline|until|ends?)\b"
    r"\s*:?\s*(?:(?:zum|am|on|einschließlich|einschliesslich)\s+)?"
    r"(?:(?:" + WEEKDAYS + r")\b\.?,?\s*)?(?:den\s+)?[\[(]?"  # "Sonntag, den …", "[01.11.2026]"
    r"(?:(?P<day>\d{1,2})\.(?P<month>\d{1,2})(?!\d)(?:\.(?:(?P<year>\d{4}|\d{2})(?!\d))?)?"
    r"|(?P<day2>\d{1,2})\.?\s*(?P<month_name>" + "|".join(MONTHS) + r")\b(?:\s+(?P<year2>\d{4})(?!\d))?)",
    re.IGNORECASE,
)

# A number that is not money, a percentage, a decimal or "50k".
_COUNT = r"(?<![\d.,])(\d{1,3})(?!\d)(?!\s*(?:€|euro|eur\b|%|\$|k\b|tsd)|[.,]\d)"
PRIZE_RES = [
    re.compile(_COUNT + r"\s?[x×](?=\s)", re.I),
    re.compile(_COUNT + r"\s+(?:gewinner|glückliche|winners?|personen|leute)", re.I),
    re.compile(r"(?:verlosen\s+wir|verlose|verlosen|giving\s+away)\s+" + _COUNT, re.I),
    re.compile(r"\b(?:1|eine?[ns]?)\s+von\s+" + _COUNT, re.I),  # "Chance auf 1 von 6 Paketen"
]
# "Ich verlose 11 Produkte an eine glückliche Person" is one prize, not eleven.
SINGLE_WINNER_RE = re.compile(
    r"\ban\s+(?:eine[nrm]?|1)\s+(?:glückliche[nr]?\s+)?(?:person|gewinner(?:in)?|follower(?:in)?)\b"
    r"|\ban\s+(?:eine[nrm]?|1)\s+von\s+euch\b|\bone\s+lucky\s+winner\b",
    re.IGNORECASE,
)
MAX_PRIZES = 20


# --------------------------------------------------------------------------- #
# Region eligibility
# --------------------------------------------------------------------------- #

COUNTRY_NAMES = {
    "DE": ["deutsch", "germany", "german", "bundesweit", "bundesgebiet"],
    "AT": ["österreich", "oesterreich", "austria"],
    "CH": ["schweiz", "switzerland", "swiss"],
    "US": ["united states", r"vereinigten?\s+staaten", "amerika", "america"],
    "UK": ["united kingdom", "großbritannien", "grossbritannien", "england", "britain", "british", r"uk\b"],
    "FR": ["frankreich", "france", "french"],
    "NL": ["niederlande", "netherlands", "holland", "nederland"],
    "CA": ["kanada", "canada"],
    "AU": ["australien", "australia"],
    "IT": ["italien", "italy", "italia"],
    "ES": ["spanien", "spain", "españa", "espana"],
    "BE": ["belgien", "belgium", "belgique", "belgië", "belgie"],
    "LU": ["luxemburg", "luxembourg"],
    "PL": [r"polen\b", "poland", "polska"],
    "CZ": ["tschechien", "czech", "česko", "cesko"],
    "DK": ["dänemark", "daenemark", "denmark", "danmark"],
    "SE": ["schweden", "sweden", "sverige"],
    "TR": ["türkei", "tuerkei", "turkey", "türkiye", "turkiye"],
    "IN": ["indien", r"india\b", "bharat"],
    "PH": ["philippinen", "philippines", "pilipinas"],
    "NG": ["nigeria"],
    "BR": ["brasilien", "brazil", "brasil"],
    "MX": ["mexiko", "mexico", "méxico"],
}
# Case-sensitive whole tokens. AT, CA and IT have none on purpose: they are
# ordinary words or abbreviations ("look at", "ca. 50€", "it").
COUNTRY_CODES = {
    "DE": ["DE", "GER"],
    "CH": ["CH"],
    "US": ["USA"],  # bare "US" is handled by the rules below
    "UK": ["UK", "GB"],
    "FR": ["FR"],
    "NL": ["NL"],
}
DACH = frozenset({"DE", "AT", "CH"})
EU = frozenset({"DE", "AT", "FR", "IT", "ES", "NL", "BE", "LU", "PL", "CZ", "DK", "SE"})

NAME_RES = {c: re.compile(r"\b(?:" + "|".join(names) + ")", re.I) for c, names in COUNTRY_NAMES.items()}
CODE_RES = {c: re.compile(r"(?<!\w)(?:" + "|".join(codes) + r")(?!\w)") for c, codes in COUNTRY_CODES.items()}
GROUP_RES = [
    (re.compile(r"(?<!\w)DACH(?!\w)"), DACH),
    (re.compile(r"(?<!\w)D-A-CH(?!\w)|(?<!\w)D/A/CH(?!\w)", re.I), DACH),
    (re.compile(r"(?<!\w)EU(?!\w)"), EU),
    (re.compile(r"eu-weit|europaweit|\beuropa|\beurope", re.I), EU),
]


def _flag(code: str) -> str:
    iso = "GB" if code == "UK" else code
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso)


FLAG_TO_COUNTRY = {_flag(code): code for code in COUNTRY_NAMES}
FLAG_PAIR_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")

WORLDWIDE_RE = re.compile(
    r"weltweit|world[\s-]?wide|internationally|internationale?n?\s+(?:shipping|versand|teilnahme)"
    r"|alle[n]?\s+ländern?|all\s+countries|every\s+country",
    re.IGNORECASE,
)
OPENER_RE = re.compile(
    r"\b(?:nur|only|ausschlie(?:ß|ss)lich|exklusiv|exclusively|lediglich|wohnsitz|wohnhaft|versand"
    r"|verschickt|shipping|ships?|open\s+to|residents?\s+of|living\s+in|teilnahmeberechtigt"
    r"|available\s+in|valid\s+in|gültig\s+in)\b",
    re.IGNORECASE,
)
CLOSER_RE = re.compile(r"\b(?:only|residents?|bewohner|einwohner)\b", re.IGNORECASE)
SENTENCE_BREAK_RE = re.compile(r"[.!?:;•|\n]")
OPEN_WINDOW = 40
CLOSE_WINDOW = 18
US_DOTS_RE = re.compile(r"(?<!\w)U\.S\.(?:A\.)?")
# Bare "US" is also the word "us" in all-caps captions ("FOLLOW US"), so it only
# counts right next to the restriction word: "nur US", "ships to the US", "US only".
US_AFTER_OPENER_RE = re.compile(r"^\s*(?:(?i:to|in den|in|for|nach|für)\s+)?(?:(?i:the)\s+)?US(?!\w)")
US_BEFORE_CLOSER_RE = re.compile(r"(?<!\w)US[\s\-–]*$")


def normalize_countries(codes) -> set[str]:
    return {"UK" if str(c).upper() == "GB" else str(c).upper() for c in codes or []}


def _countries_in(text: str, start: int, end: int, flags, opening: bool) -> set[str]:
    window = text[start:end]
    found = {c for c, rx in NAME_RES.items() if rx.search(window)}
    found |= {c for c, rx in CODE_RES.items() if rx.search(window)}
    for rx, group in GROUP_RES:
        if rx.search(window):
            found |= group
    found |= {code for fs, fe, code in flags if start <= fs and fe <= end}
    if (US_AFTER_OPENER_RE if opening else US_BEFORE_CLOSER_RE).search(window):
        found.add("US")
    return found


def region_check(text: str, allowed) -> tuple[bool, str]:
    """Return (eligible, label). Drops only captions that explicitly limit the
    giveaway to countries outside `allowed`; no mention means eligible."""
    allowed = normalize_countries(allowed)
    if not allowed:
        return True, ""
    text = US_DOTS_RE.sub("USA", text or "")
    if WORLDWIDE_RE.search(text):
        return True, "worldwide ✓"

    # Pair regional indicators from the start of each run so "🇩🇪🇦🇹" never reads as "🇪🇦".
    flags = [(m.start(), m.end(), FLAG_TO_COUNTRY[m.group()]) for m in FLAG_PAIR_RE.finditer(text)
             if m.group() in FLAG_TO_COUNTRY]
    restricted: set[str] = set()
    for m in OPENER_RE.finditer(text):
        start, end = m.end(), min(len(text), m.end() + OPEN_WINDOW)
        brk = SENTENCE_BREAK_RE.search(text, start, end)
        if brk:
            end = brk.start()
        restricted |= _countries_in(text, start, end, flags, opening=True)
    for m in CLOSER_RE.finditer(text):
        start, end = max(0, m.start() - CLOSE_WINDOW), m.start()
        breaks = list(SENTENCE_BREAK_RE.finditer(text, start, end))
        if breaks:
            start = breaks[-1].end()
        restricted |= _countries_in(text, start, end, flags, opening=False)

    if not restricted:
        return True, "region not stated"
    ok = restricted & allowed
    if ok:
        return True, f"open to {'/'.join(sorted(ok))} ✓"
    return False, f"only {'/'.join(sorted(restricted))}"


# --------------------------------------------------------------------------- #
# Caption parsing
# --------------------------------------------------------------------------- #

def is_giveaway(text: str) -> bool:
    return bool(GIVEAWAY_RE.search(text) and ACTION_RE.search(text))


def is_ended(text: str) -> bool:
    return bool(ENDED_RE.search(text))


def scam_flags(text: str) -> list[str]:
    return [label for rx, label in SCAM_FLAGS if rx.search(text)]


def parse_deadline(text: str, created: datetime) -> datetime | None:
    """First "bis 02.11." / "endet am 8. November" style date, as 23:59 Berlin time on that day."""
    posted = created.astimezone(BERLIN)
    for m in DEADLINE_RE.finditer(text):
        if m["day"]:
            day, month, year = int(m["day"]), int(m["month"]), m["year"]
        else:
            day, month, year = int(m["day2"]), MONTHS[m["month_name"].lower()], m["year2"]
        if year:
            year = int(year) + (2000 if len(year) == 2 else 0)
        else:
            year = posted.year + (1 if posted.month - month > 6 else 0)
        try:
            return datetime(year, month, day, 23, 59, tzinfo=BERLIN)
        except ValueError:
            continue
    return None


def count_prizes(text: str) -> int:
    if SINGLE_WINNER_RE.search(text):
        return 1
    counts = [int(m.group(1)) for rx in PRIZE_RES for m in rx.finditer(text)]
    counts = [n for n in counts if 1 <= n <= MAX_PRIZES]
    return max(counts, default=1)


def project_comments(comments: int, age_hours: float, expected_hours: float, tau: float) -> float:
    """Project the final comment count, assuming the share of lifetime comments
    reached after t hours is F(t) = 1 - exp(-t / tau)."""
    def share(t: float) -> float:
        return 1 - math.exp(-t / tau)

    projected = comments * share(expected_hours) / max(share(age_hours), 0.02) if expected_hours > age_hours else comments
    return max(projected, comments, 1)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _create_time(item: dict) -> datetime | None:
    ts = item.get("createTime")
    if ts:
        try:
            ts = float(ts)
            return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    iso = item.get("createTimeISO")
    if iso:
        try:
            created = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            return created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def normalize_items(items) -> list[dict]:
    """Apify items -> posts. Skips error rows (no id) and duplicate ids."""
    posts, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        post_id = str(item["id"])
        if post_id in ids:
            continue
        ids.add(post_id)
        author_meta = item.get("authorMeta") or {}
        author = author_meta.get("name") or ""
        posts.append({
            "id": post_id,
            "text": item.get("text") or "",
            "created": _create_time(item),
            "comments": _to_int(item.get("commentCount")),
            "plays": _to_int(item.get("playCount")),
            "url": item.get("webVideoUrl") or f"https://www.tiktok.com/@{author}/video/{post_id}",
            "author": author,
            "followers": author_meta.get("fans"),
            "verified": bool(author_meta.get("verified")),
        })
    return posts


def process_posts(posts: list[dict], cfg: dict, now: datetime) -> tuple[list[dict], dict]:
    stats = dict.fromkeys(STAT_KEYS, 0)
    stats["fetched"] = len(posts)
    active = []
    for post in posts:
        text = post["text"]
        if not is_giveaway(text):
            stats["not_giveaway"] += 1
            continue
        if is_ended(text):
            stats["ended"] += 1
            continue
        eligible, region = region_check(text, cfg["allowed_countries"])
        if not eligible:
            stats["wrong_region"] += 1
            continue
        created = post["created"]
        age_hours = max((now - created).total_seconds() / 3600, 1) if created else None
        if age_hours is None or age_hours > cfg["max_age_days"] * 24:
            stats["too_old"] += 1
            continue
        if post["followers"] is not None and _to_int(post["followers"]) < cfg["min_author_followers"]:
            stats["small_account"] += 1
            continue
        deadline = parse_deadline(text, created)
        if deadline and deadline < now:
            stats["ended"] += 1
            continue

        expected_hours = ((deadline - created).total_seconds() / 3600 if deadline
                          else cfg["assumed_duration_days"] * 24)
        projected = project_comments(post["comments"], age_hours, expected_hours, cfg["comment_decay_hours"])
        prizes = count_prizes(text)
        per_prize = projected / prizes
        active.append({
            **post,
            "region": region,
            "age_hours": age_hours,
            "deadline": deadline,
            "prizes": prizes,
            "projected": projected,
            "per_prize": per_prize,
            "chance": 1 / per_prize,
            "flags": scam_flags(text),
        })
    stats["active"] = len(active)
    return active, stats


# --------------------------------------------------------------------------- #
# Rolling cutoff
# --------------------------------------------------------------------------- #

def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def update_history(history: dict, active: list[dict], today: date, keep_days: int) -> dict:
    """Store today's score per post (overwriting older estimates) and drop
    entries older than keep_days."""
    history = dict(history)
    for g in active:
        history[g["id"]] = {"date": today.isoformat(), "per_prize": round(g["per_prize"], 2)}
    oldest = today - timedelta(days=keep_days)
    return {
        pid: entry for pid, entry in history.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("per_prize"), (int, float))
        and (day := _parse_date(entry.get("date"))) is not None
        and day >= oldest
    }


def percentile(values, p: float) -> float:
    xs = sorted(values)
    if not xs:
        raise ValueError("percentile of an empty list")
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def compute_cutoff(values: list[float], cfg: dict) -> tuple[float | None, str]:
    """Return (cutoff, explanation for the summary line)."""
    n, p = len(values), cfg["percentile_cutoff"]
    if n >= cfg["min_history_for_percentile"]:
        cutoff = percentile(values, p)
        method = f"the cheapest {p:g}% of {n} giveaways seen in the last {cfg['history_days']} days"
    elif n:
        cutoff = percentile(values, 50)
        method = (f"the median of {plural(n, 'giveaway')} seen so far "
                  f"(the cheapest-{p:g}% rule starts at {cfg['min_history_for_percentile']})")
    else:
        cutoff, method = None, "no history yet, so no cutoff"
    hard = cfg.get("hard_max_per_prize")
    if hard is not None and (cutoff is None or hard < cutoff):
        cutoff, method = float(hard), f"capped by hard_max_per_prize ({fmt_int(hard)})"
    return cutoff, method


def select_giveaways(active: list[dict], cutoff: float | None, cfg: dict, seen: dict) -> list[dict]:
    kept = [g for g in active if cutoff is None or round(g["per_prize"], 2) <= cutoff + 1e-9]
    if cfg["only_new"]:
        kept = [g for g in kept if g["id"] not in seen]
    return sorted(kept, key=lambda g: (-g["chance"], g["id"]))


def prune_seen(seen: dict, today: date) -> dict:
    oldest = today - timedelta(days=SEEN_RETENTION_DAYS)
    return {pid: d for pid, d in seen.items() if (day := _parse_date(d)) is not None and day >= oldest}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def fmt_int(value) -> str:
    return f"{round(value):,}".replace(",", ".")


def plural(n, word: str) -> str:
    return f"{fmt_int(n)} {word if round(n) == 1 else word + 's'}"


def short_caption(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= CAPTION_LENGTH else text[: CAPTION_LENGTH - 1].rstrip() + "…"


def age_label(hours: float) -> str:
    return f"{int(hours)}h ago" if hours < 24 else f"{int(hours // 24)}d ago"


@dataclass
class Entry:
    title: str  # without the rank
    stats: str
    caption: str
    warning: str
    url: str


@dataclass
class Report:
    date: str
    summary: str
    count: int
    entries: list[Entry] = field(default_factory=list)
    more: int = 0

    @property
    def header(self) -> str:
        return f"🎁 TikTok giveaways - {self.date}"

    @property
    def footer(self) -> str:
        if self.more:
            return f"…and {self.more} more (raise max_list_length to see them)."
        return "" if self.entries else "Nothing below the cutoff today."


def make_entry(g: dict, is_new: bool) -> Entry:
    title = ("🆕 " if is_new else "") + f"@{g['author']}" + (" ✔️" if g["verified"] else "")
    title += f" · {plural(g['prizes'], 'prize')}"
    if g["region"]:
        title += f" · {g['region']}"
    competitors = max(1, round(g["per_prize"]))
    ends = f"ends {g['deadline']:%d.%m.}" if g["deadline"] else "no date found"
    stats = (f"~{plural(competitors, 'competitor')} per prize (≈1 in {fmt_int(competitors)})"
             f" · {plural(g['comments'], 'comment')} now · {ends} · posted {age_label(g['age_hours'])}")
    warning = "⚠️ " + "; ".join(g["flags"]) if g["flags"] else ""
    return Entry(title, stats, short_caption(g["text"]), warning, g["url"])


def build_report(today: date, stats: dict, selected: list[dict], cfg: dict,
                 cutoff: float | None, method: str, seen: dict) -> Report:
    parts = [f"{plural(stats['active'], 'active giveaway')} found ({plural(stats['fetched'], 'post')} checked)."]
    if stats["wrong_region"]:
        parts.append(f"{stats['wrong_region']} skipped because they're limited to other countries.")
    n = len(selected)
    if cutoff is None:
        parts.append(f"{n} listed - {method}.")
    else:
        parts.append(f"{n} {'is' if n == 1 else 'are'} at or below {fmt_int(cutoff)} competitors per prize - {method}.")
    shown = selected[: cfg["max_list_length"]]
    return Report(
        date=today.strftime("%d.%m.%Y"),
        summary=" ".join(parts),
        count=n,
        entries=[make_entry(g, g["id"] not in seen) for g in shown],
        more=n - len(shown),
    )


def render_text(report: Report) -> str:
    blocks = [f"{report.header}\n{report.summary}"]
    for rank, e in enumerate(report.entries, 1):
        lines = [f"{rank}. {e.title}", e.stats, e.caption] + ([e.warning] if e.warning else []) + [e.url]
        blocks.append("\n".join(lines))
    if report.footer:
        blocks.append(report.footer)
    return "\n\n".join(blocks) + "\n"


_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>#|])")


def _md(text: str) -> str:
    return _MD_SPECIAL_RE.sub(r"\\\1", text)


def render_markdown(report: Report) -> str:
    blocks = [f"# {report.header}", _md(report.summary)]
    for rank, e in enumerate(report.entries, 1):
        lines = [f"[{_md(f'{rank}. {e.title}')}]({e.url})", _md(e.stats), f"*{_md(e.caption)}*"]
        if e.warning:
            lines.append(_md(e.warning))
        blocks.append("\\\n".join(lines))
    if report.footer:
        blocks.append(_md(report.footer))
    return "\n\n".join(blocks) + "\n"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_telegram(report: Report) -> list[str]:
    """HTML messages under TELEGRAM_CHUNK_LIMIT characters, split only between entries."""
    blocks = [f"<b>{_esc(report.header)}</b>\n{_esc(report.summary)}"]
    for rank, e in enumerate(report.entries, 1):
        lines = [f'<a href="{html.escape(e.url)}">{_esc(f"{rank}. {e.title}")}</a>', _esc(e.stats),
                 f"<i>{_esc(e.caption)}</i>"]
        if e.warning:
            lines.append(_esc(e.warning))
        blocks.append("\n".join(lines))
    if report.footer:
        blocks.append(_esc(report.footer))

    chunks, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) >= TELEGRAM_CHUNK_LIMIT:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


EMAIL_FOOTER = ("The odds are estimates based on comment counts, not exact participant numbers. "
                "Check each giveaway's rules (often in the profile bio) before you enter.")


def render_email_html(report: Report) -> str:
    items = []
    for e in report.entries:
        lines = [f'<a href="{html.escape(e.url)}">{_esc(e.title)}</a>', _esc(e.stats), f"<i>{_esc(e.caption)}</i>"]
        if e.warning:
            lines.append(_esc(e.warning))
        items.append(f'<li style="margin-bottom:12px">{"<br>".join(lines)}</li>')
    body = [f"<h2>{_esc(report.header)}</h2>", f"<p>{_esc(report.summary)}</p>"]
    if items:
        body.append("<ol>\n" + "\n".join(items) + "\n</ol>")
    if report.footer:
        body.append(f"<p>{_esc(report.footer)}</p>")
    body.append(f'<p style="color:#666;font-size:12px">{_esc(EMAIL_FOOTER)}</p>')
    return ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
            f"<title>{_esc(report.header)}</title></head>\n"
            '<body style="font-family:Arial,Helvetica,sans-serif;line-height:1.4">\n'
            + "\n".join(body) + "\n</body></html>\n")


def email_subject(report: Report) -> str:
    return f"🎁 {plural(report.count, 'TikTok giveaway')} - {report.date}"


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def env(name: str) -> str | None:
    """Environment variable, with "" treated as unset (GitHub passes missing secrets as "")."""
    return (os.environ.get(name) or "").strip() or None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def send_telegram(token: str, chat_id: str, chunks: list[str]) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram rejected the message (HTTP {err.code}): {detail}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {body}")


def email_settings() -> dict | None:
    host, to = env("SMTP_HOST"), env("EMAIL_TO")
    if not (host and to):
        return None
    return {
        "host": host,
        "port": int(env("SMTP_PORT") or 587),
        "user": env("SMTP_USER"),
        "password": env("SMTP_PASSWORD"),
        "to": [addr.strip() for addr in to.split(",") if addr.strip()],
    }


def send_email(settings: dict, report: Report) -> None:
    msg = EmailMessage()
    msg["Subject"] = email_subject(report)
    msg["From"] = settings["user"] or settings["to"][0]
    msg["To"] = ", ".join(settings["to"])
    msg.set_content(render_text(report) + "\n" + EMAIL_FOOTER + "\n")
    msg.add_alternative(render_email_html(report), subtype="html")
    with smtplib.SMTP(settings["host"], settings["port"], timeout=60) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings["user"] and settings["password"]:
            smtp.login(settings["user"], settings["password"])
        smtp.send_message(msg)


def deliver(report: Report) -> list[str]:
    """Send to every configured channel; print to stdout if none is. Returns errors."""
    errors = []
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    telegram = bool(token and chat_id)
    if telegram:
        try:
            chunks = render_telegram(report)
            send_telegram(token, chat_id, chunks)
            log(f"Sent {len(chunks)} Telegram message(s).")
        except Exception as exc:  # keep going so e-mail still goes out
            errors.append(f"Telegram: {exc}")
    elif token or chat_id:
        log("Telegram needs both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID - skipped.")

    smtp = email_settings()
    if smtp:
        try:
            send_email(smtp, report)
            log(f"Sent e-mail to {', '.join(smtp['to'])}.")
        except Exception as exc:
            errors.append(f"E-mail: {exc}")

    if not (telegram or smtp):
        print(render_text(report))
    for error in errors:
        log(f"ERROR {error}")
    return errors


# --------------------------------------------------------------------------- #
# Apify
# --------------------------------------------------------------------------- #

def build_run_input(cfg: dict) -> dict:
    run_input = {
        "hashtags": list(cfg["hashtags"]),
        "searchQueries": list(cfg["search_queries"]),
        "resultsPerPage": cfg["results_per_seed"],
        "searchSection": "/video",
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        # Replaces the old shouldDownloadSubtitles flag on the actor's Input tab.
        "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
    }
    if cfg["proxy_country"]:
        run_input["proxyCountryCode"] = cfg["proxy_country"]
    run_input.update(cfg.get("apify_input_extra") or {})
    return run_input


def _field(obj, snake: str, camel: str):
    """apify-client 3.x returns pydantic models; older code paths return camelCase dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(camel, obj.get(snake))
    return getattr(obj, snake, None)


def within_budget(client, cfg: dict) -> bool:
    """Check this Apify billing cycle's usage. A run costs at most
    max_cost_per_run_usd, so running only while usage + cap stays within the
    plan's monthly limit (or monthly_budget_usd, if lower) never exceeds it."""
    try:
        account = client.user().limits()
    except Exception as exc:  # e.g. a scoped token without access to account data
        log(f"Warning: could not read the Apify usage ({exc}); relying on the per-run cost cap.")
        return True
    used = _field(_field(account, "current", "current"), "monthly_usage_usd", "monthlyUsageUsd") or 0.0
    plan_limit = _field(_field(account, "limits", "limits"), "max_monthly_usage_usd", "maxMonthlyUsageUsd")
    budgets = [b for b in (plan_limit, cfg.get("monthly_budget_usd")) if b is not None]
    if not budgets:
        return True
    budget, cap = min(budgets), cfg["max_cost_per_run_usd"] or 0
    log(f"Apify usage this billing cycle: ${used:.2f} of ${budget:.2f}.")
    if used + cap <= budget:
        return True
    resets = _field(_field(account, "monthly_usage_cycle", "monthlyUsageCycle"), "end_at", "endAt")
    if isinstance(resets, datetime):
        resets = f"{resets.astimezone(BERLIN):%d.%m.%Y}"
    log(f"Skipping this run: it may cost up to ${cap:.2f}, which could exceed ${budget:.2f}. "
        f"The usage resets {'on ' + str(resets) if resets else 'with the next billing cycle'}.")
    return False


def fetch_items(cfg: dict, token: str) -> list[dict] | None:
    """Run the actor and return its items, or None when the budget check says to skip."""
    from apify_client import ApifyClient  # imported here so --sample works without it

    max_items = (len(cfg["hashtags"]) + len(cfg["search_queries"])) * cfg["results_per_seed"]
    max_cost = cfg["max_cost_per_run_usd"]
    client = ApifyClient(token)
    if not within_budget(client, cfg):
        return None
    log(f"Running {cfg['apify_actor']} for up to {max_items} posts (cost cap ${max_cost})...")
    run = client.actor(cfg["apify_actor"]).call(
        run_input=build_run_input(cfg),
        max_items=max_items,
        max_total_charge_usd=Decimal(str(max_cost)) if max_cost is not None else None,
        run_timeout=APIFY_RUN_TIMEOUT,
    )
    if run is None:
        raise RuntimeError("Apify did not return a run.")
    status = _field(run, "status", "status")
    status = getattr(status, "value", status)
    dataset_id = _field(run, "default_dataset_id", "defaultDatasetId")
    cost = _field(run, "usage_total_usd", "usageTotalUsd")
    log(f"Apify run {status}, cost ${cost if cost is not None else '?'}.")
    if not dataset_id:
        raise RuntimeError(f"Apify run {status} has no dataset.")
    items = list(client.dataset(dataset_id).iterate_items())
    if not items:  # a SUCCEEDED run can still have scraped nothing
        message = _field(run, "status_message", "statusMessage")
        raise RuntimeError(f"Apify run {status} returned no posts ({message}). Check the run in the Apify "
                           "Console; if an input option stopped working, remove it from apify_input_extra.")
    if status != "SUCCEEDED":
        log(f"Warning: Apify run {status}; using the {len(items)} items it produced.")
    return items


# --------------------------------------------------------------------------- #
# Config, state, CLI
# --------------------------------------------------------------------------- #

def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log(f"Warning: could not read {path} ({exc}); starting empty.")
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_items(path: Path) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("items", []) if isinstance(data, dict) else data


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find TikTok giveaways with the fewest competitors per prize.")
    parser.add_argument("--dry-run", action="store_true", help="print the list, send nothing, keep state unchanged")
    parser.add_argument("--sample", metavar="FILE", help="use a saved Apify JSON export instead of a live run "
                                                         "(never writes state)")
    parser.add_argument("--save-raw", metavar="FILE", help="also save the raw Apify items to FILE")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    now = datetime.now(timezone.utc)
    today = now.astimezone(BERLIN).date()

    if args.sample:
        items = load_items(args.sample)
        log(f"Loaded {len(items)} items from {args.sample}.")
    else:
        token = env("APIFY_TOKEN")
        if not token:
            sys.exit(MISSING_TOKEN_MESSAGE)
        items = fetch_items(cfg, token)
        if items is None:
            return 0
    if args.save_raw:
        save_json(Path(args.save_raw), items)
        log(f"Saved {len(items)} raw items to {args.save_raw}.")

    active, stats = process_posts(normalize_items(items), cfg, now)
    log("Stats: " + ", ".join(f"{k}={stats[k]}" for k in STAT_KEYS))

    seen = load_json(STATE_DIR / "seen.json")
    history = update_history(load_json(STATE_DIR / "history.json"), active, today, cfg["history_days"])
    cutoff, method = compute_cutoff([e["per_prize"] for e in history.values()], cfg)
    selected = select_giveaways(active, cutoff, cfg, seen)
    report = build_report(today, stats, selected, cfg, cutoff, method, seen)
    REPORT_PATH.write_text(render_markdown(report), encoding="utf-8")

    errors = []
    if args.dry_run:
        print(render_text(report))
    else:
        errors = deliver(report)

    if args.sample or args.dry_run:
        log("State not written (--sample / --dry-run).")
    else:
        for g in selected[: cfg["max_list_length"]]:
            seen.setdefault(g["id"], today.isoformat())
        save_json(STATE_DIR / "seen.json", prune_seen(seen, today))
        save_json(STATE_DIR / "history.json", history)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
