"""OddsPapi client (https://oddspapi.io) built for a tight monthly quota.

Every response is cached to disk and every live request is counted against a
local monthly budget, so re-running the app never silently burns requests.

Setup:
    Put your key in a `.env` file next to this script (gitignored):

        ODDSPAPI_KEY=your_key_here

    or export it as an environment variable.
"""

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import requests

BASE_URL = "https://api.oddspapi.io/v4"
API_KEY_ENV_VAR = "ODDSPAPI_KEY"
MONTHLY_QUOTA = 250

# American Football on OddsPapi. Hardcoded so we never spend requests on lookups.
NFL_SPORT_ID = 14
NFL_TOURNAMENT_ID = 31

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / ".cache" / "odds"
REQUEST_LOG = CACHE_DIR / "request_log.json"

# Cache lifetimes. Reference data almost never changes; odds move constantly,
# but with 250 requests/month we can only afford a handful of refreshes a week.
TTL_FOREVER = None
TTL_ODDS = 12 * 60 * 60  # 12 hours

# Hook the UI can set to ask the user before a live request is made.
# Signature: (description: str, remaining: int) -> bool
ConfirmFn = Callable[[str, int], bool]
confirm_request: ConfirmFn | None = None


class OddsApiError(RuntimeError):
    """Raised for missing keys, quota exhaustion, or API error responses."""


# --------------------------------------------------------------------------- #
# Key handling
# --------------------------------------------------------------------------- #
def _load_dotenv() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _get_api_key() -> str:
    _load_dotenv()
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise OddsApiError(
            f"Missing API key. Add {API_KEY_ENV_VAR}=... to a .env file "
            "or export it as an environment variable."
        )
    return api_key


# --------------------------------------------------------------------------- #
# Quota tracking
# --------------------------------------------------------------------------- #
def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _empty_log() -> dict[str, Any]:
    return {"month": _current_month(), "requests": [], "manual_adjustment": 0, "adjustments": []}


def _read_log() -> dict[str, Any]:
    if REQUEST_LOG.exists():
        return cast(dict[str, Any], json.loads(REQUEST_LOG.read_text()))
    return _empty_log()


def _write_log(log: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    REQUEST_LOG.write_text(json.dumps(log, indent=2))


def requests_used_this_month() -> int:
    log = _read_log()
    if log.get("month") != _current_month():
        return 0
    return len(log.get("requests", []))


def manual_adjustment_this_month() -> int:
    """Net manual adjustment applied to this month's count (can be negative)."""
    log = _read_log()
    if log.get("month") != _current_month():
        return 0
    return int(log.get("manual_adjustment", 0))


def adjustment_history_this_month() -> list[dict[str, Any]]:
    """Audit trail of manual adjustments made this month, oldest first."""
    log = _read_log()
    if log.get("month") != _current_month():
        return []
    return cast(list[dict[str, Any]], list(log.get("adjustments", [])))


def adjust_usage(delta: int, note: str = "") -> int:
    """Manually nudge this month's usage count by `delta` (+/-).

    This sits on top of the automatic per-request counting below and never
    touches the `requests` log, so the existing counting logic (and the
    untracked-file reconciliation it powers) keeps working unchanged. Use
    this to correct the displayed total when it drifts from what the
    provider actually billed (e.g. a request that failed on their end but
    still consumed quota, or a correction after the fact).

    Returns the new total usage for the month (auto-counted + adjustments).
    """
    log = _read_log()
    if log.get("month") != _current_month():
        log = _empty_log()

    current_adjustment = int(log.get("manual_adjustment", 0))
    log["manual_adjustment"] = current_adjustment + delta

    history = cast(list[dict[str, Any]], list(log.get("adjustments", [])))
    history.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "delta": delta,
            "note": note,
            "total_after": log["manual_adjustment"],
        }
    )
    log["adjustments"] = history

    _write_log(log)
    return requests_used_this_month() + log["manual_adjustment"]


def requests_remaining() -> int:
    total_used = requests_used_this_month() + manual_adjustment_this_month()
    return max(0, MONTHLY_QUOTA - total_used)


