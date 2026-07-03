"""
Import notices from the base.gov.pt API via the terminal (no HTTP timeout).

Runs the full import — which can take a while (downloads the tender specifications from
JS portals with a shared headless browser). Progress shows in the console.

Usage:
    python manage.py import_notices          # last 15 days
    python manage.py import_notices --days 30
"""

from django.core.management.base import BaseCommand

from anuncios import services


class Command(BaseCommand):
    help = "Import notices from base.gov.pt (filter by keywords, download the specifications)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=15,
            help="Number of days back to query the API (default: 15).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        self.stdout.write(f"Importing notices from the last {days} days (may take a while)...")
        services.mark_import_start()
        try:
            summary = services.import_notices(days)
        except services.BaseGovError as exc:
            self.stderr.write(self.style.ERROR(f"base.gov.pt API error: {exc}"))
            return
        finally:
            services.mark_import_end()

        self.stdout.write(self.style.SUCCESS(
            "Done — "
            f"received: {summary['total_received']}, "
            f"with keywords: {summary['with_keywords']}, "
            f"created: {summary['created']}, "
            f"updated: {summary['updated']}, "
            f"unchanged: {summary['unchanged']}"
        ))
