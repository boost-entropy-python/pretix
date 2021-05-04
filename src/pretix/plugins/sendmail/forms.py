from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from django_scopes.forms import SafeModelMultipleChoiceField
from i18nfield.forms import I18nFormField, I18nTextarea, I18nTextInput

from pretix.base.email import get_available_placeholders
from pretix.base.forms import PlaceholderValidator, I18nModelForm
from pretix.base.forms.widgets import SplitDateTimePickerWidget, TimePickerWidget
from pretix.base.models import CheckinList, Item, Order, SubEvent
from pretix.control.forms import CachedFileField, SplitDateTimeField
from pretix.control.forms.widgets import Select2, Select2Multiple
from pretix.plugins.sendmail.models import Rule


class FormPlaceholderMixin:
    def _set_field_placeholders(self, fn, base_parameters):
        phs = [
            '{%s}' % p
            for p in sorted(get_available_placeholders(self.event, base_parameters).keys())
        ]
        ht = _('Available placeholders: {list}').format(
            list=', '.join(phs)
        )
        if self.fields[fn].help_text:
            self.fields[fn].help_text += ' ' + str(ht)
        else:
            self.fields[fn].help_text = ht
        self.fields[fn].validators.append(
            PlaceholderValidator(phs)
        )


class MailForm(FormPlaceholderMixin, forms.Form):
    recipients = forms.ChoiceField(
        label=_('Send email to'),
        widget=forms.RadioSelect,
        initial='orders',
        choices=[]
    )
    sendto = forms.MultipleChoiceField()  # overridden later
    subject = forms.CharField(label=_("Subject"))
    message = forms.CharField(label=_("Message"))
    attachment = CachedFileField(
        label=_("Attachment"),
        required=False,
        ext_whitelist=(
            ".png", ".jpg", ".gif", ".jpeg", ".pdf", ".txt", ".docx", ".gif", ".svg",
            ".pptx", ".ppt", ".doc", ".xlsx", ".xls", ".jfif", ".heic", ".heif", ".pages",
            ".bmp", ".tif", ".tiff"
        ),
        help_text=_('Sending an attachment increases the chance of your email not arriving or being sorted into spam folders. We recommend only using PDFs '
                    'of no more than 2 MB in size.'),
        max_size=10 * 1024 * 1024
    )  # TODO i18n
    items = forms.ModelMultipleChoiceField(
        widget=forms.CheckboxSelectMultiple(
            attrs={'class': 'scrolling-multiple-choice'}
        ),
        label=_('Only send to people who bought'),
        required=True,
        queryset=Item.objects.none()
    )
    filter_checkins = forms.BooleanField(
        label=_('Filter check-in status'),
        required=False
    )
    checkin_lists = SafeModelMultipleChoiceField(queryset=CheckinList.objects.none(), required=False)  # overridden later
    not_checked_in = forms.BooleanField(label=_("Send to customers not checked in"), required=False)
    subevent = forms.ModelChoiceField(
        SubEvent.objects.none(),
        label=_('Only send to customers of'),
        required=False,
        empty_label=pgettext_lazy('subevent', 'All dates')
    )
    subevents_from = forms.SplitDateTimeField(
        widget=SplitDateTimePickerWidget(),
        label=pgettext_lazy('subevent', 'Only send to customers of dates starting at or after'),
        required=False,
    )
    subevents_to = forms.SplitDateTimeField(
        widget=SplitDateTimePickerWidget(),
        label=pgettext_lazy('subevent', 'Only send to customers of dates starting before'),
        required=False,
    )

    def clean(self):
        d = super().clean()
        if d.get('subevent') and d.get('subevents_from'):
            raise ValidationError(pgettext_lazy('subevent', 'Please either select a specific date or a date range, not both.'))
        if bool(d.get('subevents_from')) != bool(d.get('subevents_to')):
            raise ValidationError(pgettext_lazy('subevent', 'If you set a date range, please set both a start and an end.'))
        return d

    def __init__(self, *args, **kwargs):
        event = self.event = kwargs.pop('event')
        super().__init__(*args, **kwargs)

        recp_choices = [
            ('orders', _('Everyone who created a ticket order'))
        ]
        if event.settings.attendee_emails_asked:
            recp_choices += [
                ('attendees', _('Every attendee (falling back to the order contact when no attendee email address is '
                                'given)')),
                ('both', _('Both (all order contact addresses and all attendee email addresses)'))
            ]
        self.fields['recipients'].choices = recp_choices

        self.fields['subject'] = I18nFormField(
            label=_('Subject'),
            widget=I18nTextInput, required=True,
            locales=event.settings.get('locales'),
        )
        self.fields['message'] = I18nFormField(
            label=_('Message'),
            widget=I18nTextarea, required=True,
            locales=event.settings.get('locales'),
        )
        self._set_field_placeholders('subject', ['event', 'order', 'position_or_address'])
        self._set_field_placeholders('message', ['event', 'order', 'position_or_address'])
        choices = [(e, l) for e, l in Order.STATUS_CHOICE if e != 'n']
        choices.insert(0, ('na', _('payment pending (except unapproved)')))
        choices.insert(0, ('pa', _('approval pending')))
        if not event.settings.get('payment_term_expire_automatically', as_type=bool):
            choices.append(
                ('overdue', _('pending with payment overdue'))
            )
        self.fields['sendto'] = forms.MultipleChoiceField(
            label=_("Send to customers with order status"),
            widget=forms.CheckboxSelectMultiple(
                attrs={'class': 'scrolling-multiple-choice'}
            ),
            choices=choices
        )
        if not self.initial.get('sendto'):
            self.initial['sendto'] = ['p', 'na']
        elif 'n' in self.initial['sendto']:
            self.initial['sendto'].append('pa')
            self.initial['sendto'].append('na')

        self.fields['items'].queryset = event.items.all()
        if not self.initial.get('items'):
            self.initial['items'] = event.items.all()

        self.fields['checkin_lists'].queryset = event.checkin_lists.all()
        self.fields['checkin_lists'].widget = Select2Multiple(
            attrs={
                'data-model-select2': 'generic',
                'data-select2-url': reverse('control:event.orders.checkinlists.select2', kwargs={
                    'event': event.slug,
                    'organizer': event.organizer.slug,
                }),
                'data-placeholder': _('Send to customers checked in on list'),
            }
        )
        self.fields['checkin_lists'].widget.choices = self.fields['checkin_lists'].choices
        self.fields['checkin_lists'].label = _('Send to customers checked in on list')

        if event.has_subevents:
            self.fields['subevent'].queryset = event.subevents.all()
            self.fields['subevent'].widget = Select2(
                attrs={
                    'data-model-select2': 'event',
                    'data-select2-url': reverse('control:event.subevents.select2', kwargs={
                        'event': event.slug,
                        'organizer': event.organizer.slug,
                    }),
                    'data-placeholder': pgettext_lazy('subevent', 'Date')
                }
            )
            self.fields['subevent'].widget.choices = self.fields['subevent'].choices
        else:
            del self.fields['subevent']
            del self.fields['subevents_from']
            del self.fields['subevents_to']


