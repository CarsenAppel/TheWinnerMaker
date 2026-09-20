"""User preferences persisted to settings.json next to the code.

Currently just the bookmaker list. The *primary* bookmaker's lines drive all
projections and scoring (Pinnacle by default - it's the sharpest book); the
other books are pulled alongside so prices can be compared / line-shopped.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_DIR / "settings.json"

DEFAULT_BOOKMAKER = "pinnacle"


@dataclass
class Settings:
    bookmakers: list[str] = field(default_factory=lambda: [DEFAULT_BOOKMAKER])
    primary_bookmaker: str = DEFAULT_BOOKMAKER

    def normalized(self) -> "Settings":
        """Lower-case, de-duplicate, and make sure the primary is in the list."""
        seen: list[str] = []
        for b in self.bookmakers:
            slug = b.strip().lower()
            if slug and slug not in seen:
                seen.append(slug)
        primary = self.primary_bookmaker.strip().lower()
        if not seen:
            seen = [DEFAULT_BOOKMAKER]
        if primary not in seen:
            primary = seen[0]
        return Settings(bookmakers=seen, primary_bookmaker=primary)

    @property
    def ordered_bookmakers(self) -> list[str]:
        """Primary first, then the rest in configured order."""
        rest = [b for b in self.bookmakers if b != self.primary_bookmaker]
        return [self.primary_bookmaker, *rest]


def load() -> Settings:
    if not SETTINGS_FILE.exists():
        return Settings()
    try:
        raw = cast(dict[str, Any], json.loads(SETTINGS_FILE.read_text()))
    except (OSError, json.JSONDecodeError):
        return Settings()
    books = raw.get("bookmakers")
    return Settings(
        bookmakers=[str(b) for b in books] if isinstance(books, list) else [DEFAULT_BOOKMAKER],
        primary_bookmaker=str(raw.get("primary_bookmaker", DEFAULT_BOOKMAKER)),
    ).normalized()


def save(settings: Settings) -> Settings:
    settings = settings.normalized()
    SETTINGS_FILE.write_text(json.dumps(asdict(settings), indent=2) + "\n")
    return settings
