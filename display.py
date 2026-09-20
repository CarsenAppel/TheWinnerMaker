"""Terminal rendering with `rich` - tables, colours and panels for every report.

Pure presentation: nothing in here touches the network or does analysis.
"""

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import analysis
import apiConnect
import settings

console = Console(highlight=False)

# Colours for the game-environment tags produced by analysis.GameSignal.labels
TAG_STYLES = {
    "SHOOTOUT": "bold green",
    "COIN FLIP": "bold cyan",
    "BLOWOUT RISK": "bold red",
    "LOW SCORING": "bold yellow",
}
POSITION_STYLES = {"QB": "magenta", "RB": "green", "WR": "cyan", "TE": "yellow"}


# --------------------------------------------------------------------------- #
# Small building blocks
# --------------------------------------------------------------------------- #
def odds(price: str) -> Text:
    """American price coloured by sign: '+' is green (underdog), '-' is red (favourite)."""
    if not price:
        return Text("n/a", style="dim")
    return Text(price, style="green" if price.startswith("+") else "red")


def _over_under(over: str, under: str) -> Text:
    return Text.assemble("O ", odds(over), "  U ", odds(under))


def _tags(labels: list[str]) -> Text:
    out = Text()
    for i, label in enumerate(labels):
        if i:
            out.append(" ")
        out.append(label, style=TAG_STYLES.get(label, ""))
    return out


def short_matchup(matchup: str) -> str:
    """'Washington Commanders @ Dallas Cowboys' -> 'Commanders @ Cowboys'."""
    if " @ " not in matchup:
        return matchup
    away, home = matchup.split(" @ ", 1)
    return f"{away.split()[-1]} @ {home.split()[-1]}"


def _position(pos: str) -> Text:
    return Text(pos, style=POSITION_STYLES.get(pos, "")) if pos else Text("?", style="dim")


def _lean(value: float) -> Text:
    if value >= analysis.SKEW_THRESHOLD:
        return Text(f"OVER {value:.0%}", style="green")
    if value <= -analysis.SKEW_THRESHOLD:
        return Text(f"UNDER {abs(value):.0%}", style="red")
    return Text("-", style="dim")


def _table(
    title: str | None = None, table_box: box.Box = box.SIMPLE_HEAD, show_lines: bool = False
) -> Table:
    return Table(
        title=title,
        title_style="bold",
        title_justify="left",
        box=table_box,
        header_style="bold bright_white",
        pad_edge=False,
        show_lines=show_lines,
    )


def info(message: str) -> None:
    console.print(f"[dim]{message}[/]")


def warn(message: str) -> None:
    console.print(f"[yellow]{message}[/]")


def error(message: str) -> None:
    console.print(f"[bold red]Error:[/] {message}")


# --------------------------------------------------------------------------- #
# Menu / status
# --------------------------------------------------------------------------- #
def banner(splash: str, header: str) -> None:
    console.print(Text(splash, style="bold blue"))
    console.print(Panel(header, style="bold", expand=False))


def quota(used: int, total: int) -> None:
    remaining = total - used
    style = "green" if remaining > total * 0.4 else "yellow" if remaining > total * 0.15 else "bold red"
    console.print(
        Text.assemble(
            ("OddsPapi quota: ", "dim"),
            (f"{used}/{total}", style),
            (f" used this month  ({remaining} remaining)", "dim"),
        )
    )


def menu(options: list[str], title: str = "Menu") -> None:
    table = Table.grid(padding=(0, 2))
    for i, label in enumerate(options, 1):
        is_exit = i == len(options)
        table.add_row(f"[bold cyan]({i})[/]", f"[dim]{label}[/]" if is_exit else label)
    console.print(
        Panel(
            table,
            title=f"[bold]{title}[/]",
            title_align="left",
            subtitle=f"[dim]Enter = {options[-1]}[/]",
            subtitle_align="right",
            expand=False,
        )
    )


