"""Turn cached OddsPapi NFL fixtures into fantasy-football signals.

Runs entirely on data already on disk - no API calls here.

What we extract (Pinnacle is a sharp book, so its lines double as projections):
  * Game environment  - moneyline, main spread, main total -> blowout /
                        shootout / low-scoring / coin-flip labels.
  * Book projections  - each player's MAIN prop line (rec yds, rush yds,
                        receptions, pass yds, TD passes) = market projection.
  * Skewed lines      - main lines where Over/Under prices are far from even,
                        meaning the book strongly expects one side.
  * TD probability    - implied chance of scoring from the anytime-TD market.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
BLOWOUT_SPREAD = 9.5      # |spread| at/above this -> starters may rest late
COIN_FLIP_SPREAD = 2.5    # |spread| at/below this -> full-game usage both sides
SHOOTOUT_TOTAL = 50.0
LOW_SCORING_TOTAL = 40.5
SKEW_THRESHOLD = 0.08     # |over_prob - under_prob| on a main line worth flagging

# OddsPapi marketType values we care about (sportId 14)
MONEYLINE = "moneyline"
SPREADS = "spreads"
TOTALS = "totals"
FULL_GAME = "result"

PROP_LABELS = {
    "playertotals-receivingyards": "Rec Yds",
    "playertotals-receptions": "Receptions",
    "playertotals-rushyards": "Rush Yds",
    "playertotals-rushattempts": "Rush Att",
    "playertotals-passyards": "Pass Yds",
    "playertotals-tdpasses": "Pass TDs",
    "playertotals-passattempts": "Pass Att",
    "playertotals-passcompletions": "Completions",
    "playertotals-interceptions": "INTs",
    "playertotals-td": "Anytime TD",
}
# Which prop types make a good "projection" leaderboard, in display order.
PROJECTION_MARKETS = [
    "playertotals-passyards",
    "playertotals-tdpasses",
    "playertotals-rushyards",
    "playertotals-receivingyards",
    "playertotals-receptions",
]
ANYTIME_TD = "playertotals-td"

# Half-PPR-ish scoring used to turn book lines into a single projection number.
FANTASY_WEIGHTS = {
    "playertotals-receivingyards": 0.1,
    "playertotals-rushyards": 0.1,
    "playertotals-receptions": 1.0,
    "playertotals-passyards": 0.04,
    "playertotals-tdpasses": 4.0,
}
TD_POINTS = 6.0

# Score nudges for the add/drop search (in fantasy-point-equivalents).
BONUS_SHOOTOUT = 2.0
BONUS_COIN_FLIP = 1.0
BONUS_GARBAGE_TIME = 1.0      # underdog pass-catcher in a projected blowout
BONUS_LEAN_OVER = 1.5
PENALTY_LOW_SCORING = -2.0
PENALTY_BLOWOUT_FAVORITE = -2.0
PENALTY_LEAN_UNDER = -1.5


# --------------------------------------------------------------------------- #
# Market definitions (from /markets, filtered to one sport)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketDef:
    market_id: str
    name: str
    market_type: str
    period: str
    handicap: float
    player_prop: bool
    outcome_names: dict[str, str]  # outcomeId -> "1" | "2" | "Over" | "Under"


def build_market_defs(raw_markets: list[dict[str, Any]], sport_id: int) -> dict[str, MarketDef]:
    defs: dict[str, MarketDef] = {}
    for m in raw_markets:
        if m.get("sportId") != sport_id:
            continue
        mid = str(m["marketId"])
        defs[mid] = MarketDef(
            market_id=mid,
            name=str(m.get("marketName", mid)),
            market_type=str(m.get("marketType", "")),
            period=str(m.get("period", "")),
            handicap=float(m.get("handicap") or 0.0),
            player_prop=bool(m.get("playerProp", False)),
            outcome_names={
                str(o["outcomeId"]): str(o.get("outcomeName", "")) for o in m.get("outcomes", [])
            },
        )
    return defs


# --------------------------------------------------------------------------- #
# Odds math
# --------------------------------------------------------------------------- #
def implied_probability(decimal_price: float) -> float:
    return 1.0 / decimal_price if decimal_price and decimal_price > 1.0 else 0.0


def format_american(raw: Any) -> str:
    """Normalize an American price so favorites read '-128' and dogs '+108'.

    OddsPapi omits the '+' on positive prices; add it so the sign is explicit.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text[0] in "+-":
        return text
    try:
        return f"{int(float(text)):+d}"
    except ValueError:
        return text


