from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _

from pretix.base.models import SubEvent
from pretix.base.signals import logentry_display
from pretix.control.signals import nav_event
from pretix.plugins.sendmail.models import ScheduledMail


@receiver(post_save, sender=SubEvent)
def sendmail(sender, **kwargs):

    subevent = kwargs.get('instance')
    event = subevent.event
    rules = event.rule_set
    qs = ScheduledMail.objects.filter(subevent=subevent)

    if not qs.exists():
        for rule in rules.all():
            if not qs.filter(rule=rule).exists():
                ScheduledMail.objects.create(rule=rule, event=event, subevent=subevent)


@receiver(nav_event, dispatch_uid="sendmail_nav")
def control_nav_import(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(request.organizer, request.event, 'can_change_orders', request=request):
        return []
    return [
        {
            'label': _('Send out emails'),
            'url': reverse('plugins:sendmail:send', kwargs={
                'event': request.event.slug,
                'organizer': request.event.organizer.slug,
            }),
            'icon': 'envelope',
            'children': [
                {
                    'label': _('Send email'),
                    'url': reverse('plugins:sendmail:send', kwargs={
                        'event': request.event.slug,
                        'organizer': request.event.organizer.slug,
                    }),
                    'active': (url.namespace == 'plugins:sendmail' and url.url_name == 'send'),
                },
                {
                    'label': _('Email history'),
                    'url': reverse('plugins:sendmail:history', kwargs={
                        'event': request.event.slug,
                        'organizer': request.event.organizer.slug,
                    }),
                    'active': (url.namespace == 'plugins:sendmail' and url.url_name == 'history'),
                },
                {
                    'label': _('Email rules'),
                    'url': reverse('plugins:sendmail:rule.list', kwargs={
                        'event': request.event.slug,
                        'organizer': request.event.organizer.slug,
                    }),
                    'active': (url.namespace == 'plugins:sendmail' and url.url_name.startswith('rule.')),
                },
            ]
        },
    ]


@receiver(signal=logentry_display)
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    plains = {
        'pretix.plugins.sendmail.sent': _('Email was sent'),
        'pretix.plugins.sendmail.order.email.sent': _('The order received a mass email.'),
        'pretix.plugins.sendmail.order.email.sent.attendee': _('A ticket holder of this order received a mass email.'),
        'pretix.plugins.sendmail.rule.sent': _('A scheduled email was sent to the order'),
        'pretix.plugins.sendmail.rule.sent.attendee': _('A scheduled email was sent to a ticket holder'),
        'pretix.plugins.sendmail.rule.deleted': _('An email rule was deleted'),
    }
    if logentry.action_type in plains:
        return plains[logentry.action_type]
