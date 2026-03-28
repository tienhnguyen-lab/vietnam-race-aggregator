#!/usr/bin/env python3
"""
Vietnam Endurance Race Aggregator — CLI entry point.

Usage:
    python main.py sync
    python main.py list [--sort date|price|name] [--filter-type trail|road|triathlon]
                        [--location CITY] [--status open|upcoming|sold-out]
                        [--distance 21km]
    python main.py health
"""
import sys
import json
import logging
import click
from rich.console import Console
from rich.table import Table
from rich import box

# Make imports work from project root
sys.path.insert(0, __file__.rsplit("/", 1)[0])

from database.handler import DatabaseHandler
from scrapers.actiup import ActiUpScraper
from scrapers.truerace import TrueRaceScraper
from scrapers.vietrace365 import VietRace365Scraper
from scrapers.irace import IRaceScraper
from scrapers.vnexpress_schedule import VnExpressScheduleScraper

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)


def _get_all_scrapers(db: DatabaseHandler, headless: bool = True):
    return [
        ActiUpScraper(db),
        TrueRaceScraper(db),
        VietRace365Scraper(db),
        IRaceScraper(db),
        VnExpressScheduleScraper(db),
    ]


# ===========================================================================
# CLI commands
# ===========================================================================

@click.group()
def cli():
    """Vietnam Endurance Race Aggregator."""


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--scraper", default=None,
              help="Run only a specific scraper (e.g. actiup, irace, vietrace365).")
@click.option("--visible", is_flag=True, default=False,
              help="Show browser window (disable headless mode).")
def sync(scraper, visible):
    """Run all scrapers and update the database."""
    headless = not visible
    with DatabaseHandler() as db:
        scrapers = _get_all_scrapers(db, headless=headless)

        if scraper:
            scrapers = [s for s in scrapers if s.name == scraper]
            if not scrapers:
                console.print(f"[red]Unknown scraper: {scraper}[/red]")
                raise SystemExit(1)

        total = 0
        for s in scrapers:
            console.print(f"[cyan]→ Running scraper:[/cyan] [bold]{s.name}[/bold]")
            count = s.run()
            total += count
            console.print(f"  [green]✓[/green] {count} race(s) upserted.")

        console.print(f"\n[bold green]Sync complete.[/bold green] {total} total upsert(s).")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@cli.command("list")
@click.option("--sort", default="date",
              type=click.Choice(["date", "price", "name"]),
              help="Sort order.")
@click.option("--filter-type", "filter_type", default=None,
              type=click.Choice(["trail", "road", "triathlon", "ironman", "duathlon"],
                                case_sensitive=False),
              help="Filter by race type.")
@click.option("--location", default=None,
              help="Filter by city/location substring (e.g. 'Da Nang').")
@click.option("--status", default=None,
              type=click.Choice(["open", "upcoming", "sold-out", "unknown"],
                                case_sensitive=False),
              help="Filter by registration status.")
@click.option("--distance", default=None,
              help="Distance to use for price sort (e.g. '21km'). Defaults to 21km/42km.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw JSON instead of a formatted table.")
def list_races(sort, filter_type, location, status, distance, as_json):
    """Display races in a formatted table."""
    # Map CLI status values to DB values
    status_map = {"open": "Open", "upcoming": "Upcoming", "sold-out": "Sold Out", "unknown": "Unknown"}
    db_status = status_map.get((status or "").lower())

    with DatabaseHandler() as db:
        races = db.list_races(
            sort=sort,
            race_type=filter_type,
            location=location,
            status=db_status,
            distance_filter=distance,
        )

    if not races:
        console.print("[yellow]No races found matching your criteria.[/yellow]")
        return

    if as_json:
        click.echo(json.dumps(races, ensure_ascii=False, indent=2))
        return

    _render_table(races, sort, distance)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

@cli.command()
def health():
    """Show scraper health (last run, status, races found)."""
    with DatabaseHandler() as db:
        rows = db.get_scraper_health()

    if not rows:
        console.print("[yellow]No scraper runs recorded yet. Run 'python main.py sync' first.[/yellow]")
        return

    table = Table(
        title="Scraper Health",
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
    )
    table.add_column("Scraper", style="bold cyan")
    table.add_column("Last Run", style="dim")
    table.add_column("Status")
    table.add_column("Races Found", justify="right")
    table.add_column("Message", style="dim", max_width=50)

    for row in rows:
        status_style = "green" if row["status"] == "success" else "red"
        table.add_row(
            row["scraper"],
            row["last_run"] or "—",
            f"[{status_style}]{row['status']}[/{status_style}]",
            str(row["races_found"]),
            row["message"] or "",
        )

    console.print(table)


# ===========================================================================
# Table renderer
# ===========================================================================

def _render_table(races: list[dict], sort: str, distance_filter: str | None):
    title_parts = [f"Vietnam Endurance Races ({len(races)} results)"]
    if sort == "price":
        title_parts.append(f"sorted by price ({distance_filter or '21km/42km'})")
    else:
        title_parts.append(f"sorted by {sort}")

    table = Table(
        title=" — ".join(title_parts),
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
        expand=True,
    )

    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Race Name", style="bold", min_width=28)
    table.add_column("Date", no_wrap=True)
    table.add_column("City")
    table.add_column("Type")
    table.add_column("Distances")
    table.add_column("Best Price", justify="right")
    table.add_column("Status")
    table.add_column("Registration URL", max_width=35, overflow="fold")

    for idx, race in enumerate(races, 1):
        pricing = race.get("pricing") or {}
        best_price = _best_price_str(pricing, distance_filter)

        status = race.get("registration_status", "Unknown")
        status_fmt = _status_fmt(status)

        distances = ", ".join(race.get("distances") or []) or "—"
        date_str = (race.get("date") or "TBD")[:10]

        table.add_row(
            str(idx),
            race.get("race_name") or "—",
            date_str,
            race.get("city") or race.get("location") or "—",
            race.get("race_type") or "—",
            distances,
            best_price,
            status_fmt,
            race.get("registration_url") or race.get("official_website") or "—",
        )

    console.print(table)


def _best_price_str(pricing: dict, distance_filter: str | None) -> str:
    """Return a formatted best-price string from pricing dict."""
    if not pricing:
        return "—"
    targets = [distance_filter] if distance_filter else ["21km", "42km", "half", "full"]

    for dist_key, tiers in pricing.items():
        if any(t.lower() in dist_key.lower() for t in targets):
            if isinstance(tiers, dict):
                prices = {}
                for tier, val in tiers.items():
                    if tier in ("early_bird", "regular", "late"):
                        try:
                            prices[tier] = int(val)
                        except (TypeError, ValueError):
                            pass
                if prices:
                    label, amount = min(prices.items(), key=lambda x: x[1])
                    currency = tiers.get("currency", "VND")
                    return f"{amount:,} {currency}\n({label.replace('_', ' ')})"

    # Fallback: show any first price
    for dist_key, tiers in pricing.items():
        if isinstance(tiers, dict):
            for tier, val in tiers.items():
                if tier not in ("currency",):
                    try:
                        return f"{int(val):,} VND"
                    except (TypeError, ValueError):
                        pass
    return "—"


def _status_fmt(status: str) -> str:
    colour_map = {
        "Open": "bold green",
        "Upcoming": "yellow",
        "Sold Out": "red",
        "Unknown": "dim",
    }
    colour = colour_map.get(status, "dim")
    return f"[{colour}]{status}[/{colour}]"


if __name__ == "__main__":
    cli()
