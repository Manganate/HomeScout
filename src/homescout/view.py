"""Ranked output: rich table for the terminal, standalone HTML for sharing."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from homescout.config import REPORT_PATH
from homescout.database import get_connection, get_top_ranked
from homescout.models import SearchCriteria

console = Console()


def show(limit: int = 10, criteria: SearchCriteria | None = None, write_html: bool = True) -> int:
    """Print the top N ranked listings. Returns the number shown."""
    conn = get_connection()
    try:
        rows = [dict(r) for r in get_top_ranked(conn, limit)]
        cohort = conn.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE score_total IS NOT NULL"
        ).fetchone()["n"]
    finally:
        conn.close()

    if not rows:
        console.print("[yellow]No ranked listings yet. Run [cyan]homescout run[/cyan] first.[/yellow]")
        return 0

    _print_table(rows, cohort)

    for i, row in enumerate(rows, start=1):
        console.print(f"\n[bold cyan]#{i}[/bold cyan] [bold]{row['address']}[/bold] — {_money(row['price'])}")
        console.print(f"   {row.get('explanation') or ''}")
        if row.get("url"):
            console.print(f"   [dim blue]{row['url']}[/dim blue]")

    if write_html:
        path = _write_html(rows, cohort, criteria)
        console.print(f"\n[green]Report written to[/green] {_display(path)}")

    return len(rows)


def _print_table(rows: list[dict], cohort: int) -> None:
    table = Table(
        title=f"Top {len(rows)} of {cohort} matching listings",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("#", justify="right", width=3)
    table.add_column("Address")
    table.add_column("Price", justify="right")
    table.add_column("Bd/Ba", justify="center")
    table.add_column("Sqft", justify="right")
    table.add_column("$/sqft", justify="right")
    table.add_column("Commute", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("V/L/F", justify="center")
    table.add_column("Data", justify="right")

    for i, r in enumerate(rows, start=1):
        table.add_row(
            str(i),
            _truncate(r.get("address", ""), 30),
            _money(r.get("price")),
            f"{_num(r.get('beds'))}/{_num(r.get('baths'))}",
            _num(r.get("sqft")),
            _ppsf(r),
            _commute(r),
            f"[bold]{_num(r.get('score_total'), 0)}[/bold]",
            "/".join(_num(r.get(k), 0) for k in ("score_value", "score_location", "score_freshness")),
            f"{_num(r.get('data_completeness'), 0)}%",
        )

    console.print(table)
    if cohort < 10:
        console.print(f"[yellow]Cohort is only {cohort} listings — percentile comparisons are coarse.[/yellow]")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _write_html(rows: list[dict], cohort: int, criteria: SearchCriteria | None) -> Path:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    subtitle = ""
    if criteria:
        subtitle = (
            f"{html.escape(criteria.area)} &middot; "
            f"${criteria.price_min:,}–${criteria.price_max:,} &middot; "
            f"{criteria.beds_min}+ bed / {criteria.baths_min}+ bath &middot; "
            f"&le;{criteria.max_commute_min} min {html.escape(criteria.commute_mode)} &middot; "
            f"&le;{criteria.max_listing_age_days} days listed"
        )

    cards = "\n".join(_card(i, r) for i, r in enumerate(rows, start=1))

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HomeScout — Top {len(rows)} Listings</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
    --card: #ffffff; --accent: #1d4ed8; --chip: #f3f4f6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1115; --fg: #e6e6e6; --muted: #9ca3af; --line: #262a33;
      --card: #161922; --accent: #7aa2ff; --chip: #1f2430;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  .wrap {{ max-width: 60rem; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .25rem; }}
  .sub {{ color: var(--muted); font-size: .9rem; margin-bottom: 1.5rem; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 1rem 1.15rem; margin-bottom: .9rem;
  }}
  .head {{ display: flex; flex-wrap: wrap; gap: .6rem; align-items: baseline; }}
  .rank {{ font-weight: 700; color: var(--accent); font-size: 1.05rem; }}
  .addr {{ font-weight: 600; flex: 1 1 16rem; }}
  .price {{ font-weight: 700; white-space: nowrap; }}
  .score {{
    background: var(--chip); border-radius: 999px; padding: .15rem .6rem;
    font-size: .85rem; font-weight: 600; white-space: nowrap;
  }}
  .facts {{ color: var(--muted); font-size: .88rem; margin: .5rem 0; }}
  .why {{ font-size: .93rem; margin: .5rem 0 .6rem; }}
  a {{ color: var(--accent); text-decoration: none; font-size: .88rem; }}
  a:hover {{ text-decoration: underline; }}
  .note {{ color: var(--muted); font-size: .8rem; margin-top: 2rem; border-top: 1px solid var(--line); padding-top: 1rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Top {len(rows)} of {cohort} matching listings</h1>
  <div class="sub">{subtitle}<br>Generated {generated}</div>
  {cards}
  <p class="note">
    Scores are percentile ranks within this cohort of {cohort} listings, not absolute ratings.
    &ldquo;Walk proxy&rdquo; is an OpenStreetMap-derived measure of amenity density &mdash; it is not Walk Score&reg;.
    &ldquo;Data&rdquo; shows the share of scoring signals actually available for that listing.
  </p>
</div>
</body>
</html>"""

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(document, encoding="utf-8")
    return REPORT_PATH


def _card(rank: int, r: dict) -> str:
    facts = " &middot; ".join(filter(None, [
        f"{_num(r.get('beds'))} bed / {_num(r.get('baths'))} bath",
        f"{_num(r.get('sqft'))} sqft" if r.get("sqft") else "",
        f"{_ppsf(r)}/sqft" if r.get("sqft") else "",
        f"{_commute(r)} to downtown" if r.get("commute_min") is not None else "",
        f"{_num(r.get('data_completeness'), 0)}% data" if r.get("data_completeness") is not None else "",
    ]))
    url = r.get("url") or ""
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">View listing &rarr;</a>' if url else ""

    return f"""<div class="card">
    <div class="head">
      <span class="rank">#{rank}</span>
      <span class="addr">{html.escape(str(r.get('address') or ''))}</span>
      <span class="price">{_money(r.get('price'))}</span>
      <span class="score">{_num(r.get('score_total'), 0)}/100</span>
    </div>
    <div class="facts">{facts}</div>
    <div class="why">{html.escape(str(r.get('explanation') or ''))}</div>
    {link}
  </div>"""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _money(value) -> str:
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _num(value, places: int = 0) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{f:,.{places}f}" if places else f"{f:,.0f}"


def _ppsf(r: dict) -> str:
    price, sqft = r.get("price"), r.get("sqft")
    if not price or not sqft:
        return "—"
    try:
        return f"${float(price) / float(sqft):,.0f}"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _commute(r: dict) -> str:
    minutes = r.get("commute_min")
    if minutes is None:
        return "—"
    suffix = "~" if r.get("commute_estimated") else ""
    return f"{suffix}{float(minutes):.0f} min"


def _truncate(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


def _display(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)