# --------------------------------------------------------------------------- #
# League-wide odds reports
# --------------------------------------------------------------------------- #
def game_report(signals: list[analysis.GameSignal]) -> None:
    if not signals:
        warn("No full-game markets found in the fetched fixtures.")
        return

    table = _table("Game environments (sorted by spread)")
    table.add_column("Date", style="dim")
    table.add_column("Away")
    table.add_column("Home")
    table.add_column("Home spread", justify="right")
    table.add_column("O/U", justify="right")
    table.add_column("Favorite")
    table.add_column("Win %", justify="right")
    table.add_column("Tags")

    for s in signals:
        fav_prob = max(s.home_prob, s.away_prob)
        table.add_row(
            s.start_time[:10],
            s.away,
            s.home,
            f"{s.home_spread:+.1f}" if s.home_spread is not None else "-",
            f"{s.total:.1f}" if s.total is not None else "-",
            s.favorite,
            f"{fav_prob:.0%}" if fav_prob else "-",
            _tags(s.labels),
        )
    console.print(table)

    notes: list[Text] = []
    for s in signals:
        if "BLOWOUT RISK" in s.labels:
            notes.append(
                Text.assemble(
                    ("BLOWOUT  ", TAG_STYLES["BLOWOUT RISK"]),
                    f"{s.favorite} by {abs(s.home_spread or 0):.1f} over {s.underdog}: "
                    f"{s.favorite} starters may sit late; {s.underdog} pass-catchers get garbage-time volume.",
                )
            )
        if "SHOOTOUT" in s.labels:
            notes.append(
                Text.assemble(
                    ("SHOOTOUT ", TAG_STYLES["SHOOTOUT"]),
                    f"{s.matchup} total {s.total}: start everything; stack QB + WR1.",
                )
            )
        if "LOW SCORING" in s.labels:
            notes.append(
                Text.assemble(
                    ("LOW      ", TAG_STYLES["LOW SCORING"]),
                    f"{s.matchup} total {s.total}: fade TD-dependent players; favor volume RBs.",
                )
            )
    if notes:
        console.print(Panel(Group(*notes), title="Fantasy notes", title_align="left"))


def projection_report(props: list[analysis.PropLine], per_market: int = 12) -> None:
    if not props:
        warn("No player props in the fetched data for this bookmaker.")
        return
    primary = next((p.bookmaker for p in props if p.bookmaker), "primary book")
    console.print(Rule(f"Book-implied projections ({primary} main lines)", style="bright_white"))
    for mtype in analysis.PROJECTION_MARKETS:
        rows = analysis.top_projections(props, mtype, per_market)
        if not rows:
            continue
        table = _table(analysis.PROP_LABELS[mtype])
        table.add_column("Player")
        table.add_column("Line", justify="right", style="bold")
        table.add_column("Over / Under")
        table.add_column("Lean")
        table.add_column("Matchup", style="dim")
        for p in rows:
            table.add_row(
                apiConnect.display_player_name(p.player),
                f"{p.line:.1f}",
                _over_under(p.over_american, p.under_american),
                _lean(p.lean),
                short_matchup(p.matchup),
            )
        console.print(table)


def td_report(props: list[analysis.PropLine], limit: int = 20) -> None:
    tds = analysis.top_td_scorers(props, limit)
    if not tds:
        return
    table = _table(f"Most likely to score a TD (top {limit})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Player")
    table.add_column("TD %", justify="right", style="bold green")
    table.add_column("Price", justify="right")
    table.add_column("Matchup", style="dim")
    for i, p in enumerate(tds, 1):
        table.add_row(
            str(i),
            apiConnect.display_player_name(p.player),
            f"{p.td_probability:.0%}",
            odds(p.over_american),
            short_matchup(p.matchup),
        )
    console.print(table)