def _team(participants: dict[int, str], pid: Any) -> str:
    try:
        return participants.get(int(pid), f"team {pid}")
    except (TypeError, ValueError):
        return f"team {pid}"


# --------------------------------------------------------------------------- #
# Flatten one bookmaker's odds into rows we can reason about
# --------------------------------------------------------------------------- #
@dataclass
class OddsRow:
    fixture_id: str
    market: MarketDef
    outcome_id: str
    outcome_name: str
    player: str | None
    price: float          # decimal
    price_american: str
    main_line: bool


def _iter_rows(fixture: dict[str, Any], defs: dict[str, MarketDef], bookmaker: str):
    markets = fixture.get("bookmakerOdds", {}).get(bookmaker, {}).get("markets", {})
    for mid, market in markets.items():
        mdef = defs.get(str(mid))
        if mdef is None:
            continue
        for oid, outcome in market.get("outcomes", {}).items():
            for entry in (outcome.get("players") or {}).values():
                price = entry.get("price")
                if price is None or not entry.get("active", True):
                    continue
                yield OddsRow(
                    fixture_id=str(fixture.get("fixtureId")),
                    market=mdef,
                    outcome_id=str(oid),
                    outcome_name=mdef.outcome_names.get(str(oid), "?"),
                    player=entry.get("playerName") or None,
                    price=float(price),
                    price_american=format_american(entry.get("priceAmerican", "")),
                    main_line=bool(entry.get("mainLine", False)),
                )


# --------------------------------------------------------------------------- #
# Game environment
# --------------------------------------------------------------------------- #
@dataclass
class GameSignal:
    fixture_id: str
    start_time: str
    home: str
    away: str
    home_prob: float = 0.0
    away_prob: float = 0.0
    home_spread: float | None = None   # negative = home favored
    total: float | None = None

    @property
    def favorite(self) -> str:
        return self.home if self.home_prob >= self.away_prob else self.away

    @property
    def underdog(self) -> str:
        return self.away if self.home_prob >= self.away_prob else self.home

    @property
    def matchup(self) -> str:
        return f"{self.away} @ {self.home}"

    def is_favorite(self, team: str) -> bool:
        return team == self.favorite and self.home_prob != self.away_prob

    @property
    def labels(self) -> list[str]:
        out: list[str] = []
        if self.home_spread is not None:
            if abs(self.home_spread) >= BLOWOUT_SPREAD:
                out.append("BLOWOUT RISK")
            elif abs(self.home_spread) <= COIN_FLIP_SPREAD:
                out.append("COIN FLIP")
        if self.total is not None:
            if self.total >= SHOOTOUT_TOTAL:
                out.append("SHOOTOUT")
            elif self.total <= LOW_SCORING_TOTAL:
                out.append("LOW SCORING")
        return out


def _pick_main(rows: list[OddsRow]) -> list[OddsRow]:
    """Prefer rows flagged mainLine; otherwise the line priced closest to even."""
    flagged = [r for r in rows if r.main_line]
    if flagged:
        return flagged
    by_line: dict[float, list[OddsRow]] = defaultdict(list)
    for r in rows:
        by_line[r.market.handicap].append(r)
    best = min(
        by_line.values(),
        key=lambda rs: abs(max(r.price for r in rs) - min(r.price for r in rs)),
        default=[],
    )
    return best


def extract_game_signals(
    fixtures: list[dict[str, Any]],
    participants: dict[int, str],
    defs: dict[str, MarketDef],
    bookmaker: str,
) -> list[GameSignal]:
    signals: list[GameSignal] = []
    for fx in fixtures:
        sig = GameSignal(
            fixture_id=str(fx.get("fixtureId")),
            start_time=str(fx.get("startTime", "")),
            home=_team(participants, fx.get("participant1Id")),
            away=_team(participants, fx.get("participant2Id")),
        )
        rows = [r for r in _iter_rows(fx, defs, bookmaker) if r.player is None]
        full = [r for r in rows if r.market.period == FULL_GAME]

        for r in full:
            if r.market.market_type == MONEYLINE:
                if r.outcome_name == "1":
                    sig.home_prob = implied_probability(r.price)
                elif r.outcome_name == "2":
                    sig.away_prob = implied_probability(r.price)

        spread_rows = _pick_main([r for r in full if r.market.market_type == SPREADS])
        if spread_rows:
            sig.home_spread = spread_rows[0].market.handicap

        total_rows = _pick_main([r for r in full if r.market.market_type == TOTALS])
        if total_rows:
            sig.total = total_rows[0].market.handicap

        if sig.home_prob or sig.home_spread is not None:
            signals.append(sig)

    return sorted(signals, key=lambda s: abs(s.home_spread or 0.0), reverse=True)


