from datetime import timedelta, datetime

from django.db import models, transaction
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _, pgettext_lazy, ngettext
from django_scopes import ScopedManager
from i18nfield.fields import I18nCharField, I18nTextField

from pretix.base.models import Event, SubEvent, OrderPosition, Item


class ScheduledMail(models.Model):
    rule = models.ForeignKey("Rule", on_delete=models.CASCADE)
    subevent = models.ForeignKey(SubEvent, null=True, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, null=True, on_delete=models.CASCADE)

    sent = models.BooleanField(default=False)
    last_computed = models.DateTimeField(null=True)
    computed_datetime = models.DateTimeField(null=True)  # gets filled out on creation (on rule creation)

    def recompute(self):
        lm = self.subevent.last_modified if self.subevent else self.event.last_modified
        if self.last_computed < lm:
            self.compute_time()

    def compute_time(self):
        if self.rule.date_is_absolute:
            self.computed_datetime = self.rule.send_date
        else:
            e = self.subevent if self.subevent else self.event
            offset = timedelta(days=self.rule.send_offset_days)
            st = self.rule.send_offset_time
            base_time = e.date_to if self.rule.offset_to_event_end else e.date_from

            d = base_time + offset if self.rule.offset_is_after else base_time - offset
            self.computed_datetime = d.replace(hour=st.hour, minute=st.minute, second=st.second)

        self.last_computed = datetime.now()


class Rule(models.Model):
    CUSTOMERS = "customers"
    ATTENDEES = "attendees"

    SEND_TO_CHOICES = [
        (CUSTOMERS, _("Customers")),
        (ATTENDEES, _("Attendees")),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE)

    subject = I18nCharField(max_length=255)
    template = I18nTextField()

    # all products or limit products
    all_products = models.BooleanField(default=True, blank=True)
    limit_products = models.ManyToManyField(Item, blank=True)

    include_pending = models.BooleanField(default=False, blank=True)

    # either send_date or send_offset_* have to be set
    send_date = models.DateTimeField(null=True, blank=True)
    send_offset_days = models.IntegerField(null=True, blank=True)
    send_offset_time = models.TimeField(null=True, blank=True)

    date_is_absolute = models.BooleanField(default=True, blank=True)
    offset_to_event_end = models.BooleanField(default=False, blank=True)
    offset_is_after = models.BooleanField(default=False, blank=True)

    send_to = models.CharField(max_length=10, choices=SEND_TO_CHOICES, default=CUSTOMERS)

    objects = ScopedManager(event='event')

    def get_absolute_url(self):  # TODO: figure out why the fuck this isn't doing anything
        return reverse('plugins:sendmail:rule.update', kwargs={
            'organizer': self.event.organizer.slug,
            'event': self.event.event.slug,
            'rule': self.pk,
        })

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        super().save(force_insert, force_update, using, update_fields)
        # first, create scheduled mails that need to be created
        if self.event.has_subevents:
            with transaction.atomic():
                for se in self.event.subevents.annotate(has_sm=Exists(ScheduledMail.objects.filter(subevent=OuterRef('pk'), rule=self))).filter(has_sm=False):
                    sm = ScheduledMail.objects.create(rule=self, subevent=se)
                    sm.compute_time()
                    sm.save()
        else:
            if not Exists(ScheduledMail.objects.filter(rule=self, event=self.event)):
                sm = ScheduledMail.objects.create(rule=self, event=self.event)
                sm.compute_time()
                sm.save()

        # todo: continue here, write creation code, make sure recompute works, or
        for sm in ScheduledMail.objects.filter(rule=self):
            sm.recompute()

    @property
    def human_readable_time(self):
        if self.date_is_absolute:
            d = self.send_date
            return _('on {date} at {time}').format(date=date_format(d, 'SHORT_DATE_FORMAT'),
                                                   time=date_format(d, 'TIME_FORMAT'))
        else:
            if self.offset_to_event_end:
                if self.offset_is_after:
                    s = ngettext(
                        '%(count)d day after event end at %(time)s',
                        '%(count)d days after event end at %(time)s',
                        self.send_offset_days
                    ) % {
                        'count': self.send_offset_days,
                        'time': date_format(self.send_offset_time, 'TIME_FORMAT')
                    }
                else:
                    s = ngettext(
                        '%(count)d day before event end at %(time)s',
                        '%(count)d days before event end at %(time)s',
                        self.send_offset_days
                    ) % {
                        'count': self.send_offset_days,
                        'time': date_format(self.send_offset_time, 'TIME_FORMAT')
                    }
            else:
                if self.offset_is_after:
                    s = ngettext(
                        '%(count)d day after event start at %(time)s',
                        '%(count)d days after event start at %(time)s',
                        self.send_offset_days
                    ) % {
                        'count': self.send_offset_days,
                        'time': date_format(self.send_offset_time, 'TIME_FORMAT')
                    }
                else:
                    s = ngettext(
                        '%(count)d day before event start at %(time)s',
                        '%(count)d days before event start at %(time)s',
                        self.send_offset_days
                    ) % {
                        'count': self.send_offset_days,
                        'time': date_format(self.send_offset_time, 'TIME_FORMAT')
                    }
            return s.format(days=self.send_offset_days, time=self.send_offset_time)
