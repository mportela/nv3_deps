from django.core.management.base import BaseCommand
from django.conf import settings

import logging
import os
import sys
import time

logger = logging.getLogger('chronograph.commands.cron_serial')


class Command(BaseCommand):
    help = 'Runs all jobs that are due.  Run them one at a time rather than forking into multiple threads.'

    def handle(self, *args, **options):
        from chronograph.models import Job

        procs = []
        for job in Job.objects.due():
            if not job.check_is_running():
                logger.info("Running Job: '%s'" % job)
                job.run()
