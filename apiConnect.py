"""NFL schedule + roster data via nfl_data_py (free, no quota).

Used to answer "which games are on <day>?" and to attach a team/position to
the player names that come back from the odds feed. Every download is cached
to disk so re-running the app is fast and works offline.
"""

import math
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import nfl_data_py as nfl
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / ".cache" / "nfl"
TTL_SCHEDULE = 6 * 60 * 60  # results fill in after games; refresh a few times a day
TTL_ROSTERS = 24 * 60 * 60
TTL_FOREVER = None

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
DAY_ORDER = ["Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday"]

_EASTERN = ZoneInfo("America/New_York")
_MST = timezone(timedelta(hours=-7))  # fixed Mountain Standard Time, no DST (e.g. Arizona)


def _eastern_to_mst(gameday: str, gametime: str) -> datetime:
    """Combine the schedule's Eastern gameday/gametime into an MST-aware datetime.

    `nfl_data_py` reports kickoff as US Eastern wall-clock time (EDT or EST
    depending on the date), so the conversion has to localize to
    `America/New_York` first to pick the right offset before shifting to the
    fixed UTC-7 MST offset. Late games can roll over to the next calendar day
    in MST, which is why this returns a full datetime rather than separate
    strings.
    """
    naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    eastern = naive.replace(tzinfo=_EASTERN)
    return eastern.astimezone(_MST)


class NflDataError(RuntimeError):
    """Raised when nfl_data_py cannot supply the requested dataset."""


def current_season(today: date | None = None) -> int:
    """NFL season label: a season that kicks off in September runs into the next February."""
    today = today or datetime.now().date()
    return today.year if today.month >= 3 else today.year - 1


SEASON = current_season()


# --------------------------------------------------------------------------- #
# Disk cache for DataFrames
# --------------------------------------------------------------------------- #
Record = dict[str, Any]


def _cached_frame(name: str, ttl: int | None, loader: Callable[[], Any]) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.parquet"
    if path.exists() and (ttl is None or time.time() - path.stat().st_mtime <= ttl):
        return pd.read_parquet(path)
    try:
        frame = cast(pd.DataFrame, loader())
    except Exception as exc:  # nfl_data_py raises urllib/HTTP errors for missing years
        if path.exists():
            return pd.read_parquet(path)
        raise NflDataError(f"Could not download '{name}' from nflverse: {exc!r}") from exc
    frame.to_parquet(path, index=False)
    return frame


def _records(frame: pd.DataFrame, columns: list[str]) -> list[Record]:
    """Plain dict rows for the given columns - easier to type-check than DataFrame ops."""
    series = [cast(list[Any], frame[col].tolist()) for col in columns]
    return [dict(zip(columns, values)) for values in zip(*series)]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


# --------------------------------------------------------------------------- #
# Raw datasets
# --------------------------------------------------------------------------- #
def get_schedule(season: int = SEASON) -> pd.DataFrame:
    """Game schedule for a season (gameday/gametime are US Eastern)."""
    return _cached_frame(
        f"schedule_{season}", TTL_SCHEDULE, lambda: nfl.import_schedules([season])
    )


def get_rosters(season: int = SEASON) -> pd.DataFrame:
    """Season rosters (player_name, team, position, status, ...)."""
    return _cached_frame(
        f"rosters_{season}", TTL_ROSTERS, lambda: nfl.import_seasonal_rosters([season])
    )


def get_team_names() -> dict[str, str]:
    """Team abbreviation -> full name (e.g. 'KC' -> 'Kansas City Chiefs')."""
    frame = _cached_frame("team_desc", TTL_FOREVER, nfl.import_team_desc)
    return {
        str(row["team_abbr"]): str(row["team_name"])
        for row in _records(frame, ["team_abbr", "team_name"])
    }


def get_weekly_stats(season: int = SEASON) -> pd.DataFrame:
    """Weekly fantasy-relevant player stats.

    nflverse publishes these with a lag, so the current season may 404 until
    the data exists; callers should catch NflDataError.
    """
    return _cached_frame(
        f"weekly_{season}",
        TTL_SCHEDULE,
        lambda: nfl.import_weekly_data(
            [season],
            columns=[
                "player_display_name",
                "position",
                "recent_team",
                "week",
                "fantasy_points",
                "fantasy_points_ppr",
            ],
        ),
    )


# --------------------------------------------------------------------------- #
# Upcoming games
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScheduledGame:
    game_id: str
    week: int
    gameday: str  # YYYY-MM-DD, Eastern
    weekday: str  # e.g. "Sunday", Eastern
    gametime: str  # HH:MM Eastern
    away: str  # abbreviation
    home: str
    away_name: str  # full name, matches OddsPapi participants
    home_name: str

    @property
    def matchup(self) -> str:
        return f"{self.away_name} @ {self.home_name}"

    @property
    def kickoff_mst(self) -> datetime:
        """Kickoff as an MST-aware datetime (fixed UTC-7, no DST)."""
        return _eastern_to_mst(self.gameday, self.gametime)

    @property
    def gameday_mst(self) -> str:
        return self.kickoff_mst.strftime("%Y-%m-%d")

    @property
    def weekday_mst(self) -> str:
        return self.kickoff_mst.strftime("%A")

    @property
    def gametime_mst(self) -> str:
        return self.kickoff_mst.strftime("%H:%M")


def _unplayed_games(season: int, today: date) -> list[Record]:
    columns = [
        "game_id",
        "week",
        "gameday",
        "weekday",
        "gametime",
        "away_team",
        "home_team",
        "result",
    ]
    return [
        row
        for row in _records(get_schedule(season), columns)
        if _is_missing(row["result"]) and str(row["gameday"]) >= today.isoformat()
    ]