class RuleForm(FormPlaceholderMixin, I18nModelForm):
    class Meta:
        model = Rule

        fields = ['subject', 'template',
                  'date_is_absolute',
                  'send_date', 'send_offset_days', 'send_offset_time',
                  'include_pending', 'all_products', 'limit_products',
                  'send_to']

        field_classes = {
            'subevent': SafeModelMultipleChoiceField,
            'limit_products': SafeModelMultipleChoiceField,
            'send_date': SplitDateTimeField,
            'date_is_absolute': forms.ChoiceField,
        }

        widgets = {
            'send_date': SplitDateTimePickerWidget(attrs={
                'data-display-dependency': '#id_date_is_absolute_0',
            }),
            'send_offset_days': forms.NumberInput(attrs={
                'data-display-dependency': '#id_date_is_absolute_1,#id_date_is_absolute_2,#id_date_is_absolute_3,'
                                           '#id_date_is_absolute_4',
            }),
            'send_offset_time': TimePickerWidget(attrs={
                'data-display-dependency': '#id_date_is_absolute_1,#id_date_is_absolute_2,#id_date_is_absolute_3,'
                                           '#id_date_is_absolute_4',
            }),
            'limit_products': forms.CheckboxSelectMultiple(
                attrs={'class': 'scrolling-multiple-choice',
                       'data-inverse-dependency': '#id_all_products'},
            ),
            'date_is_absolute': forms.RadioSelect,
            'send_to': forms.RadioSelect,
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')

        if instance:
            if instance.date_is_absolute:
                dia = "abs"
            else:
                dia = "rel"
                dia += "_a" if instance.offset_is_after else "_b"
                dia += "_e" if instance.offset_to_event_end else "_s"

        else:
            dia = "abs"

        kwargs.setdefault('initial', {})
        kwargs['initial']['date_is_absolute'] = dia

        super().__init__(*args, **kwargs)

        self.fields['limit_products'].queryset = Item.objects.filter(event=self.event)

        self.fields['date_is_absolute'].choices = [('abs', _('Absolute')),
                                                   ('rel_b_s', _('Relative, before event start')),
                                                   ('rel_b_e', _('Relative, before event end')),
                                                   ('rel_a_s', _('Relative, after event start')),
                                                   ('rel_a_e', _('Relative, after event end'))]

        self._set_field_placeholders('subject', ['event', 'order'])
        self._set_field_placeholders('template', ['event', 'order'])

    def clean(self):
        d = super().clean()

        dia = d.get('date_is_absolute')
        if dia == 'abs':
            if not d.get('send_date'):
                raise ValidationError(_('Please specify the send date'))
            d['date_is_absolute'] = True
            d['send_offset_days'] = d['send_offset_time'] = None
        else:
            if not (d.get('send_offset_days') and d.get('send_offset_time')):
                raise ValidationError(_('Please specify the offset days and time'))
            d['offset_is_after'] = '_a' in dia
            d['offset_to_event_end'] = '_e' in dia
            d['date_is_absolute'] = False
            d['send_date'] = None

        if d.get('all_products'):
            # having products checked while the option is ignored is probably counterintuitive
            d['limit_products'] = Item.objects.none()
        else:
            if not d.get('limit_products'):
                raise ValidationError(_('Please specify a product'))

        self.instance.offset_is_after = d.get('offset_is_after', False)
        self.instance.offset_to_event_end = d.get('offset_to_event_end', False)

        return d
