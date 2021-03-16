from datetime import datetime, timezone

from django.core.management import BaseCommand
from django.db import transaction
from django_scopes import scope

from pretix.plugins.sendmail.models import ScheduledMail


class Command(BaseCommand):
    def handle(self, *args, **options):
        with scope(organizer=None, subevent=None, event=None):
            mails = ScheduledMail.objects.all()

            for m in mails:
                m.recompute()

            for m in mails.filter(computed_datetime__lte=datetime.now(timezone.utc), sent=False).only('pk'):
                with transaction.atomic():
                    self.stdout.write(f'{m.pk}')
                    m = ScheduledMail.objects.get(pk=m.pk)
                    if m.sent:
                        continue
                    m.send()
