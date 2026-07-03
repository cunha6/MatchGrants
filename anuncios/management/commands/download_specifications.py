"""
Backfill the tender specifications: for notices already in the DB that still have no
`specifications_path`, download the specifications (when they exist) and store the path.

Does not hit the base.gov API — works only over the existing records. May take a while
(uses a headless browser for JS portals). Progress shows in the console.

Usage:
    python manage.py download_specifications
"""

from django.core.management.base import BaseCommand

from anuncios import services


class Command(BaseCommand):
    help = "Download and store the tender specifications for notices that still lack them."

    def handle(self, *args, **options):
        summary = services.download_missing_specifications()
        self.stdout.write(self.style.SUCCESS(
            "Done — "
            f"pending: {summary['pending']}, "
            f"downloaded: {summary['downloaded']}, "
            f"missing: {summary['missing']}"
        ))
