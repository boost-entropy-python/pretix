from django.core.management import BaseCommand
from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone
from django_scopes import scopes_disabled

from pretix.plugins.sendmail.models import ScheduledMail


class Command(BaseCommand):
    def handle(self, *args, **options):
        with scopes_disabled():
            mails = ScheduledMail.objects.all()

            # TODO: figure out a way to create scheduledMails as needed (probably with signals?)

            for m in mails.filter(Q(last_computed__isnull=True)
                                  | Q(subevent__last_modified__gt=F('last_computed'))
                                  | Q(event__last_modified__gt=F('last_computed'))):
                m.recompute()

            for m in mails.filter(computed_datetime__lte=timezone.now()).only('pk'):
                with transaction.atomic():
                    m = ScheduledMail.objects.select_for_update().get(pk=m.pk)
                    if m.sent:
                        continue
                    m.send()