def _games_for_week(rows: list[Record], week: int, names: dict[str, str]) -> list[ScheduledGame]:
    week_rows = sorted(
        (row for row in rows if int(row["week"]) == week),
        key=lambda row: (str(row["gameday"]), str(row["gametime"])),
    )
    games: list[ScheduledGame] = []
    for row in week_rows:
        away, home = str(row["away_team"]), str(row["home_team"])
        games.append(
            ScheduledGame(
                game_id=str(row["game_id"]),
                week=week,
                gameday=str(row["gameday"]),
                weekday=str(row["weekday"]),
                gametime=str(row["gametime"]),
                away=away,
                home=home,
                away_name=names.get(away, away),
                home_name=names.get(home, home),
            )
        )
    return games


def upcoming_weeks(season: int = SEASON, today: date | None = None) -> list[int]:
    """Every week number (ascending) that still has at least one unplayed game.

    Lets callers offer a "look ahead N weeks" choice without guessing how far
    out the published schedule goes.
    """
    today = today or datetime.now().date()
    unplayed = _unplayed_games(season, today)
    return sorted({int(row["week"]) for row in unplayed})


def upcoming_week_games(
    season: int = SEASON, today: date | None = None, week_offset: int = 0
) -> list[ScheduledGame]:
    """All games in the week `week_offset` weeks after the earliest unplayed week.

    A game counts as unplayed when it has no result yet and its date is today
    or later, so a Sunday slate stays available all day Sunday. `week_offset=0`
    (the default) is the current/next upcoming week; `week_offset=1` is the
    week after that, and so on, capped at the last week with scheduled games.
    """
    today = today or datetime.now().date()
    names = get_team_names()
    unplayed = _unplayed_games(season, today)
    if not unplayed:
        return []

    weeks = sorted({int(row["week"]) for row in unplayed})
    index = min(max(week_offset, 0), len(weeks) - 1)
    week = weeks[index]
    return _games_for_week(unplayed, week, names)


def games_by_day(games: list[ScheduledGame]) -> dict[str, list[ScheduledGame]]:
    """Group games by weekday, ordered Thursday -> Monday like an NFL week."""
    grouped: dict[str, list[ScheduledGame]] = {}
    for day in DAY_ORDER:
        todays = [g for g in games if g.weekday == day]
        if todays:
            grouped[day] = todays
    return grouped


# --------------------------------------------------------------------------- #
# Player name matching (odds feed uses "Last, First"; rosters use "First Last")
# --------------------------------------------------------------------------- #
_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")

# Odds feeds use formal first names; rosters often use the nickname (or vice versa).
_NICKNAMES = {
    "cameron": "cam",
    "kenneth": "kenny",
    "michael": "mike",
    "christopher": "chris",
    "joshua": "josh",
    "matthew": "matt",
    "anthony": "tony",
    "nicholas": "nick",
    "zachary": "zach",
    "alexander": "alex",
    "jonathan": "jon",
    "joseph": "joe",
    "daniel": "dan",
    "william": "will",
}


def normalize_player_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    if "," in text:
        last, first = (part.strip() for part in text.split(",", 1))
        text = f"{first} {last}"
    text = _SUFFIXES.sub("", text.lower())
    return " ".join(re.sub(r"[^a-z ]", "", text).split())


def display_player_name(name: str) -> str:
    """'Chase, Ja'Marr' -> 'Ja'Marr Chase'; names already in that order pass through."""
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        return f"{first} {last}"
    return name


@dataclass(frozen=True)
class PlayerInfo:
    team: str  # abbreviation
    position: str


def build_player_index(season: int = SEASON) -> dict[str, PlayerInfo]:
    """normalized name -> (team, position) for skill-position players.

    Active players win ties so a released veteran doesn't shadow a starter.
    """
    rows = _records(get_rosters(season), ["player_name", "team", "position", "status"])
    skill = [
        row
        for row in rows
        if row["position"] in SKILL_POSITIONS and not _is_missing(row["player_name"])
    ]
    skill.sort(key=lambda row: row["status"] != "ACT")
    index: dict[str, PlayerInfo] = {}
    for row in skill:
        key = normalize_player_name(str(row["player_name"]))
        if key not in index:
            index[key] = PlayerInfo(team=str(row["team"]), position=str(row["position"]))
    return index


def lookup_player(index: dict[str, PlayerInfo], name: str) -> PlayerInfo | None:
    """Exact normalized match, then nickname swap, then a last-name-only match."""
    key = normalize_player_name(name)
    hit = index.get(key)
    if hit:
        return hit

    parts = key.split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    swapped = _NICKNAMES.get(first)
    if swapped:
        hit = index.get(" ".join([swapped, *parts[1:]]))
        if hit:
            return hit

    # Hyphenated / compound surnames ("Merritt" vs "Croskey-Merritt") - accept a
    # unique candidate whose last token matches and whose first name shares a prefix.
    fuzzy = [
        info
        for cand, info in index.items()
        if cand.split()[-1].endswith(last) and cand.split()[0][:3] == first[:3]
    ]
    return fuzzy[0] if len(fuzzy) == 1 else None


if __name__ == "__main__":
    games = upcoming_week_games()
    print(f"Season {SEASON}, upcoming week {games[0].week if games else '?'}:")
    for day, todays in games_by_day(games).items():
        print(f"\n{day}")
        for g in todays:
            print(f"  {g.gameday} {g.gametime}  {g.matchup}")
    print(f"\nIndexed {len(build_player_index())} skill players at {datetime.now():%H:%M}.")