def skew_report(props: list[analysis.PropLine], limit: int = 20) -> None:
    skewed = analysis.skewed_lines(props, limit)
    if not skewed:
        info("Skewed lines: no main lines priced far from even right now.")
        return
    table = _table(f"Skewed lines - book leaning hard one way (top {limit})")
    table.add_column("Player")
    table.add_column("Market")
    table.add_column("Line", justify="right", style="bold")
    table.add_column("Lean")
    table.add_column("Over / Under")
    table.add_column("Matchup", style="dim")
    for p in skewed:
        table.add_row(
            apiConnect.display_player_name(p.player),
            p.label,
            f"{p.line:.1f}",
            _lean(p.lean),
            _over_under(p.over_american, p.under_american),
            short_matchup(p.matchup),
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #
def day_picker(week: int, by_day: dict[str, list[apiConnect.ScheduledGame]]) -> None:
    table = _table(f"Week {week} - upcoming games by day")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Day")
    table.add_column("Date", style="dim")
    table.add_column("Games", justify="right")
    for i, (day, games) in enumerate(by_day.items(), 1):
        table.add_row(str(i), day, games[0].gameday, str(len(games)))
    console.print(table)


def schedule(day: str, games: list[apiConnect.ScheduledGame]) -> None:
    table = _table(f"{day} {games[0].gameday}")
    table.add_column("Kickoff (ET)", style="dim")
    table.add_column("Away")
    table.add_column("", style="dim")
    table.add_column("Home")
    for g in games:
        table.add_row(g.gametime, g.away_name, "@", g.home_name)
    console.print(table)


# --------------------------------------------------------------------------- #
# Add / drop search
# --------------------------------------------------------------------------- #
def _lines_cell(c: analysis.PlayerCandidate) -> Text:
    parts: list[Text] = []
    for m in analysis.PROJECTION_MARKETS:
        if m in c.props:
            parts.append(Text.assemble((analysis.PROP_LABELS[m], "dim"), f" {c.props[m].line:g}"))
    if c.td_probability is not None:
        parts.append(Text.assemble(("TD", "dim"), f" {c.td_probability:.0%}"))
    return Text("  ").join(parts) if parts else Text("-", style="dim")


def _reasons_cell(c: analysis.PlayerCandidate) -> Text:
    if not c.reasons:
        return Text("-", style="dim")
    out = Text()
    for i, reason in enumerate(c.reasons):
        if i:
            out.append("\n")
        style = "red" if ("UNDER" in reason or "low-scoring" in reason or "rest" in reason) else "green"
        out.append(reason, style=style)
    return out


def _candidate_table(title: str, rows: list[analysis.PlayerCandidate], style: str) -> Table:
    table = _table(title, table_box=box.ROUNDED, show_lines=True)
    table.title_style = f"bold {style}"
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Player", style="bold", min_width=20)
    table.add_column("Pos", width=3)
    table.add_column("Team", width=4)
    table.add_column("Score", justify="right", style=f"bold {style}", width=6)
    table.add_column("Adj", justify="right", width=5)
    table.add_column("Book lines", min_width=30)
    table.add_column("Matchup", style="dim", no_wrap=True)
    table.add_column("Why", min_width=24)
    for i, c in enumerate(rows, 1):
        adj = Text(f"{c.adjustment:+.1f}", style="green" if c.adjustment > 0 else "red" if c.adjustment < 0 else "dim")
        table.add_row(
            str(i),
            c.player,
            _position(c.position),
            c.team or "?",
            f"{c.score:.1f}",
            adj,
            _lines_cell(c),
            short_matchup(c.matchup),
            _reasons_cell(c),
        )
    return table


def _other_books_cell(p: analysis.PropLine) -> Text:
    """One line per other book: 'draftkings 74.5  O -110 / U -110', flagging a different number."""
    if not p.other_books:
        return Text("-", style="dim")
    out = Text()
    for i, b in enumerate(p.other_books):
        if i:
            out.append("\n")
        line_style = "bold yellow" if b.line != p.line else ""
        out.append(f"{b.bookmaker} ", style="dim")
        out.append(f"{b.line:g}", style=line_style)
        out.append("  O ")
        out.append_text(odds(b.over_american))
        out.append(" / U ")
        out.append_text(odds(b.under_american))
    return out


# --------------------------------------------------------------------------- #
# Bookmakers
# --------------------------------------------------------------------------- #
def odds_status(ages: dict[str, float | None], primary: str, fixture_count: int) -> None:
    parts = Text()
    for i, (book, age) in enumerate(ages.items()):
        if i:
            parts.append("  ")
        parts.append(book, style="bold" if book == primary else "")
        if book == primary:
            parts.append("*", style="bold")
        parts.append(f" {age / 3600:.1f}h" if age is not None else " fresh", style="dim")
    console.print(Text.assemble(("Odds: ", "dim"), parts, (f"   ({fixture_count} fixtures)", "dim")))


def bookmaker_settings(cfg: settings.Settings) -> None:
    table = _table("Configured bookmakers")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Bookmaker")
    table.add_column("Role")
    for i, book in enumerate(cfg.bookmakers, 1):
        primary = book == cfg.primary_bookmaker
        table.add_row(
            str(i),
            Text(book, style="bold" if primary else ""),
            Text("primary - drives projections", style="green") if primary else Text("comparison", style="dim"),
        )
    console.print(table)
    info(f"Each odds refresh costs {len(cfg.bookmakers)} request(s) - one per bookmaker.")


def available_bookmakers(books: list[dict[str, object]], configured: list[str]) -> None:
    table = _table(f"Bookmakers on OddsPapi ({len(books)})")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("")
    rows: list[tuple[str, str]] = []
    for b in books:
        slug = str(b.get("bookmakerSlug") or b.get("slug") or b.get("bookmaker") or "")
        name = str(b.get("bookmakerName") or b.get("name") or "")
        rows.append((slug, name) if slug else ("?", str(b)))
    for slug, name in sorted(rows):
        table.add_row(slug, name, Text("configured", style="green") if slug in configured else "")
    console.print(table)
    info("Use the slug when adding a bookmaker.")


def line_shopping_report(props: list[analysis.PropLine]) -> None:
    if not props:
        info("Books agree on every main line right now.")
        return
    table = _table("Line shopping - books posting different numbers", table_box=box.ROUNDED, show_lines=True)
    table.add_column("Player", style="bold")
    table.add_column("Market")
    table.add_column("Spread", justify="right", style="bold yellow")
    table.add_column("Primary")
    table.add_column("Other books")
    table.add_column("Matchup", style="dim")
    for p in props:
        primary = Text.assemble(
            (f"{p.bookmaker} ", "dim"),
            (f"{p.line:g}", "bold"),
            "  O ",
            odds(p.over_american),
            " / U ",
            odds(p.under_american),
        )
        table.add_row(
            apiConnect.display_player_name(p.player),
            p.label,
            f"{p.line_spread:g}",
            primary,
            _other_books_cell(p),
            short_matchup(p.matchup),
        )
    console.print(table)
    info("Spread = highest minus lowest main line across books. A book posting a lower Over line (or higher Under) is the softer number.")


# --------------------------------------------------------------------------- #
# Single-player search
# --------------------------------------------------------------------------- #
def _verdict(c: analysis.PlayerCandidate) -> Text:
    if c.score >= 15:
        return Text("MUST START", style="bold green")
    if c.score >= 10:
        return Text("START", style="green")
    if c.score >= analysis.DROP_SCORE_CEILING:
        return Text("FLEX / BENCH", style="yellow")
    return Text("FADE / DROP", style="bold red")


def player_card(c: analysis.PlayerCandidate, kickoff: str = "") -> None:
    game = c.game
    header = Text.assemble(
        (c.player, "bold bright_white"),
        "  ",
        _position(c.position),
        (f"  {c.team}" if c.team else "", "bold"),
    )

    context = Table.grid(padding=(0, 2))
    context.add_column(style="dim")
    context.add_column()
    context.add_row("Game", Text.assemble(c.matchup, (f"   {kickoff}" if kickoff else "", "dim")))
    if game is not None:
        spread = f"{game.home_spread:+.1f}" if game.home_spread is not None else "-"
        total = f"{game.total:.1f}" if game.total is not None else "-"
        fav_prob = max(game.home_prob, game.away_prob)
        context.add_row(
            "Line",
            Text.assemble(
                f"{game.home} {spread}   O/U {total}   ",
                (f"{game.favorite} {fav_prob:.0%}", "bold"),
                "   ",
                _tags(game.labels),
            ),
        )
    context.add_row(
        "Verdict",
        Text.assemble(
            _verdict(c),
            (f"   {c.score:.1f} pts", "bold"),
            (f"  ({c.projection:.1f} book-implied ", "dim"),
            (f"{c.adjustment:+.1f}", "green" if c.adjustment > 0 else "red" if c.adjustment < 0 else "dim"),
            (" adj)", "dim"),
        ),
    )

    primary = next((p.bookmaker for p in c.props.values() if p.bookmaker), "")
    multi_book = any(p.other_books for p in c.props.values())
    lines = _table(f"Book lines ({primary} main lines)" if primary else "Book lines")
    lines.add_column("Market")
    lines.add_column("Line", justify="right", style="bold")
    lines.add_column("Over / Under")
    lines.add_column("Lean")
    if multi_book:
        lines.add_column("Other books")
    td = c.props.get(analysis.ANYTIME_TD)
    ordered = [m for m in analysis.PROJECTION_MARKETS if m in c.props] + sorted(
        m for m in c.props if m not in analysis.PROJECTION_MARKETS and m != analysis.ANYTIME_TD
    )

    def row(p: analysis.PropLine, *cells: RenderableType) -> None:
        extra = (_other_books_cell(p),) if multi_book else ()
        lines.add_row(*cells, *extra)

    for m in ordered:
        p = c.props[m]
        row(p, p.label, f"{p.line:g}", _over_under(p.over_american, p.under_american), _lean(p.lean))
    if td and td.over_price:
        row(
            td,
            "Anytime TD",
            Text(f"{td.td_probability:.0%}", style="bold green"),
            Text.assemble("Yes ", odds(td.over_american), "  No ", odds(td.under_american)),
            Text("-", style="dim"),
        )

    body: list[RenderableType] = [context, "", lines]
    if c.reasons:
        body += ["", Text("Why", style="bold"), _reasons_cell(c)]

    console.print(Panel(Group(*body), title=header, title_align="left", border_style="bright_white"))


def player_matches(hits: list[analysis.PlayerCandidate]) -> None:
    """Disambiguation list when a search matches several players."""
    table = _table(f"{len(hits)} players matched - pick one")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Player", style="bold")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("Score", justify="right")
    table.add_column("Matchup", style="dim")
    for i, c in enumerate(hits, 1):
        table.add_row(str(i), c.player, _position(c.position), c.team or "?", f"{c.score:.1f}", short_matchup(c.matchup))
    console.print(table)


def add_drop_report(
    adds: list[analysis.PlayerCandidate],
    drops: list[analysis.PlayerCandidate],
    day: str,
    filters: str,
) -> None:
    title = f"{day} slate - add/drop search" + (f"  [dim]({filters})[/]" if filters else "")
    console.print(Rule(title, style="bright_white"))
    if not adds:
        warn("No players with props matched those filters.")
        return
    console.print(_candidate_table(f"ADD / START candidates (top {len(adds)})", adds, "green"))
    if drops:
        console.print(_candidate_table("DROP / FADE candidates", drops, "red"))
    else:
        info("No drop/fade candidates flagged.")
    info(
        "Score = book-implied fantasy pts (half-PPR, 6/TD) + Adj, where Adj sums "
        "game-script and line-lean adjustments."
    )
