"""Persistent maintenance worker for assignment expiry, redistribution and webhook delivery."""

import logging
import signal
import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run collaborative maintenance periodically. Restart-safe; all work is stored in PostgreSQL.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=30)
        parser.add_argument('--health-file', default='/tmp/collab-worker-health')
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        if options['interval'] < 1:
            raise CommandError('interval must be positive')
        running = True

        def stop(*_):
            nonlocal running
            running = False

        if not options['once']:
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
        while running:
            started = time.monotonic()
            close_old_connections()
            try:
                call_command('collab_maintenance', stdout=self.stdout)
                Path(options['health_file']).touch()
            except Exception:
                logger.exception('Collaborative maintenance failed; retrying on next interval')
                if options['once']:
                    raise
            finally:
                close_old_connections()
            if options['once']:
                break
            while running and time.monotonic() - started < options['interval']:
                time.sleep(min(1, options['interval']))
