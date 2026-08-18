"""First-time setup wizard.

Walks through every search parameter and writes ~/.homescout/search.yaml.
Re-runnable at any time; existing values are offered as defaults so pressing
Enter through the whole flow is a safe no-op.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from homescout.config import (
    SEARCH_CONFIG_PATH,
    ensure_dirs,
    load_search_config,
    save_search_config,
)
from homescout.database import init_db
from homescout.models import SearchCriteria

console = Console()

# Well-known downtown anchors, so the common cases need no coordinate lookup.
CBD_PRESETS: dict[str, tuple[float, float]] = {
    "edmonton": (53.5444, -113.4909),
    "vancouver": (49.2827, -123.1207),
    "toronto": (43.6487, -79.3817),
    "montreal": (45.5017, -73.5673),
    "ottawa": (45.4215, -75.6989),
    "winnipeg": (49.8951, -97.1384),
    "halifax": (44.6488, -63.5752),
}

TERMS_NOTICE = (
    "HomeScout retrieves listing data for [bold]personal, single-market use at human pace[/bold], "
    "and never redistributes or republishes it.\n\n"
    "Some listing portals prohibit automated access in their terms of service. Review the terms "
    "of any source you enable. Where a licensed MLS feed is available to you, that is the "
    "compliant alternative — the [cyan]ListingSource[/cyan] protocol is designed so one can be "
    "dropped in without touching the rest of the pipeline."
)


def run_wizard(non_interactive: bool = False) -> SearchCriteria:
    """Run the full intake flow and persist the result."""
    ensure_dirs()
    init_db()

    console.print(Panel.fit(
        "[bold]HomeScout setup[/bold]\n"
        "Configure what you're looking for. Everything here is editable later in\n"
        f"[cyan]{_display_path(SEARCH_CONFIG_PATH)}[/cyan] or by re-running [cyan]homescout init[/cyan].",
        border_style="cyan",
    ))

    current = load_search_config()

    if non_interactive:
        console.print("[yellow]Non-interactive mode — writing defaults without prompting.[/yellow]")
        path = save_search_config(current)
        console.print(f"[green]Wrote[/green] {_display_path(path)}")
        return current

    area, cbd_lat, cbd_lon = _ask_location(current)
    price_min, price_max = _ask_budget(current)
    beds_min, beds_max, baths_min, baths_max = _ask_rooms(current)
    sqft_min, sqft_max = _ask_sqft(current)
    max_commute_min, commute_mode = _ask_commute(current)
    max_age = _ask_listing_age(current)
    property_types = _ask_property_types(current)
    w_value, w_location, w_freshness = _ask_weights(current)

    criteria = SearchCriteria(
        area=area,
        cbd_lat=cbd_lat,
        cbd_lon=cbd_lon,
        price_min=price_min,
        price_max=price_max,
        beds_min=beds_min,
        beds_max=beds_max,
        baths_min=baths_min,
        baths_max=baths_max,
        sqft_min=sqft_min,
        sqft_max=sqft_max,
        max_commute_min=max_commute_min,
        commute_mode=commute_mode,
        max_listing_age_days=max_age,
        property_types=property_types,
        weight_value=w_value,
        weight_location=w_location,
        weight_freshness=w_freshness,
    )

    _show_summary(criteria)
    if not Confirm.ask("\nSave this configuration?", default=True):
        console.print("[yellow]Not saved.[/yellow]")
        return criteria

    path = save_search_config(criteria)
    console.print(f"[green]Saved[/green] {_display_path(path)}")

    console.print(Panel(TERMS_NOTICE, title="Terms of use", border_style="yellow"))
    console.print("\nNext: [cyan]homescout run --dry-run[/cyan] to preview, or [cyan]homescout run[/cyan] to go.")
    return criteria


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _ask_location(c: SearchCriteria) -> tuple[str, float, float]:
    console.print(Panel("[bold]Step 1: Area[/bold]\nWhere to search, and the downtown point commute is measured to.",
                        border_style="dim"))
    area = Prompt.ask("City / area", default=c.area)

    preset = _match_preset(area)
    if preset and Confirm.ask(
        f"Use the known downtown anchor for this city ([cyan]{preset[0]:.4f}, {preset[1]:.4f}[/cyan])?",
        default=True,
    ):
        return area, preset[0], preset[1]

    console.print("[dim]Enter the CBD coordinates to measure commute against.[/dim]")
    lat = FloatPrompt.ask("CBD latitude", default=c.cbd_lat)
    lon = FloatPrompt.ask("CBD longitude", default=c.cbd_lon)
    return area, lat, lon


def _ask_budget(c: SearchCriteria) -> tuple[int, int]:
    console.print(Panel("[bold]Step 2: Budget[/bold]", border_style="dim"))
    while True:
        lo = IntPrompt.ask("Minimum price", default=c.price_min)
        hi = IntPrompt.ask("Maximum price", default=c.price_max)
        if hi > lo:
            return lo, hi
        console.print("[red]Maximum must be greater than minimum.[/red]")


def _ask_rooms(c: SearchCriteria) -> tuple[int, int | None, int, int | None]:
    console.print(Panel("[bold]Step 3: Bedrooms & bathrooms[/bold]", border_style="dim"))
    beds_min = IntPrompt.ask("Minimum bedrooms", default=c.beds_min)
    beds_max = _ask_optional_int("Maximum bedrooms", c.beds_max)
    baths_min = IntPrompt.ask("Minimum bathrooms", default=c.baths_min)
    baths_max = _ask_optional_int("Maximum bathrooms", c.baths_max)
    return beds_min, beds_max, baths_min, baths_max


def _ask_sqft(c: SearchCriteria) -> tuple[int | None, int | None]:
    console.print(Panel("[bold]Step 4: Size[/bold]\nOptional — leave blank for no limit.", border_style="dim"))
    return _ask_optional_int("Minimum sqft", c.sqft_min), _ask_optional_int("Maximum sqft", c.sqft_max)


def _ask_commute(c: SearchCriteria) -> tuple[int, str]:
    console.print(Panel(
        "[bold]Step 5: Commute to downtown[/bold]\n"
        "Listings are pre-filtered on straight-line distance, then real routing is\n"
        "computed for the survivors and this cut is applied to the result.",
        border_style="dim",
    ))
    minutes = IntPrompt.ask("Max commute (minutes)", default=c.max_commute_min)
    mode = Prompt.ask("Mode", choices=["driving", "transit"], default=c.commute_mode)
    return minutes, mode


def _ask_listing_age(c: SearchCriteria) -> int:
    console.print(Panel("[bold]Step 6: Listing freshness[/bold]", border_style="dim"))
    return IntPrompt.ask("Max listing age (days)", default=c.max_listing_age_days)


def _ask_property_types(c: SearchCriteria) -> list[str]:
    console.print(Panel("[bold]Step 7: Property types[/bold]\nComma-separated. Blank accepts all types.",
                        border_style="dim"))
    raw = Prompt.ask("Property types", default=", ".join(c.property_types))
    types = [t.strip() for t in raw.split(",") if t.strip()]
    return types


def _ask_weights(c: SearchCriteria) -> tuple[float, float, float]:
    console.print(Panel(
        "[bold]Step 8: Ranking priorities[/bold]\n"
        "Relative weights — they're normalized, so they need not sum to 1.\n"
        "  [cyan]value[/cyan]     price per sqft, and list price vs. City assessed value\n"
        "  [cyan]location[/cyan]  commute time, walkability proxy, nearby amenities\n"
        "  [cyan]freshness[/cyan] days on market",
        border_style="dim",
    ))
    v = FloatPrompt.ask("Weight: value", default=round(c.weight_value, 2))
    l = FloatPrompt.ask("Weight: location", default=round(c.weight_location, 2))
    f = FloatPrompt.ask("Weight: freshness", default=round(c.weight_freshness, 2))
    if v + l + f <= 0:
        console.print("[yellow]All weights were zero — falling back to equal weighting.[/yellow]")
        return 1 / 3, 1 / 3, 1 / 3
    return v, l, f


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ask_optional_int(label: str, default: int | None) -> int | None:
    """Prompt for an int that may be left blank to mean 'no limit'."""
    shown = "" if default is None else str(default)
    raw = Prompt.ask(f"{label} [dim](blank = no limit)[/dim]", default=shown, show_default=bool(shown))
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        console.print("[yellow]Not a number — treating as no limit.[/yellow]")
        return None


def _match_preset(area: str) -> tuple[float, float] | None:
    key = area.strip().lower()
    for city, coords in CBD_PRESETS.items():
        if key.startswith(city) or city in key:
            return coords
    return None


def _show_summary(c: SearchCriteria) -> None:
    from rich.table import Table

    t = Table(title="Search configuration", show_header=False, title_style="bold")
    t.add_column("Field", style="cyan")
    t.add_column("Value")

    t.add_row("Area", c.area)
    t.add_row("CBD anchor", f"{c.cbd_lat:.4f}, {c.cbd_lon:.4f}")
    t.add_row("Budget", f"${c.price_min:,} – ${c.price_max:,}")
    t.add_row("Bedrooms", _range_text(c.beds_min, c.beds_max))
    t.add_row("Bathrooms", _range_text(c.baths_min, c.baths_max))
    if c.sqft_min or c.sqft_max:
        t.add_row("Size (sqft)", _range_text(c.sqft_min, c.sqft_max))
    t.add_row("Max commute", f"{c.max_commute_min} min ({c.commute_mode})")
    t.add_row("Pre-filter radius", f"{c.geo_bound_km:.1f} km straight-line")
    t.add_row("Max listing age", f"{c.max_listing_age_days} days")
    t.add_row("Property types", ", ".join(c.property_types) or "any")
    t.add_row(
        "Weights",
        f"value {c.weight_value:.2f} · location {c.weight_location:.2f} · freshness {c.weight_freshness:.2f}",
    )
    console.print(t)


def _range_text(lo, hi) -> str:
    if lo is not None and hi is not None:
        return f"{lo}–{hi}"
    if lo is not None:
        return f"{lo}+"
    if hi is not None:
        return f"up to {hi}"
    return "any"


def _display_path(path) -> str:
    """Render a path with the home directory collapsed to ~."""
    from pathlib import Path
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)