def _last_logged_time() -> float | None:
    """Timestamp (epoch seconds) of the most recent logged request, or None."""
    log = _read_log()
    if log.get("month") != _current_month():
        return None
    entries = cast(list[dict[str, Any]], log.get("requests", []))
    if not entries:
        return None
    latest = max(entries, key=lambda e: e.get("at", ""))
    try:
        return datetime.fromisoformat(latest["at"]).timestamp()
    except (KeyError, ValueError):
        return None


def find_untracked_cache_files() -> list[Path]:
    """Odds cache files written *after* the last logged request.

    Every live request made through this module logs itself before writing
    its cache file, so a cache file newer than the most recent log entry
    can only mean a request was made outside this app (e.g. a manual script
    or curl call) and never counted against the quota. Surfacing these lets
    the UI warn you and offer to reconcile the count.
    """
    if not CACHE_DIR.exists():
        return []
    last_logged = _last_logged_time()
    if last_logged is None:
        # No requests logged yet this month; any cache file is suspect.
        return sorted(p for p in CACHE_DIR.glob("*.json") if p.name != REQUEST_LOG.name)
    return sorted(
        p
        for p in CACHE_DIR.glob("*.json")
        if p.name != REQUEST_LOG.name and p.stat().st_mtime > last_logged + 1
    )


def reconcile_untracked_requests() -> int:
    """Log a manual-adjustment entry for each untracked cache file found.

    Returns the number of entries added. Call this once you've confirmed
    the untracked file(s) came from a real request against the live API
    (not e.g. a file you copied in by hand), so the monthly count matches
    what OddsPapi actually billed.
    """
    untracked = find_untracked_cache_files()
    for cache_file in untracked:
        _record_request(
            "manual-adjustment",
            {"note": f"untracked cache file reconciled: {cache_file.name}"},
        )
    return len(untracked)


def _record_request(path: str, params: dict[str, Any]) -> None:
    log = _read_log()
    if log.get("month") != _current_month():
        log = _empty_log()
    safe_params = {k: v for k, v in params.items() if k != "apiKey"}
    entries = cast(list[dict[str, Any]], list(log.get("requests", [])))
    entries.append(
        {"at": datetime.now(timezone.utc).isoformat(), "path": path, "params": safe_params}
    )
    log["requests"] = entries
    _write_log(log)


# --------------------------------------------------------------------------- #
# Cached GET
# --------------------------------------------------------------------------- #
def _cache_path(path: str, params: dict[str, Any]) -> Path:
    key_parts = [path.replace("/", "_")] + [
        f"{k}-{v}" for k, v in sorted(params.items()) if k != "apiKey"
    ]
    return CACHE_DIR / ("__".join(str(p) for p in key_parts) + ".json")


def _read_cache(cache_file: Path, ttl: int | None) -> Any | None:
    if not cache_file.exists():
        return None
    if ttl is not None and time.time() - cache_file.stat().st_mtime > ttl:
        return None
    return json.loads(cache_file.read_text())


def _get(
    path: str,
    params: dict[str, Any] | None = None,
    ttl: int | None = TTL_ODDS,
    force_refresh: bool = False,
) -> Any:
    """GET from OddsPapi, serving from disk cache whenever possible.

    A live request is only made when the cache is missing/expired (or
    force_refresh is set), the quota isn't exhausted, and the confirm hook
    (if installed) says yes.
    """
    params = dict(params or {})
    cache_file = _cache_path(path, params)

    if not force_refresh:
        cached = _read_cache(cache_file, ttl)
        if cached is not None:
            return cached

    remaining = requests_remaining()
    if remaining <= 0:
        raise OddsApiError(
            f"Monthly quota of {MONTHLY_QUOTA} requests exhausted. "
            "Serving cached data only until next month."
        )

    if confirm_request is not None and not confirm_request(path, remaining):
        # User declined; fall back to stale cache if any exists.
        stale = _read_cache(cache_file, ttl=None)
        if stale is not None:
            return stale
        raise OddsApiError("Request cancelled and no cached data available.")

    params["apiKey"] = _get_api_key()
    response = requests.get(f"{BASE_URL}/{path}", params=params, timeout=15)
    _record_request(path, params)

    if not response.ok:
        raise OddsApiError(f"OddsPapi '{path}' failed ({response.status_code}): {response.text}")

    data = response.json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    return data