# --------------------------------------------------------------------------- #
# Player props
# --------------------------------------------------------------------------- #
@dataclass
class BookPrice:
    """One bookmaker's main line for a prop, kept for cross-book comparison."""

    bookmaker: str
    line: float
    over_price: float | None = None
    under_price: float | None = None
    over_american: str = ""
    under_american: str = ""


@dataclass
class PropLine:
    fixture_id: str
    matchup: str
    player: str
    market_type: str
    line: float
    over_price: float | None = None
    under_price: float | None = None
    over_american: str = ""
    under_american: str = ""
    main_line: bool = False
    bookmaker: str = ""
    # Other books' main lines for the same player/market (excludes `bookmaker`).
    other_books: list[BookPrice] = field(default_factory=list)

    @property
    def all_books(self) -> list[BookPrice]:
        primary = BookPrice(
            self.bookmaker, self.line, self.over_price, self.under_price,
            self.over_american, self.under_american,
        )
        return [primary, *self.other_books]

    def best_over(self) -> BookPrice | None:
        """Book paying the most on the Over at the *same* line as the primary."""
        same = [b for b in self.all_books if b.line == self.line and b.over_price]
        return max(same, key=lambda b: b.over_price or 0.0, default=None)

    def best_under(self) -> BookPrice | None:
        same = [b for b in self.all_books if b.line == self.line and b.under_price]
        return max(same, key=lambda b: b.under_price or 0.0, default=None)

    @property
    def line_spread(self) -> float:
        """Max - min main line across books; >0 means the books disagree on the number."""
        lines = [b.line for b in self.all_books]
        return max(lines) - min(lines) if len(lines) > 1 else 0.0

    @property
    def label(self) -> str:
        return PROP_LABELS.get(self.market_type, self.market_type)

    @property
    def over_prob(self) -> float:
        return implied_probability(self.over_price or 0.0)

    @property
    def under_prob(self) -> float:
        return implied_probability(self.under_price or 0.0)

    @property
    def lean(self) -> float:
        """Positive = book leans Over, negative = leans Under."""
        if not self.over_price or not self.under_price:
            return 0.0
        return self.over_prob - self.under_prob

    @property
    def td_probability(self) -> float:
        """For the anytime-TD market: vig-removed chance of scoring."""
        total = self.over_prob + self.under_prob
        return self.over_prob / total if total else self.over_prob


def extract_prop_lines(
    fixtures: list[dict[str, Any]],
    participants: dict[int, str],
    defs: dict[str, MarketDef],
    bookmaker: str,
) -> list[PropLine]:
    """One PropLine per (fixture, player, market type), using the main line."""
    grouped: dict[tuple[str, str, str], list[OddsRow]] = defaultdict(list)
    matchups: dict[str, str] = {}

    for fx in fixtures:
        fid = str(fx.get("fixtureId"))
        matchups[fid] = (
            f"{_team(participants, fx.get('participant2Id'))} @ "
            f"{_team(participants, fx.get('participant1Id'))}"
        )
        for r in _iter_rows(fx, defs, bookmaker):
            if r.player and r.market.player_prop:
                grouped[(fid, r.player, r.market.market_type)].append(r)

    lines: list[PropLine] = []
    for (fid, player, mtype), rows in grouped.items():
        main = _pick_main(rows)
        if not main:
            continue
        pl = PropLine(
            fixture_id=fid,
            matchup=matchups.get(fid, ""),
            player=player,
            market_type=mtype,
            line=main[0].market.handicap,
            main_line=main[0].main_line,
            bookmaker=bookmaker,
        )
        for r in main:
            if r.outcome_name == "Over":
                pl.over_price, pl.over_american = r.price, r.price_american
            elif r.outcome_name == "Under":
                pl.under_price, pl.under_american = r.price, r.price_american
        lines.append(pl)
    return lines


