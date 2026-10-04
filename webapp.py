"""WinnerMaker - Flask front end.

Run with:  python main.py   (or: flask --app webapp run)

A traditional server-rendered app: every request renders complete HTML with
its styling already baked in, so there's no "first paint before the page has
settled" moment like a JS-driven single-page app has - the class of bug where
formatting looks right after some client-side event but resets on a hard
refresh simply can't happen here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from flask import Flask, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import analysis
import apiConnect
import oddsApi

app = Flask(__name__)
app.secret_key = "winnermaker-dev-secret"  # local single-user app; no real sessions to protect

HEADER = "The WinnerMaker"
INSTRUCTIONS = (
    "Paste your roster as a plain list of player names, one per line. "
    "WinnerMaker will use this and flag specific events for these players."
)
BOOKMAKER = "pinnacle"
TEAM_FILE = Path("data/team.json")

ODDS_ERRORS = (oddsApi.OddsApiError, KeyError, TypeError, ValueError)


# --------------------------------------------------------------------------- #
# Formatting helpers shared by templates
# --------------------------------------------------------------------------- #
def tag_class(label: str) -> str:
    return {
        "BLOWOUT RISK": "tag tag-blowout",
        "COIN FLIP": "tag tag-coinflip",
        "SHOOTOUT": "tag tag-shootout",
        "LOW SCORING": "tag tag-lowscoring",
    }.get(label, "tag tag-none")


def price_class(price: str) -> str:
    if not price or price in ("-", "n/a"):
        return "price-na"
    return "price-pos" if price.startswith("+") else "price-neg"


def lean_text(value: float) -> str:
    if value >= analysis.SKEW_THRESHOLD:
        return f"OVER {value:.0%}"
    if value <= -analysis.SKEW_THRESHOLD:
        return f"UNDER {abs(value):.0%}"
    return "-"


def lean_class(text: str) -> str:
    if text.startswith("OVER"):
        return "lean-over"
    if text.startswith("UNDER"):
        return "lean-under"
    return "lean-none"


def adj_class(value: float) -> str:
    if value > 0:
        return "adj-pos"
    if value < 0:
        return "adj-neg"
    return "adj-none"


def reason_class(reason: str) -> str:
    lowered = reason.lower()
    if "under" in lowered or "low-scoring" in lowered or "rest" in lowered:
        return "reason-neg"
    return "reason-pos"


app.template_filter("tag_class")(tag_class)
app.template_filter("price_class")(price_class)
app.template_filter("lean_text")(lean_text)
app.template_filter("lean_class")(lean_class)
app.template_filter("adj_class")(adj_class)
app.template_filter("reason_class")(reason_class)


@app.template_filter("short_matchup")
def short_matchup(matchup: str) -> str:
    """'Washington Commanders @ Dallas Cowboys' -> 'Commanders @ Cowboys'."""
    if " @ " not in matchup:
        return matchup
    away, home = matchup.split(" @ ", 1)
    return f"{away.split()[-1]} @ {home.split()[-1]}"


app.template_filter("display_player_name")(apiConnect.display_player_name)


@app.template_filter("pos_class")
def pos_class(position: str) -> str:
    return f"pos-{position}" if position else ""


jinja_globals = cast("dict[str, Any]", app.jinja_env.globals)
jinja_globals.update(
    PROJECTION_MARKETS=analysis.PROJECTION_MARKETS,
    PROP_LABELS=analysis.PROP_LABELS,
    ANYTIME_TD=analysis.ANYTIME_TD,
)


def lines_text(c: analysis.PlayerCandidate) -> str:
    parts = []
    for m in analysis.PROJECTION_MARKETS:
        if m in c.props:
            parts.append(f"{analysis.PROP_LABELS[m]} {c.props[m].line:g}")
    if c.td_probability is not None:
        parts.append(f"TD {c.td_probability:.0%}")
    return "  ".join(parts) if parts else "-"


app.template_filter("lines_text")(lines_text)


def verdict_text(c: analysis.PlayerCandidate) -> str:
    if c.score >= 15:
        return "MUST START"
    if c.score >= 10:
        return "START"
    if c.score >= analysis.DROP_SCORE_CEILING:
        return "FLEX / BENCH"
    return "FADE / DROP"


def verdict_class(text: str) -> str:
    return {
        "MUST START": "verdict verdict-must-start",
        "START": "verdict verdict-start",
        "FLEX / BENCH": "verdict verdict-flex",
        "FADE / DROP": "verdict verdict-fade",
    }.get(text, "verdict")


def ordered_props(c: analysis.PlayerCandidate) -> list[analysis.PropLine]:
    """Projection markets first (in display order), then any other non-TD markets."""
    ordered = [c.props[m] for m in analysis.PROJECTION_MARKETS if m in c.props]
    extra = sorted(
        m for m in c.props if m not in analysis.PROJECTION_MARKETS and m != analysis.ANYTIME_TD
    )
    ordered += [c.props[m] for m in extra]
    return ordered


app.template_filter("verdict_text")(verdict_text)
app.template_filter("verdict_class")(verdict_class)
app.template_filter("ordered_props")(ordered_props)


def game_rows(signals: list[analysis.GameSignal]) -> list[dict[str, Any]]:
    rows = []
    for s in signals:
        fav_prob = max(s.home_prob, s.away_prob)
        labels = s.labels
        spread_tag = next((l for l in labels if l in ("BLOWOUT RISK", "COIN FLIP")), "-")
        total_tag = next((l for l in labels if l in ("SHOOTOUT", "LOW SCORING")), "-")
        rows.append(
            {
                "date": s.start_time[:10],
                "away": s.away,
                "home": s.home,
                "home_spread": f"{s.home_spread:+.1f}" if s.home_spread is not None else "-",
                "total": f"{s.total:.1f}" if s.total is not None else "-",
                "favorite": s.favorite,
                "win_pct": (f"{fav_prob:.0%}" if fav_prob else "-"),
                "spread_tag": spread_tag,
                "total_tag": total_tag,
            }
        )
    return rows


def projection_rows(props: list[analysis.PropLine], per_market: int = 12) -> list[dict[str, Any]]:
    rows = []
    for mtype in analysis.PROJECTION_MARKETS:
        for p in analysis.top_projections(props, mtype, per_market):
            rows.append(
                {
                    "market": analysis.PROP_LABELS[mtype],
                    "player": apiConnect.display_player_name(p.player),
                    "line": f"{p.line:.1f}",
                    "over": p.over_american or "-",
                    "under": p.under_american or "-",
                    "lean": lean_text(p.lean),
                    "matchup": p.matchup,
                }
            )
    return rows


def td_scorer_rows(props: list[analysis.PropLine], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "rank": i,
            "player": apiConnect.display_player_name(p.player),
            "td_pct": f"{p.td_probability:.0%}",
            "price": p.over_american or "-",
            "matchup": p.matchup,
        }
        for i, p in enumerate(analysis.top_td_scorers(props, limit), 1)
    ]


def skew_rows(props: list[analysis.PropLine], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "player": apiConnect.display_player_name(p.player),
            "market": p.label,
            "line": f"{p.line:.1f}",
            "lean": lean_text(p.lean),
            "over": p.over_american or "-",
            "under": p.under_american or "-",
            "matchup": p.matchup,
        }
        for p in analysis.skewed_lines(props, limit)
    ]


# --------------------------------------------------------------------------- #
# Live-request confirmation (replaces the input() prompt)
# --------------------------------------------------------------------------- #
def confirm_live_request(path: str, remaining: int) -> bool:
    """Called by oddsApi before every request that would hit the network.

    A web request can't block for a click, so: approve only if the visitor
    has already confirmed via the "spend a request" form (a one-shot flag in
    the session), otherwise remember what was blocked so the page can offer
    that confirmation.
    """
    if session.get("allow_live"):
        return True
    session["blocked_request"] = {"path": path, "remaining": remaining}
    return False


oddsApi.confirm_request = confirm_live_request


def report_odds_error(exc: Exception) -> None:
    if isinstance(exc, oddsApi.OddsApiError):
        flash(str(exc), "error")
    else:
        flash(f"Unexpected response shape from OddsPapi: {exc!r}", "error")
        flash("Inspect the cached JSON in .cache/odds/ and adjust the field names.", "info")


def blocked_request_notice() -> dict[str, Any] | None:
    return session.get("blocked_request")


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
@dataclass
class OddsData:
    games: list[analysis.GameSignal]
    props: list[analysis.PropLine]
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


def load_odds() -> OddsData | None:
    session["blocked_request"] = None
    try:
        return _fetch_odds(force_refresh=False)
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return None
    finally:
        session["allow_live"] = False


def refresh_odds() -> bool:
    """Forced refresh: bypasses the on-disk cache and spends a request."""
    session["allow_live"] = True
    try:
        _fetch_odds(force_refresh=True)
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return False
    finally:
        session["allow_live"] = False
    return True


def week_games(week_offset: int = 0) -> list[apiConnect.ScheduledGame] | None:
    try:
        games = apiConnect.upcoming_week_games(week_offset=week_offset)
    except apiConnect.NflDataError as exc:
        flash(str(exc), "error")
        return None
    if not games:
        flash(f"No unplayed games left on the {apiConnect.SEASON} schedule.", "warning")
        return None
    return games


def default_day(by_day: dict[str, list[apiConnect.ScheduledGame]]) -> str | None:
    """The day with the most games - usually the main Sunday slate - rather
    than whichever day happens to come first chronologically (often a
    single-game Thursday night slate)."""
    if not by_day:
        return None
    return max(by_day, key=lambda d: len(by_day[d]))


def build_candidates(
    matchups: set[str],
) -> tuple[list[analysis.PlayerCandidate], set[str]] | None:
    """Returns (candidates, covered_matchups) or None (after flashing why)."""
    session["blocked_request"] = None
    try:
        odds = _fetch_odds(force_refresh=False)
    except ODDS_ERRORS as exc:
        report_odds_error(exc)
        return None
    finally:
        session["allow_live"] = False

    covered = {g.matchup for g in odds.games} & matchups
    if not covered:
        flash(
            "The cached odds don't include any of these games yet. "
            "Try a forced refresh on the Odds report page.",
            "warning",
        )
        return None

    try:
        index = apiConnect.build_player_index()
        roster_ok = True
    except apiConnect.NflDataError:
        index, roster_ok = {}, False

    def lookup(name: str) -> tuple[str, str] | None:
        info = apiConnect.lookup_player(index, name)
        return (info.team, info.position) if info else None

    candidates = analysis.build_candidates(odds.props, odds.games, matchups, lookup)
    for c in candidates:
        c.player = apiConnect.display_player_name(c.player)

    missing = matchups - covered
    if missing:
        flash(f"No odds yet for: {', '.join(sorted(missing))}", "warning")
    if not roster_ok:
        flash("Roster data unavailable; team/position filters disabled.", "warning")
    return candidates, covered


def quota_context() -> dict[str, Any]:
    auto_used = oddsApi.requests_used_this_month()
    adjustment = oddsApi.manual_adjustment_this_month()
    used = auto_used + adjustment
    total = oddsApi.MONTHLY_QUOTA
    return {
        "auto_used": auto_used,
        "adjustment": adjustment,
        "used": used,
        "total": total,
        "pct": min(used / total, 1.0) if total else 0.0,
        "untracked": oddsApi.find_untracked_cache_files(),
        "history": list(reversed(oddsApi.adjustment_history_this_month())),
    }


@app.context_processor
def inject_quota() -> dict[str, Any]:
    return {"quota": quota_context(), "blocked": blocked_request_notice()}


# --------------------------------------------------------------------------- #
# Routes - top-level
# --------------------------------------------------------------------------- #
@app.get("/")
def home() -> str:
    return render_template("home.html", header=HEADER, instructions=INSTRUCTIONS)


@app.get("/help")
def help_page() -> str:
    return render_template("help.html", instructions=INSTRUCTIONS)


@app.post("/spend-request")
def spend_request() -> Any:
    """Confirms the pending blocked request, then retries the page that asked for it."""
    session["allow_live"] = True
    next_url = request.form.get("next") or url_for("home")
    return redirect(next_url)


# --------------------------------------------------------------------------- #
# Quota sidebar actions
# --------------------------------------------------------------------------- #
@app.post("/quota/reconcile")
def quota_reconcile() -> Any:
    added = oddsApi.reconcile_untracked_requests()
    flash(f"Logged {added} untracked request(s).", "success")
    return redirect(request.referrer or url_for("home"))


@app.post("/quota/adjust")
def quota_adjust() -> Any:
    delta_raw = request.form.get("delta", "0")
    note = request.form.get("note", "")
    try:
        delta = int(delta_raw)
    except ValueError:
        delta = 0
    if delta:
        new_total = oddsApi.adjust_usage(delta, note)
        flash(f"Applied {delta:+d}. New total: {new_total} / {oddsApi.MONTHLY_QUOTA}.", "success")
    return redirect(request.referrer or url_for("home"))


# --------------------------------------------------------------------------- #
# Odds report
# --------------------------------------------------------------------------- #
@app.get("/odds")
def odds_page() -> str:
    tab = request.args.get("tab", "games")
    odds = load_odds()
    games = game_rows(odds.games) if odds else []
    projections = projection_rows(odds.props) if odds else []
    td_scorers = td_scorer_rows(odds.props) if odds else []
    skewed = skew_rows(odds.props) if odds else []
    return render_template(
        "odds.html",
        odds=odds,
        tab=tab,
        games=games,
        projections=projections,
        td_scorers=td_scorers,
        skewed=skewed,
    )


@app.post("/odds/refresh")
def odds_refresh() -> Any:
    if refresh_odds():
        flash("Odds refreshed.", "success")
    return redirect(url_for("odds_page"))


# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #
@app.get("/schedule")
def schedule_page() -> str:
    week_offset = int(request.args.get("week", 0))
    games = week_games(week_offset)
    if games is None:
        return render_template("schedule.html", games=None, weeks=[], week_offset=week_offset)

    try:
        weeks = apiConnect.upcoming_weeks()
    except apiConnect.NflDataError:
        weeks = []

    by_day = apiConnect.games_by_day(games)
    day = request.args.get("day") or default_day(by_day)
    if day not in by_day:
        day = default_day(by_day)

    return render_template(
        "schedule.html",
        games=games,
        weeks=weeks,
        week_offset=week_offset,
        by_day=by_day,
        day=day,
    )


# --------------------------------------------------------------------------- #
# Player search
# --------------------------------------------------------------------------- #
@app.get("/players")
def player_search_page() -> str:
    week_offset = int(request.args.get("week", 0))
    games = week_games(week_offset)
    if games is None:
        return render_template(
            "player_search.html", games=None, weeks=[], week_offset=week_offset, query=""
        )

    try:
        weeks = apiConnect.upcoming_weeks()
    except apiConnect.NflDataError:
        weeks = []

    built = build_candidates({g.matchup for g in games})
    if built is None:
        return render_template(
            "player_search.html",
            games=games,
            weeks=weeks,
            week_offset=week_offset,
            query="",
        )
    candidates, _covered = built
    kickoffs = {g.matchup: f"{g.weekday} {g.gametime_mst} MST" for g in games}

    query = request.args.get("q", "").strip()
    hits: list[analysis.PlayerCandidate] = []
    pick_index = request.args.get("pick")
    if query:
        hits = analysis.search_players(candidates, query)
        if not hits:
            flash(f"No player with props matches '{query}' this week.", "warning")
        elif pick_index is not None:
            try:
                idx = int(pick_index)
                if 0 <= idx < len(hits):
                    hits = [hits[idx]]
            except ValueError:
                pass

    return render_template(
        "player_search.html",
        games=games,
        weeks=weeks,
        week_offset=week_offset,
        query=query,
        candidate_count=len(candidates),
        hits=hits,
        kickoffs=kickoffs,
        pick_index=pick_index,
    )


# --------------------------------------------------------------------------- #
# Add / drop (gameday search)
# --------------------------------------------------------------------------- #
@app.get("/add-drop")
def add_drop_page() -> str:
    week_offset = int(request.args.get("week", 0))
    games = week_games(week_offset)
    if games is None:
        return render_template("add_drop.html", games=None, weeks=[], week_offset=week_offset)

    try:
        weeks = apiConnect.upcoming_weeks()
    except apiConnect.NflDataError:
        weeks = []

    by_day = apiConnect.games_by_day(games)
    day = request.args.get("day") or default_day(by_day)
    if day not in by_day:
        day = default_day(by_day)

    if day is None:
        return render_template(
            "add_drop.html", games=games, weeks=weeks, week_offset=week_offset, by_day=by_day
        )

    slate = by_day[day]
    built = build_candidates({g.matchup for g in slate})
    if built is None:
        return render_template(
            "add_drop.html",
            games=games,
            weeks=weeks,
            week_offset=week_offset,
            by_day=by_day,
            day=day,
        )
    candidates, covered = built

    position = request.args.get("position") or None
    team = (request.args.get("team") or "").strip().upper() or None
    name = (request.args.get("name") or "").strip() or None

    adds, drops = analysis.select_add_drop(candidates, position, team, name)
    filters = analysis.describe_filters(position, team, name)

    return render_template(
        "add_drop.html",
        games=games,
        weeks=weeks,
        week_offset=week_offset,
        by_day=by_day,
        day=day,
        candidate_count=len(candidates),
        covered_count=len(covered),
        adds=adds,
        drops=drops,
        filters=filters,
        position=position or "",
        team=team or "",
        name=name or "",
    )


# --------------------------------------------------------------------------- #
# My team
# --------------------------------------------------------------------------- #
@app.get("/team")
def team_page() -> str:
    team = None
    if TEAM_FILE.exists():
        try:
            team = json.loads(TEAM_FILE.read_text())
        except json.JSONDecodeError:
            team = None
    players = team.get("players", []) if isinstance(team, dict) else []
    roster_text = "\n".join(players)
    return render_template("team.html", players=players, roster_text=roster_text)


@app.post("/team/upload")
def team_upload() -> Any:
    raw_text = request.form.get("roster_text", "")
    players = [line.strip() for line in raw_text.splitlines() if line.strip()]
    # De-duplicate while preserving order.
    players = list(dict.fromkeys(players))
    if not players:
        flash("Enter at least one player name.", "warning")
        return redirect(url_for("team_page"))
    TEAM_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEAM_FILE.write_text(json.dumps({"players": players}, indent=2))
    flash(f"Team saved ({len(players)} player{'s' if len(players) != 1 else ''}).", "success")
    return redirect(url_for("team_page"))


# --------------------------------------------------------------------------- #
# Template helper: render nl2br-ish reasons list safely
# --------------------------------------------------------------------------- #
@app.template_filter("reasons_html")
def reasons_html(reasons: list[str]) -> Markup:
    if not reasons:
        return Markup('<span class="muted">-</span>')
    items = "".join(f'<li class="{reason_class(r)}">{escape(r)}</li>' for r in reasons)
    return Markup(f"<ul>{items}</ul>")


if __name__ == "__main__":
    app.run(debug=True)