def cache_age(path: str, params: dict[str, Any] | None = None) -> float | None:
    """Seconds since the cached response for this request was written, or None."""
    cache_file = _cache_path(path, dict(params or {}))
    if not cache_file.exists():
        return None
    return time.time() - cache_file.stat().st_mtime


# --------------------------------------------------------------------------- #
# Reference data (fetched once, cached forever)
# --------------------------------------------------------------------------- #
def get_sports() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _get("sports", ttl=TTL_FOREVER))


def get_markets() -> list[dict[str, Any]]:
    """Market definitions (id -> name). Needed to decode odds responses."""
    return cast(list[dict[str, Any]], _get("markets", ttl=TTL_FOREVER))


def get_bookmakers() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _get("bookmakers", ttl=TTL_FOREVER))


def get_tournaments(sport_id: int) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _get("tournaments", {"sportId": sport_id}, ttl=TTL_FOREVER),
    )


def get_participants(sport_id: int) -> dict[int, str]:
    """Team id -> name mapping for a sport.

    OddsPapi returns a flat object: {"347948": "Team Name", ...}.
    """
    raw = cast(
        dict[str, str],
        _get("participants", {"sportId": sport_id}, ttl=TTL_FOREVER),
    )
    return {int(pid): name for pid, name in raw.items()}


def find_nfl_tournament_id(sport_id: int = NFL_SPORT_ID) -> int:
    """Look up the NFL tournamentId. Prefer NFL_TOURNAMENT_ID; this is a fallback."""
    for t in get_tournaments(sport_id):
        if (
            str(t.get("tournamentSlug", "")).lower() == "nfl"
            or str(t.get("tournamentName", "")).upper() == "NFL"
        ):
            return int(t["tournamentId"])
    raise OddsApiError("Could not find an NFL tournament for this sport.")


# --------------------------------------------------------------------------- #
# Odds (the one call that actually costs us regularly)
# --------------------------------------------------------------------------- #
def _odds_params(
    tournament_id: int, bookmaker: str, odds_format: str = "american"
) -> dict[str, Any]:
    return {
        "bookmaker": bookmaker,
        "tournamentIds": str(tournament_id),
        "oddsFormat": odds_format,
    }


def get_odds_by_tournament(
    tournament_id: int,
    bookmaker: str = "pinnacle",
    odds_format: str = "american",
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """All upcoming fixtures + odds for a tournament from ONE bookmaker in ONE request."""
    return cast(
        list[dict[str, Any]],
        _get(
            "odds-by-tournaments",
            _odds_params(tournament_id, bookmaker, odds_format),
            ttl=TTL_ODDS,
            force_refresh=force_refresh,
        ),
    )


def odds_cache_age(tournament_id: int, bookmaker: str) -> float | None:
    return cache_age("odds-by-tournaments", _odds_params(tournament_id, bookmaker))


def get_odds_for_bookmakers(
    tournament_id: int,
    bookmakers: list[str],
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch each bookmaker (one request each) and merge them by fixture.

    The API only accepts a single `bookmaker` per call, so N books = N requests
    (cached independently). Returns (fixtures, errors) where `errors` maps a
    bookmaker slug to the reason it was skipped, so one bad book doesn't sink
    the whole report.
    """
    merged: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for book in bookmakers:
        try:
            fixtures = get_odds_by_tournament(
                tournament_id, bookmaker=book, force_refresh=force_refresh
            )
        except OddsApiError as exc:
            errors[book] = str(exc)
            continue
        for fx in fixtures:
            fid = str(fx.get("fixtureId"))
            target = merged.get(fid)
            if target is None:
                target = dict(fx)
                target["bookmakerOdds"] = {}
                merged[fid] = target
            target["bookmakerOdds"].update(fx.get("bookmakerOdds", {}))

    return list(merged.values()), errors
