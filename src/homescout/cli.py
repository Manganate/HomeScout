"""HomeScout command-line interface."""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from homescout import pipeline
from homescout.config import load_env, load_search_config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find, filter, enrich, and rank real estate listings.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def init(
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Write defaults without prompting."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Set up your search criteria (interactive)."""
    _setup_logging(verbose)
    from homescout.wizard.init import run_wizard
    run_wizard(non_interactive=non_interactive)


@app.command()
def run(
    stages: list[str] = typer.Argument(None, help="Stages to run: scrape, filter, analyze, rank. Default: all."),
    source: str = typer.Option(None, "--source", "-s", help="Listing source to use (e.g. fixtures)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the stage plan without executing."),
    refresh_assessments: bool = typer.Option(
        False, "--refresh-assessments",
        help="Re-download the City assessment data instead of using the cached copy.",
    ),
    top: int = typer.Option(10, "--top", help="How many listings to show when the run completes."),
    no_view: bool = typer.Option(False, "--no-view", help="Skip the results table at the end."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the pipeline."""
    _setup_logging(verbose)
    load_env()

    try:
        stats = pipeline.run(
            stages=list(stages) if stages else None,
            source=source,
            dry_run=dry_run,
            refresh_assessments=refresh_assessments,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if dry_run or no_view:
        return

    ran = [s for s in stats if s != "dry_run"]
    if "rank" in ran:
        from homescout import view
        console.rule("[bold]Results[/bold]")
        view.show(limit=top, criteria=load_search_config())


@app.command()
def view(
    top: int = typer.Option(10, "--top", "-n", help="How many listings to show."),
    no_html: bool = typer.Option(False, "--no-html", help="Skip writing the HTML report."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the ranked listings from the last run."""
    _setup_logging(verbose)
    from homescout import view as view_module
    view_module.show(limit=top, criteria=load_search_config(), write_html=not no_html)


@app.command()
def sources(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """List available listing sources."""
    _setup_logging(verbose)
    from homescout.config import load_sources_config
    from homescout.discovery.base import available_sources

    configured = load_sources_config().get("listing_sources", {}) or {}

    table = Table(title="Listing sources", header_style="bold cyan")
    table.add_column("Name", style="cyan")
    table.add_column("Enabled", justify="center")
    table.add_column("Notes")

    notes = {
        "realtor_ca": "Live listings via a real browser. Review the portal's terms before use.",
        "fixtures": "Offline sample data for development and tests.",
    }

    for name in available_sources():
        enabled = bool((configured.get(name) or {}).get("enabled"))
        table.add_row(name, "[green]yes[/green]" if enabled else "[dim]no[/dim]", notes.get(name, ""))

    console.print(table)


@app.command()
def config(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Show the current search configuration."""
    _setup_logging(verbose)
    from homescout.config import SEARCH_CONFIG_PATH

    c = load_search_config()

    table = Table(title="Search configuration", show_header=False, title_style="bold")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Area", c.area)
    table.add_row("CBD anchor", f"{c.cbd_lat:.4f}, {c.cbd_lon:.4f}")
    table.add_row("Budget", f"${c.price_min:,} – ${c.price_max:,}")
    table.add_row("Bedrooms", f"{c.beds_min}+" if c.beds_max is None else f"{c.beds_min}–{c.beds_max}")
    table.add_row("Bathrooms", f"{c.baths_min}+" if c.baths_max is None else f"{c.baths_min}–{c.baths_max}")
    table.add_row("Max commute", f"{c.max_commute_min} min ({c.commute_mode})")
    table.add_row("Pre-filter radius", f"{c.geo_bound_km:.1f} km")
    table.add_row("Max listing age", f"{c.max_listing_age_days} days")
    table.add_row("Property types", ", ".join(c.property_types) or "any")
    table.add_row("Weights",
                  f"value {c.weight_value:.2f} · location {c.weight_location:.2f} · freshness {c.weight_freshness:.2f}")
    console.print(table)

    exists = SEARCH_CONFIG_PATH.exists()
    location = str(SEARCH_CONFIG_PATH).replace(str(SEARCH_CONFIG_PATH.home()), "~")
    console.print(f"\n[dim]{'Loaded from' if exists else 'Defaults — no config at'}[/dim] {location}")
    if not exists:
        console.print("[dim]Run [cyan]homescout init[/cyan] to create it.[/dim]")


@app.command()
def reset(
    stage: str = typer.Argument(..., help="Move listings back to this stage: scraped, filtered, analyzed."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Move listings back a stage so it can be re-run."""
    _setup_logging(verbose)
    from homescout.database import (
        STAGE_ANALYZED,
        STAGE_FILTERED,
        STAGE_RANKED,
        STAGE_SCRAPED,
        get_connection,
        reset_stage,
    )

    transitions = {
        "scraped": (STAGE_FILTERED, STAGE_SCRAPED),
        "filtered": (STAGE_ANALYZED, STAGE_FILTERED),
        "analyzed": (STAGE_RANKED, STAGE_ANALYZED),
    }
    if stage not in transitions:
        console.print(f"[red]Unknown stage {stage!r}. Choose: {', '.join(transitions)}[/red]")
        raise typer.Exit(code=1)

    src, dst = transitions[stage]
    conn = get_connection()
    try:
        n = reset_stage(conn, src, dst)
    finally:
        conn.close()
    console.print(f"Moved [bold]{n}[/bold] listings from {src} back to {dst}.")


if __name__ == "__main__":
    app()