def extract_prop_lines_multi(
    fixtures: list[dict[str, Any]],
    participants: dict[int, str],
    defs: dict[str, MarketDef],
    bookmakers: list[str],
) -> list[PropLine]:
    """PropLines from the first (primary) bookmaker, annotated with every other
    book's main line for the same player/market.

    Players/markets the primary book doesn't offer are still included (taken
    from the first other book that has them) so nothing disappears just
    because Pinnacle skipped it.
    """
    if not bookmakers:
        return []
    per_book = {b: extract_prop_lines(fixtures, participants, defs, b) for b in bookmakers}

    def key(p: PropLine) -> tuple[str, str, str]:
        return (p.fixture_id, p.player, p.market_type)

    merged: dict[tuple[str, str, str], PropLine] = {}
    for book in bookmakers:
        for p in per_book[book]:
            k = key(p)
            if k not in merged:
                merged[k] = p
            elif book != merged[k].bookmaker:
                merged[k].other_books.append(
                    BookPrice(book, p.line, p.over_price, p.under_price, p.over_american, p.under_american)
                )
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Report selections (rendering lives in display.py)
# --------------------------------------------------------------------------- #
def top_projections(props: list[PropLine], market_type: str, limit: int = 12) -> list[PropLine]:
    return sorted(
        (p for p in props if p.market_type == market_type), key=lambda p: p.line, reverse=True
    )[:limit]


def top_td_scorers(props: list[PropLine], limit: int = 20) -> list[PropLine]:
    return sorted(
        (p for p in props if p.market_type == ANYTIME_TD and p.over_price),
        key=lambda p: p.td_probability,
        reverse=True,
    )[:limit]


def skewed_lines(props: list[PropLine], limit: int = 20) -> list[PropLine]:
    """Main lines where the book is clearly leaning one way - the 'disparities'."""
    return sorted(
        (p for p in props if p.market_type != ANYTIME_TD and abs(p.lean) >= SKEW_THRESHOLD),
        key=lambda p: abs(p.lean),
        reverse=True,
    )[:limit]


def line_disagreements(props: list[PropLine], limit: int = 30) -> list[PropLine]:
    """Props where at least one other book posts a different main line, sorted
    by how far apart the books are. Ties broken by primary-book line size."""
    multi = [p for p in props if p.other_books and p.market_type != ANYTIME_TD]
    return sorted(
        (p for p in multi if p.line_spread > 0),
        key=lambda p: (p.line_spread, p.line),
        reverse=True,
    )[:limit]


# --------------------------------------------------------------------------- #
# Add / drop search for a slate of games
# --------------------------------------------------------------------------- #
@dataclass
class PlayerCandidate:
    """Everything the book says about one player in one game, rolled into a score."""

    player: str
    matchup: str
    team: str = ""
    position: str = ""
    props: dict[str, PropLine] = field(default_factory=dict)
    game: GameSignal | None = None
    reasons: list[str] = field(default_factory=list)
    adjustment: float = 0.0

    @property
    def projection(self) -> float:
        """Fantasy points implied by the book's main lines (half-PPR, 6 pt TD)."""
        points = sum(
            p.line * FANTASY_WEIGHTS[m] for m, p in self.props.items() if m in FANTASY_WEIGHTS
        )
        td = self.props.get(ANYTIME_TD)
        if td and td.over_price:
            points += td.td_probability * TD_POINTS
        return points

    @property
    def score(self) -> float:
        return self.projection + self.adjustment

    @property
    def td_probability(self) -> float | None:
        td = self.props.get(ANYTIME_TD)
        return td.td_probability if td and td.over_price else None


def _apply_game_context(cand: PlayerCandidate) -> None:
    game = cand.game
    if game is None:
        return
    labels = game.labels
    if "SHOOTOUT" in labels:
        cand.adjustment += BONUS_SHOOTOUT
        cand.reasons.append(f"shootout (O/U {game.total:g})")
    if "COIN FLIP" in labels:
        cand.adjustment += BONUS_COIN_FLIP
        cand.reasons.append("coin flip - full-game usage")
    if "LOW SCORING" in labels:
        cand.adjustment += PENALTY_LOW_SCORING
        cand.reasons.append(f"low-scoring game (O/U {game.total:g})")
    if "BLOWOUT RISK" in labels and cand.team:
        if game.is_favorite(cand.team):
            cand.adjustment += PENALTY_BLOWOUT_FAVORITE
            cand.reasons.append("heavy favorite - may rest late")
        elif cand.position in ("WR", "TE", "QB"):
            cand.adjustment += BONUS_GARBAGE_TIME
            cand.reasons.append("underdog passing volume / garbage time")


