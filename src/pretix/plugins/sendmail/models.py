from datetime import timedelta

from django.db import models, transaction
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _, pgettext_lazy, ngettext
from django_scopes import ScopedManager
from i18nfield.fields import I18nCharField, I18nTextField
from i18nfield.strings import LazyI18nString

from pretix.base.email import get_email_context
from pretix.base.models import Event, SubEvent, OrderPosition, Item, InvoiceAddress, Order
from pretix.base.services.mail import SendMailException


class ScheduledMail(models.Model):
    rule = models.ForeignKey("Rule", on_delete=models.CASCADE)
    subevent = models.ForeignKey(SubEvent, null=True, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)

    sent = models.BooleanField(default=False)
    last_computed = models.DateTimeField(auto_now_add=True)
    computed_datetime = models.DateTimeField()

    def save(self, **kwargs):
        if not self.computed_datetime:
            self.recompute()
        super().save(**kwargs)

    def recompute(self):
        if self.rule.date_is_absolute:
            self.computed_datetime = self.rule.send_date
        else:
            e = self.subevent or self.event
            o_days = self.rule.send_offset_days
            if not self.rule.offset_is_after:
                o_days *= -1

            offset = timedelta(days=o_days)
            st = self.rule.send_offset_time
            base_time = (e.date_to or e.date_from) if self.rule.offset_to_event_end else e.date_from
            d = base_time.astimezone(self.event.timezone) + offset
            self.computed_datetime = d.replace(hour=st.hour, minute=st.minute, second=st.second)

        self.last_computed = timezone.now()

    def send(self):
        e = self.event
        if self.subevent:
            orders = e.orders.annotate(has_matching_position=Exists(OrderPosition.objects.filter(
                order=OuterRef('pk'), subevent=self.subevent))).filter(has_matching_position=True)

        else:
            orders = e.orders.all()

        status = [Order.STATUS_PENDING, Order.STATUS_PAID] if self.rule.include_pending else [Order.STATUS_PAID]

        orders = orders.filter(status__in=status).select_related('invoice_address').prefetch_related('positions')

        send_to_orders = self.rule.send_to in (Rule.CUSTOMERS, Rule.BOTH)
        send_to_attendees = self.rule.send_to in (Rule.ATTENDEES, Rule.BOTH)

        for o in orders:
            o_sent = False

            try:
                ia = o.invoice_address
            except InvoiceAddress.DoesNotExist:
                ia = InvoiceAddress(order=o)

            if send_to_orders:
                email_ctx = get_email_context(event=e, order=o, position_or_address=ia)
                try:
                    o.send_mail(self.rule.subject, self.rule.template, email_ctx,
                                log_entry_type='pretix.plugins.sendmail.rule.sent')
                    o_sent = True
                except SendMailException:
                    ...  # ¯\_(ツ)_/¯

            if send_to_attendees:
                positions = o.positions.all()
                if not self.rule.all_products:
                    positions = positions.filter(item__in=self.rule.limit_products.all())

                for p in positions:
                    email_ctx = get_email_context(event=e, order=o, position_or_address=ia, position=p)
                    try:
                        if p.attendee_email:
                            p.send_mail(self.rule.subject, self.rule.template, email_ctx,
                                        log_entry_type='pretix.plugins.sendmail.rule.sent.attendee')
                        elif not o_sent:
                            o.send_mail(self.rule.subject, self.rule.template, email_ctx,
                                        log_entry_type='pretix.plugins.sendmail.rule.sent')
                            o_sent = True
                    except SendMailException:
                        ...  # ¯\_(ツ)_/¯

        self.sent = True
        self.save()


class Rule(models.Model):
    CUSTOMERS = "orders"
    ATTENDEES = "attendees"
    BOTH = "both"

    SEND_TO_CHOICES = [
        (CUSTOMERS, _("Customers")),
        (ATTENDEES, _("Attendees")),
        (BOTH, _("Both customers and attendees"))
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE)

    subject = I18nCharField(max_length=255, verbose_name=_('Subject'))
    template = I18nTextField(verbose_name=_('Message'))

    all_products = models.BooleanField(default=True, verbose_name=_('All products'))
    limit_products = models.ManyToManyField(Item, blank=True, verbose_name=_('Limit products'))

    include_pending = models.BooleanField(default=False, verbose_name=_('Include pending orders'))

    # either send_date or send_offset_* have to be set
    send_date = models.DateTimeField(null=True, blank=True, verbose_name=_('Send date'))
    send_offset_days = models.IntegerField(null=True, blank=True, verbose_name=_('Number of days'))
    send_offset_time = models.TimeField(null=True, blank=True, verbose_name=_('Time of day'))

    date_is_absolute = models.BooleanField(default=True, blank=True, verbose_name=_('Type of schedule time'))
    offset_to_event_end = models.BooleanField(default=False, blank=True)  # no verbose name because not actually
    offset_is_after = models.BooleanField(default=False, blank=True)      # displayed in any forms

    send_to = models.CharField(max_length=10, choices=SEND_TO_CHOICES, default=CUSTOMERS)

    objects = ScopedManager(organizer='event__organizer')

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        super().save(force_insert, force_update, using, update_fields)

        if self.event.has_subevents:
            for se in self.event.subevents.annotate(has_sm=Exists(ScheduledMail.objects.filter(
                    subevent=OuterRef('pk'), rule=self))).filter(has_sm=False):
                ScheduledMail.objects.create(rule=self, subevent=se, event=self.event)
        else:
            if not ScheduledMail.objects.filter(rule=self, event=self.event).exists():
                ScheduledMail.objects.create(rule=self, event=self.event)

        for sm in self.scheduledmail_set.all():
            sm.recompute()

    @property
    def human_readable_time(self):
        if self.date_is_absolute:
            d = self.send_date.astimezone(self.event.timezone)
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
            return s

    @property
    def total_mails(self):
        return len(ScheduledMail.objects.filter(rule=self))

    @property
    def sent_mails(self):
        return len(ScheduledMail.objects.filter(rule=self, sent=True))
