"""WinnerMaker - Streamlit front end.

Run with:  streamlit run main.py
"""
from __future__ import annotations

import contextlib
import io
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

import analysis
import apiConnect
import display
import oddsApi

st.set_page_config(page_title="WinnerMaker", page_icon="", layout="wide")

# Paste your ASCII art back in here if you want it on the home page.
SPLASH_IMAGE = ""

HEADER = "Welcome to The WinnerMaker"
INSTRUCTIONS = (
    "For uploading a team please use json format. WinnerMaker will use this and "
    "flag specific events for these players."
)
BOOKMAKER = "pinnacle"
TEAM_FILE = Path("data/team.json")

ODDS_ERRORS = (oddsApi.OddsApiError, KeyError, TypeError, ValueError)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def capture(fn, *args, **kwargs) -> str:
    """Run one of the existing display.* functions and return what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return ANSI_RE.sub("", buf.getvalue())


def show_report(fn, *args) -> None:
    text = capture(fn, *args).rstrip()
    if text:
        st.code(text, language=None)
    else:
        st.info("Nothing to report.")


def to_df(items) -> pd.DataFrame:
    """Best-effort conversion of dataclasses / dicts / plain objects to a DataFrame."""
    rows = []
    for it in items:
        if is_dataclass(it) and not isinstance(it, type):
            rows.append(asdict(it))
        elif isinstance(it, dict):
            rows.append(it)
        elif hasattr(it, "_asdict"):
            rows.append(it._asdict())
        elif hasattr(it, "__dict__"):
            rows.append(vars(it))
        else:
            rows.append({"value": it})
    return pd.DataFrame(rows)


def show_table(items) -> None:
    df = to_df(items)
    try:
        st.dataframe(df, width="stretch", hide_index=True)
    except Exception:  # mixed / nested cell types
        st.dataframe(df.astype(str), width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# Live-request confirmation (replaces the input() prompt)
# --------------------------------------------------------------------------- #
def confirm_live_request(path: str, remaining: int) -> bool:
    """Called by oddsApi before every request that would hit the network.

    A web app can't block on input(), so: approve only if the user has just
    clicked a button that set `allow_live`; otherwise remember what was blocked
    so the page can offer a "spend a request" button.
    """
    if st.session_state.get("allow_live"):
        return True
    st.session_state["blocked_request"] = (path, remaining)
    return False


def live_request_gate() -> None:
    blocked = st.session_state.get("blocked_request")
    if not blocked:
        return
    path, remaining = blocked
    st.warning(
        f"`{path}` isn't cached. Fetching it will spend a request "
        f"({remaining} remaining this month)."
    )
    if st.button("Spend a request and fetch", type="primary"):
        st.session_state["allow_live"] = True
        st.session_state["blocked_request"] = None
        st.rerun()


def report_odds_error(exc: Exception) -> None:
    if isinstance(exc, oddsApi.OddsApiError):
        st.error(str(exc))
        live_request_gate()
    else:
        st.error(f"Unexpected response shape from OddsPapi: {exc!r}")
        st.caption("Inspect the cached JSON in .cache/odds/ and adjust the field names.")


def sidebar_quota() -> None:
    used = oddsApi.requests_used_this_month()
    total = oddsApi.MONTHLY_QUOTA
    st.sidebar.metric("Odds API requests this month", f"{used} / {total}")
    st.sidebar.progress(min(used / total, 1.0) if total else 0.0)


# --------------------------------------------------------------------------- #
# Data loading (cached so widget clicks don't redo the work)
# --------------------------------------------------------------------------- #
@dataclass
class OddsData:
    games: list
    props: list
    age_seconds: float | None
    fixture_count: int


def _fetch_odds(force_refresh: bool) -> OddsData:
    """Raises OddsApiError / KeyError / TypeError / ValueError on failure."""
    sport_id = oddsApi.NFL_SPORT_ID
    tournament_id = oddsApi.NFL_TOURNAMENT_ID
    participants = oddsApi.get_participants(sport_id)
    market_defs = analysis.build_market_defs(oddsApi.get_markets(), sport_id)
    fixtures = oddsApi.get_odds_by_tournament(
        tournament_id, bookmaker=BOOKMAKER, force_refresh=force_refresh
    )
    age = oddsApi.cache_age(
        "odds-by-tournaments",
        {"bookmaker": BOOKMAKER, "tournamentIds": str(tournament_id), "oddsFormat": "american"},
    )
    games = analysis.extract_game_signals(fixtures, participants, market_defs, BOOKMAKER)
    props = analysis.extract_prop_lines(fixtures, participants, market_defs, BOOKMAKER)
    return OddsData(games, props, age, len(fixtures))


# Exceptions are never cached by Streamlit, so a blocked/failed load retries next time.
@st.cache_resource(ttl=1800, show_spinner="Loading odds...")
def _cached_odds() -> OddsData:
    return _fetch_odds(force_refresh=False)


@st.cache_resource(ttl=3600)
def _week_games():
    return apiConnect.upcoming_week_games()


@st.cache_resource(ttl=6 * 3600)
def _player_index():
    return apiConnect.build_player_index()


@st.cache_resource(ttl=1800, show_spinner="Building player table...")
def _build_candidates(matchups: tuple[str, ...]):
    odds = _cached_odds()
    wanted = set(matchups)
    covered = {g.matchup for g in odds.games} & wanted
    if not covered:
        return [], covered, True

    try:
        index = _player_index()
        roster_ok = True
    except apiConnect.NflDataError:
        index, roster_ok = {}, False

    def lookup(name: str) -> tuple[str, str] | None:
        info = apiConnect.lookup_player(index, name)
        return (info.team, info.position) if info else None

    candidates = analysis.build_candidates(odds.props, odds.games, wanted, lookup)
    for c in candidates:
        c.player = apiConnect.display_player_name(c.player)
    return candidates, covered, roster_ok


def load_odds() -> OddsData | None:
    st.session_state["blocked_request"] = None
    try:
        return _cached_odds()
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return None
    finally:
        st.session_state["allow_live"] = False


def refresh_odds() -> None:
    """Forced refresh: bypasses the on-disk cache and spends a request."""
    st.session_state["allow_live"] = True
    try:
        _fetch_odds(force_refresh=True)
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return
    finally:
        st.session_state["allow_live"] = False
    st.cache_resource.clear()
    st.rerun()


@st.dialog("Force refresh odds?")
def confirm_refresh_dialog() -> None:
    remaining = oddsApi.MONTHLY_QUOTA - oddsApi.requests_used_this_month()
    st.write(f"This spends a request from your monthly quota ({remaining} remaining).")
    c1, c2 = st.columns(2)
    if c1.button("Refresh", type="primary", width="stretch"):
        refresh_odds()
    if c2.button("Cancel", width="stretch"):
        st.rerun()


def get_week_games():
    try:
        games = _week_games()
    except apiConnect.NflDataError as exc:
        st.error(str(exc))
        return None
    if not games:
        st.warning(f"No unplayed games left on the {apiConnect.SEASON} schedule.")
        return None
    return games


def pick_game_day(key: str):
    week_games = get_week_games()
    if week_games is None:
        return None
    by_day = apiConnect.games_by_day(week_games)
    st.subheader(f"Week {week_games[0].week}")
    day = st.radio("Game day", list(by_day), horizontal=True, key=key)
    return day, by_day[day]


def get_candidates(matchups: set[str], label: str):
    """Returns (candidates, covered_matchups) or None (after showing why)."""
    st.session_state["blocked_request"] = None
    try:
        candidates, covered, roster_ok = _build_candidates(tuple(sorted(matchups)))
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return None
    finally:
        st.session_state["allow_live"] = False

    if not covered:
        st.warning(
            f"The cached odds don't include any {label} games yet. "
            "Try a forced refresh on the Odds report page."
        )
        return None
    missing = matchups - covered
    if missing:
        st.warning(f"No odds yet for: {', '.join(sorted(missing))}")
    if not roster_ok:
        st.warning("Roster data unavailable; team/position filters disabled.")
    return candidates, covered


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def home_page() -> None:
    st.title(f" {HEADER}", text_alignment="center")
    st.info(INSTRUCTIONS)
    if SPLASH_IMAGE:
        with st.expander("🏆", expanded=False):
            st.code(SPLASH_IMAGE, language=None)
    st.write("Use the sidebar to open the odds report, search a player, browse the week's games, "
             "or manage your team.")


def odds_page() -> None:
    st.header("Odds analysis")
    top = st.columns([1, 3])
    if top[0].button("Force refresh (spends a request)"):
        confirm_refresh_dialog()

    odds = load_odds()
    if odds is None:
        return
    if odds.age_seconds is not None:
        st.caption(f"Odds data is {odds.age_seconds / 3600:.1f} hours old ({odds.fixture_count} fixtures).")

    tabs = st.tabs(["Games", "Projections", "TD scorers", "Skew"])
    with tabs[0]:
        show_report(display.game_report, odds.games)
        with st.expander("Raw data"):
            show_table(odds.games)
    with tabs[1]:
        show_report(display.projection_report, odds.props)
        with st.expander("Raw data"):
            show_table(odds.props)
    with tabs[2]:
        show_report(display.td_report, odds.props)
    with tabs[3]:
        show_report(display.skew_report, odds.props)


def player_search_page() -> None:
    st.header("Player search")
    week_games = get_week_games()
    if not week_games:
        return
    built = get_candidates({g.matchup for g in week_games}, label=f"week {week_games[0].week}")
    if built is None:
        return
    candidates, _ = built
    kickoffs = {g.matchup: f"{g.weekday} {g.gametime} ET" for g in week_games}

    query = st.text_input("Player name", placeholder="e.g. mahomes").strip()
    if not query:
        st.caption(f"{len(candidates)} players with props this week.")
        return

    hits = analysis.search_players(candidates, query)
    if not hits:
        st.warning(f"No player with props matches '{query}' this week.")
        return
    if len(hits) > 1:
        labels = [f"{c.player} ({c.matchup})" for c in hits]
        pick = st.selectbox("Multiple matches", ["Show all", *labels])
        if pick != "Show all":
            hits = [hits[labels.index(pick)]]
    for c in hits:
        show_report(display.player_card, c, kickoffs.get(c.matchup, ""))


def schedule_page() -> None:
    st.header("Upcoming games")
    picked = pick_game_day("schedule_day")
    if picked is None:
        return
    day, games = picked
    st.caption(f"{len(games)} game(s) on {day}")
    show_table(games)


def add_drop_page() -> None:
    st.header("Gameday search")
    picked = pick_game_day("adddrop_day")
    if picked is None:
        return
    day, slate = picked
    built = get_candidates({g.matchup for g in slate}, label=day)
    if built is None:
        return
    candidates, covered = built
    st.caption(f"{len(candidates)} players with props across {len(covered)} {day} game(s).")

    c1, c2, c3 = st.columns(3)
    position = c1.selectbox("Position", ["Any", "QB", "RB", "WR", "TE"])
    team = c2.text_input("Team abbreviation", placeholder="ex: KC")
    name = c3.text_input("Player name contains")

    position = None if position == "Any" else position
    team = team.strip().upper() or None
    name = name.strip() or None

    adds, drops = analysis.select_add_drop(candidates, position, team, name)
    show_report(display.add_drop_report, adds, drops, day, analysis.describe_filters(position, team, name))


def team_page() -> None:
    st.header("My team")
    upload = st.file_uploader("Upload team (JSON)", type="json")
    if upload is not None:
        try:
            data = json.load(upload)
        except json.JSONDecodeError as exc:
            st.error(f"That file isn't valid JSON: {exc}")
        else:
            TEAM_FILE.parent.mkdir(parents=True, exist_ok=True)
            TEAM_FILE.write_text(json.dumps(data, indent=2))
            st.success("Team saved.")

    if not TEAM_FILE.exists():
        st.info("No team uploaded yet.")
        return
    team = json.loads(TEAM_FILE.read_text())
    players = team.get("players", team) if isinstance(team, dict) else team
    if isinstance(players, list) and players and all(isinstance(p, dict) for p in players):
        st.dataframe(pd.DataFrame(players), width="stretch", hide_index=True)
    elif isinstance(players, list):
        st.dataframe(pd.DataFrame({"player": players}), width="stretch", hide_index=True)
    else:
        st.json(team)


def help_page() -> None:
    st.header("Help")
    st.info(INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    oddsApi.confirm_request = confirm_live_request
    st.session_state.setdefault("allow_live", False)
    st.session_state.setdefault("blocked_request", None)

    pages = {
        "": [st.Page(home_page, title="Home", default=True)],
        "Analysis": [
            st.Page(odds_page, title="Odds report"),
            st.Page(player_search_page, title="Player search"),
        ],
        "Games": [
            st.Page(schedule_page, title="Schedule"),
            st.Page(add_drop_page, title="Add / drop"),
        ],
        "Team": [
            st.Page(team_page, title="My team"),
            st.Page(help_page, title="Help"),
        ],
    }
    sidebar_quota()
    st.navigation(pages).run()


main()