def _apply_line_leans(cand: PlayerCandidate) -> None:
    for m, p in cand.props.items():
        if m == ANYTIME_TD:
            continue
        if p.lean >= SKEW_THRESHOLD:
            cand.adjustment += BONUS_LEAN_OVER
            cand.reasons.append(f"book leans OVER {p.label} {p.line:g}")
        elif p.lean <= -SKEW_THRESHOLD:
            cand.adjustment += PENALTY_LEAN_UNDER
            cand.reasons.append(f"book leans UNDER {p.label} {p.line:g}")


def build_candidates(
    props: list[PropLine],
    games: list[GameSignal],
    matchups: set[str],
    lookup: Callable[[str], tuple[str, str] | None] | None = None,
) -> list[PlayerCandidate]:
    """Roll props + game environment into one PlayerCandidate per player.

    `matchups` are "Away @ Home" strings (full team names) for the games to
    include. `lookup(player_name)` may return (team_abbr, position) from a
    roster source so blowout logic knows which side the player is on.
    """
    game_by_matchup = {g.matchup: g for g in games}
    by_player: dict[tuple[str, str], PlayerCandidate] = {}

    for p in props:
        if p.matchup not in matchups:
            continue
        key = (p.fixture_id, p.player)
        cand = by_player.get(key)
        if cand is None:
            cand = PlayerCandidate(
                player=p.player, matchup=p.matchup, game=game_by_matchup.get(p.matchup)
            )
            if lookup is not None:
                info = lookup(p.player)
                if info:
                    cand.team, cand.position = info
            by_player[key] = cand
        cand.props[p.market_type] = p

    for cand in by_player.values():
        _apply_game_context(cand)
        _apply_line_leans(cand)
    return list(by_player.values())


def _matches(cand: PlayerCandidate, position: str | None, team: str | None, name: str | None) -> bool:
    if position and cand.position != position:
        return False
    if team and cand.team != team:
        return False
    if name and name.lower() not in cand.player.lower():
        return False
    return True


DROP_SCORE_CEILING = 8.0   # below this many implied points a player is a fade regardless of script


def select_add_drop(
    candidates: list[PlayerCandidate],
    position: str | None = None,
    team: str | None = None,
    name: str | None = None,
    limit: int = 15,
) -> tuple[list[PlayerCandidate], list[PlayerCandidate]]:
    """Filter candidates, then split into (adds, drops).

    Adds: highest score. Drops: players the book expects little from or whose
    game script works against them; single-prop players are ignored as noise.
    """
    pool = [c for c in candidates if _matches(c, position, team, name)]
    adds = sorted(pool, key=lambda c: c.score, reverse=True)[:limit]
    added = {id(c) for c in adds}
    drops = sorted(
        (
            c
            for c in pool
            if id(c) not in added
            and len(c.props) >= 2
            and (c.adjustment < 0 or c.score < DROP_SCORE_CEILING)
        ),
        key=lambda c: (c.adjustment, c.score),
    )[:limit]
    return adds, drops


def search_players(candidates: list[PlayerCandidate], query: str) -> list[PlayerCandidate]:
    """Case-insensitive match on player name; every query word must appear.

    Works on "First Last" or "Last, First" names and on partial names
    ("chase", "ja'marr", "lamb dal" also matches team abbreviations).
    """
    words = [w for w in query.lower().replace(",", " ").split() if w]
    if not words:
        return []
    hits: list[PlayerCandidate] = []
    for c in candidates:
        haystack = f"{c.player} {c.team} {c.position}".lower().replace(",", " ")
        if all(w in haystack for w in words):
            hits.append(c)
    return sorted(hits, key=lambda c: c.score, reverse=True)


def describe_filters(position: str | None, team: str | None, name: str | None) -> str:
    return ", ".join(
        f for f in (position and f"pos={position}", team and f"team={team}", name and f"name~{name}")
        if f
    )
