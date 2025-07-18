#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020 Raphael Michel and contributors
# Copyright (C) 2020-2021 rami.io GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
import datetime
import logging
import mimetypes
import os
from decimal import Decimal
from zoneinfo import ZoneInfo

import django_filters
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import (
    Exists, F, OuterRef, Prefetch, Q, Subquery, prefetch_related_objects,
)
from django.db.models.functions import Coalesce, Concat
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import formats
from django.utils.timezone import make_aware, now
from django.utils.translation import gettext as _
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
from django_scopes import scopes_disabled
from PIL import Image
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import (
    APIException, NotFound, PermissionDenied, ValidationError,
)
from rest_framework.mixins import CreateModelMixin
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from pretix.api.filters import MultipleCharFilter
from pretix.api.models import OAuthAccessToken
from pretix.api.pagination import TotalOrderingFilter
from pretix.api.serializers.order import (
    BlockedTicketSecretSerializer, InvoiceSerializer, OrderCreateSerializer,
    OrderPaymentCreateSerializer, OrderPaymentSerializer,
    OrderPositionSerializer, OrderRefundCreateSerializer,
    OrderRefundSerializer, OrderSerializer, OrganizerTransactionSerializer,
    PriceCalcSerializer, PrintLogSerializer, RevokedTicketSecretSerializer,
    SimulatedOrderSerializer, TransactionSerializer,
)
from pretix.api.serializers.orderchange import (
    BlockNameSerializer, OrderChangeOperationSerializer,
    OrderFeeChangeSerializer, OrderFeeCreateForExistingOrderSerializer,
    OrderPositionChangeSerializer,
    OrderPositionCreateForExistingOrderSerializer,
    OrderPositionInfoPatchSerializer,
)
from pretix.api.views import RichOrderingFilter
from pretix.base.decimal import round_decimal
from pretix.base.i18n import language
from pretix.base.models import (
    CachedCombinedTicket, CachedTicket, Checkin, Device, EventMetaValue,
    Invoice, InvoiceAddress, ItemMetaValue, ItemVariation,
    ItemVariationMetaValue, Order, OrderFee, OrderPayment, OrderPosition,
    OrderRefund, Quota, ReusableMedium, SubEvent, SubEventMetaValue, TaxRule,
    TeamAPIToken, generate_secret,
)
from pretix.base.models.orders import (
    BlockedTicketSecret, PrintLog, QuestionAnswer, RevokedTicketSecret,
    Transaction,
)
from pretix.base.payment import PaymentException
from pretix.base.pdf import get_images
from pretix.base.secrets import assign_ticket_secret
from pretix.base.services import tickets
from pretix.base.services.invoices import (
    generate_cancellation, generate_invoice, invoice_pdf, invoice_qualified,
    regenerate_invoice,
)
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import (
    OrderChangeManager, OrderError, _order_placed_email,
    _order_placed_email_attendee, approve_order, cancel_order, deny_order,
    extend_order, mark_order_expired, mark_order_refunded, reactivate_order,
)
from pretix.base.services.pricing import get_price
from pretix.base.services.tickets import generate
from pretix.base.signals import (
    order_modified, order_paid, order_placed, register_ticket_outputs,
)
from pretix.control.signals import order_search_filter_q
from pretix.helpers import OF_SELF

logger = logging.getLogger(__name__)

with scopes_disabled():
    class OrderFilter(FilterSet):
        email = django_filters.CharFilter(field_name='email', lookup_expr='iexact')
        code = django_filters.CharFilter(field_name='code', lookup_expr='iexact')
        status = django_filters.CharFilter(field_name='status', lookup_expr='iexact')
        modified_since = django_filters.IsoDateTimeFilter(field_name='last_modified', lookup_expr='gte')
        created_since = django_filters.IsoDateTimeFilter(field_name='datetime', lookup_expr='gte')
        created_before = django_filters.IsoDateTimeFilter(field_name='datetime', lookup_expr='lt')
        subevent_after = django_filters.IsoDateTimeFilter(method='subevent_after_qs')
        subevent_before = django_filters.IsoDateTimeFilter(method='subevent_before_qs')
        search = django_filters.CharFilter(method='search_qs')
        item = django_filters.CharFilter(field_name='all_positions', lookup_expr='item_id', distinct=True)
        variation = django_filters.CharFilter(field_name='all_positions', lookup_expr='variation_id', distinct=True)
        subevent = django_filters.CharFilter(field_name='all_positions', lookup_expr='subevent_id', distinct=True)
        customer = django_filters.CharFilter(field_name='customer__identifier')
        sales_channel = django_filters.CharFilter(field_name='sales_channel__identifier')
        payment_provider = django_filters.CharFilter(method='provider_qs')

        class Meta:
            model = Order
            fields = ['code', 'status', 'email', 'locale', 'testmode', 'require_approval', 'customer']

        @scopes_disabled()
        def subevent_after_qs(self, qs, name, value):
            if getattr(self.request, 'event', None):
                subevents = self.request.event.subevents
            else:
                subevents = SubEvent.objects.filter(event__organizer=self.request.organizer)

            qs = qs.filter(
                pk__in=Subquery(
                    OrderPosition.all.filter(
                        subevent_id__in=subevents.filter(
                            Q(date_to__gt=value) | Q(date_from__gt=value, date_to__isnull=True),
                        ).values_list('id'),
                    ).values_list('order_id')
                )
            )
            return qs

        def provider_qs(self, qs, name, value):
            return qs.filter(Exists(
                OrderPayment.objects.filter(order=OuterRef('pk'), provider=value)
            ))

        def subevent_before_qs(self, qs, name, value):
            if getattr(self.request, 'event', None):
                subevents = self.request.event.subevents
            else:
                subevents = SubEvent.objects.filter(event__organizer=self.request.organizer)

            qs = qs.filter(
                pk__in=Subquery(
                    OrderPosition.all.filter(
                        subevent_id__in=subevents.filter(
                            Q(date_from__lt=value),
                        ).values_list('id'),
                    ).values_list('order_id')
                )
            )
            return qs

        def search_qs(self, qs, name, value):
            u = value
            if "-" in value:
                code = (Q(event__slug__icontains=u.rsplit("-", 1)[0])
                        & Q(code__icontains=Order.normalize_code(u.rsplit("-", 1)[1])))
            else:
                code = Q(code__icontains=Order.normalize_code(u))

            invoice_nos = {u, u.upper()}
            if u.isdigit():
                for i in range(2, 12):
                    invoice_nos.add(u.zfill(i))

            matching_invoices = Invoice.objects.filter(
                Q(invoice_no__in=invoice_nos)
                | Q(full_invoice_no__iexact=u)
            ).values_list('order_id', flat=True)

            matching_positions = OrderPosition.all.filter(
                Q(order=OuterRef('pk')) & Q(
                    Q(attendee_name_cached__icontains=u) | Q(attendee_email__icontains=u)
                    | Q(secret__istartswith=u)
                    # | Q(voucher__code__icontains=u)  # temporarily removed since it caused bad query performance on postgres
                )
            ).values('id')

            matching_media = ReusableMedium.objects.filter(identifier=u).values_list('linked_orderposition__order_id', flat=True)

            mainq = (
                code
                | Q(email__icontains=u)
                | Q(invoice_address__name_cached__icontains=u)
                | Q(invoice_address__company__icontains=u)
                | Q(pk__in=matching_invoices)
                | Q(pk__in=matching_media)
                | Q(comment__icontains=u)
                | Q(has_pos=True)
            )
            for recv, q in order_search_filter_q.send(sender=getattr(self, 'event', None), query=u):
                mainq = mainq | q
            return qs.annotate(has_pos=Exists(matching_positions)).filter(
                mainq
            )


class OrderViewSetMixin:
    serializer_class = OrderSerializer
    queryset = Order.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('datetime',)
    ordering_fields = ('datetime', 'code', 'status', 'last_modified', 'cancellation_date')
    filterset_class = OrderFilter
    lookup_field = 'code'

    def get_base_queryset(self):
        raise NotImplementedError()

    def get_queryset(self):
        qs = self.get_base_queryset()
        if 'fees' not in self.request.GET.getlist('exclude'):
            if self.request.query_params.get('include_canceled_fees', 'false') == 'true':
                fqs = OrderFee.all
            else:
                fqs = OrderFee.objects
            qs = qs.prefetch_related(Prefetch('fees', queryset=fqs.all()))
        if 'payments' not in self.request.GET.getlist('exclude'):
            qs = qs.prefetch_related('payments')
        if 'refunds' not in self.request.GET.getlist('exclude'):
            qs = qs.prefetch_related('refunds', 'refunds__payment')
        if 'invoice_address' not in self.request.GET.getlist('exclude'):
            qs = qs.select_related('invoice_address')
        if 'customer' not in self.request.GET.getlist('exclude'):
            qs = qs.select_related('customer')

        qs = qs.select_related('sales_channel').prefetch_related(self._positions_prefetch(self.request))
        return qs

    def _positions_prefetch(self, request):
        if request.query_params.get('include_canceled_positions', 'false') == 'true':
            opq = OrderPosition.all
        else:
            opq = OrderPosition.objects
        if request.query_params.get('pdf_data', 'false') == 'true' and getattr(request, 'event', None):
            prefetch_related_objects([request.organizer], 'meta_properties')
            prefetch_related_objects(
                [request.event],
                Prefetch('meta_values', queryset=EventMetaValue.objects.select_related('property'),
                         to_attr='meta_values_cached'),
                'questions',
                'item_meta_properties',
            )
            return Prefetch(
                'positions',
                opq.all().prefetch_related(
                    Prefetch('checkins', queryset=Checkin.objects.select_related('device')),
                    Prefetch('print_logs', queryset=PrintLog.objects.select_related('device')),
                    Prefetch('item', queryset=self.request.event.items.prefetch_related(
                        Prefetch('meta_values', ItemMetaValue.objects.select_related('property'), to_attr='meta_values_cached')
                    )),
                    Prefetch('variation', queryset=ItemVariation.objects.prefetch_related(
                        Prefetch('meta_values', ItemVariationMetaValue.objects.select_related('property'), to_attr='meta_values_cached')
                    )),
                    'answers', 'answers__options', 'answers__question',
                    'item__category',
                    'addon_to__answers', 'addon_to__answers__options', 'addon_to__answers__question',
                    Prefetch('subevent', queryset=self.request.event.subevents.prefetch_related(
                        Prefetch('meta_values', to_attr='meta_values_cached', queryset=SubEventMetaValue.objects.select_related('property'))
                    )),
                    Prefetch('addons', opq.select_related('item', 'variation', 'seat')),
                    'linked_media',
                ).select_related('seat', 'addon_to', 'addon_to__seat')
            )
        else:
            return Prefetch(
                'positions',
                opq.all().prefetch_related(
                    Prefetch('checkins', queryset=Checkin.objects.select_related('device')),
                    Prefetch('print_logs', queryset=PrintLog.objects.select_related('device')),
                    'item', 'variation',
                    Prefetch('answers', queryset=QuestionAnswer.objects.prefetch_related('options', 'question').order_by('question__position')),
                    'seat',
                )
            )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['exclude'] = self.request.query_params.getlist('exclude')
        ctx['include'] = self.request.query_params.getlist('include')
        ctx['pdf_data'] = False
        return ctx

    @scopes_disabled()  # we are sure enough that get_queryset() is correct, so we save some perforamnce
    def list(self, request, **kwargs):
        date = serializers.DateTimeField().to_representation(now())
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            resp = self.get_paginated_response(serializer.data)
            resp['X-Page-Generated'] = date
            return resp

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, headers={'X-Page-Generated': date})


class OrganizerOrderViewSet(OrderViewSetMixin, viewsets.ReadOnlyModelViewSet):
    def get_base_queryset(self):
        perm = "can_view_orders" if self.request.method in SAFE_METHODS else "can_change_orders"
        if isinstance(self.request.auth, (TeamAPIToken, Device)):
            return Order.objects.filter(
                event__organizer=self.request.organizer,
                event__in=self.request.auth.get_events_with_permission(perm, request=self.request)
            )
        elif self.request.user.is_authenticated:
            return Order.objects.filter(
                event__organizer=self.request.organizer,
                event__in=self.request.user.get_events_with_permission(perm, request=self.request)
            )
        else:
            raise PermissionDenied()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['organizer'] = self.request.organizer
        return ctx


class EventOrderViewSet(OrderViewSetMixin, viewsets.ModelViewSet):
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['pdf_data'] = self.request.query_params.get('pdf_data', 'false') == 'true'
        return ctx

    def get_base_queryset(self):
        return self.request.event.orders

    def _get_output_provider(self, identifier):
        responses = register_ticket_outputs.send(self.request.event)
        for receiver, response in responses:
            prov = response(self.request.event)
            if prov.identifier == identifier:
                return prov
        raise NotFound('Unknown output provider.')

    @action(detail=True, url_name='download', url_path='download/(?P<output>[^/]+)')
    def download(self, request, output, **kwargs):
        provider = self._get_output_provider(output)
        order = self.get_object()

        if order.status in (Order.STATUS_CANCELED, Order.STATUS_EXPIRED):
            raise PermissionDenied("Downloads are not available for canceled or expired orders.")

        if order.status == Order.STATUS_PENDING and not (order.valid_if_pending or request.event.settings.ticket_download_pending):
            raise PermissionDenied("Downloads are not available for pending orders.")

        ct = CachedCombinedTicket.objects.filter(
            order=order, provider=provider.identifier, file__isnull=False
        ).last()
        if not ct or not ct.file:
            generate.apply_async(args=('order', order.pk, provider.identifier))
            raise RetryException()
        else:
            if ct.type == 'text/uri-list':
                resp = HttpResponse(ct.file.file.read(), content_type='text/uri-list')
                return resp
            else:
                resp = FileResponse(ct.file.file, content_type=ct.type)
                resp['Content-Disposition'] = 'attachment; filename="{}-{}-{}{}"'.format(
                    self.request.event.slug.upper(), order.code,
                    provider.identifier, ct.extension
                )
                return resp

    @action(detail=True, methods=['POST'])
    def mark_paid(self, request, **kwargs):
        order = self.get_object()
        send_mail = request.data.get('send_email', True) if request.data else True

        if order.status in (Order.STATUS_PENDING, Order.STATUS_EXPIRED):

            ps = order.pending_sum
            try:
                p = order.payments.get(
                    state__in=(OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED),
                    provider='manual',
                    amount=ps
                )
            except OrderPayment.DoesNotExist:
                for p in order.payments.filter(state__in=(OrderPayment.PAYMENT_STATE_PENDING,
                                                          OrderPayment.PAYMENT_STATE_CREATED)):
                    try:
                        with transaction.atomic():
                            p.payment_provider.cancel_payment(p)
                            order.log_action('pretix.event.order.payment.canceled', {
                                'local_id': p.local_id,
                                'provider': p.provider,
                            }, user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)
                    except PaymentException as e:
                        order.log_action(
                            'pretix.event.order.payment.canceled.failed',
                            {
                                'local_id': p.local_id,
                                'provider': p.provider,
                                'error': str(e)
                            },
                            user=self.request.user if self.request.user.is_authenticated else None,
                            auth=self.request.auth
                        )
                p = order.payments.create(
                    state=OrderPayment.PAYMENT_STATE_CREATED,
                    provider='manual',
                    amount=ps,
                    fee=None
                )

            try:
                p.confirm(auth=self.request.auth,
                          user=self.request.user if request.user.is_authenticated else None,
                          send_mail=send_mail,
                          count_waitinglist=False)
            except Quota.QuotaExceededException as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except PaymentException as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except SendMailException:
                pass

            return self.retrieve(request, [], **kwargs)
        return Response(
            {'detail': 'The order is not pending or expired.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    @action(detail=True, methods=['POST'])
    def mark_canceled(self, request, **kwargs):
        send_mail = request.data.get('send_email', True) if request.data else True
        comment = request.data.get('comment', None)
        cancellation_fee = request.data.get('cancellation_fee', None)
        if cancellation_fee:
            cancellation_fee = serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(
                cancellation_fee,
            )

        order = self.get_object()
        if not order.cancel_allowed():
            return Response(
                {'detail': 'The order is not allowed to be canceled.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            cancel_order(
                order,
                user=request.user if request.user.is_authenticated else None,
                api_token=request.auth if isinstance(request.auth, TeamAPIToken) else None,
                device=request.auth if isinstance(request.auth, Device) else None,
                oauth_application=request.auth.application if isinstance(request.auth, OAuthAccessToken) else None,
                send_mail=send_mail,
                email_comment=comment,
                cancellation_fee=cancellation_fee
            )
        except OrderError as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def reactivate(self, request, **kwargs):

        order = self.get_object()
        if order.status != Order.STATUS_CANCELED:
            return Response(
                {'detail': 'The order is not allowed to be reactivated.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            reactivate_order(
                order,
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth if isinstance(request.auth, (Device, TeamAPIToken, OAuthAccessToken)) else None,
            )
        except OrderError as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def approve(self, request, **kwargs):
        send_mail = request.data.get('send_email', True) if request.data else True

        order = self.get_object()
        try:
            approve_order(
                order,
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth if isinstance(request.auth, (Device, TeamAPIToken, OAuthAccessToken)) else None,
                send_mail=send_mail,
            )
        except Quota.QuotaExceededException as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except OrderError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def deny(self, request, **kwargs):
        send_mail = request.data.get('send_email', True) if request.data else True
        comment = request.data.get('comment', '')

        order = self.get_object()
        try:
            deny_order(
                order,
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth if isinstance(request.auth, (Device, TeamAPIToken, OAuthAccessToken)) else None,
                send_mail=send_mail,
                comment=comment,
            )
        except OrderError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def mark_pending(self, request, **kwargs):
        order = self.get_object()

        if order.status != Order.STATUS_PAID:
            return Response(
                {'detail': 'The order is not paid.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        order.status = Order.STATUS_PENDING
        order.save(update_fields=['status'])
        order.log_action(
            'pretix.event.order.unpaid',
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth,
        )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def mark_expired(self, request, **kwargs):
        order = self.get_object()

        if order.status != Order.STATUS_PENDING:
            return Response(
                {'detail': 'The order is not pending.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        mark_order_expired(
            order,
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth,
        )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def mark_refunded(self, request, **kwargs):
        order = self.get_object()

        if order.status != Order.STATUS_PAID:
            return Response(
                {'detail': 'The order is not paid.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        mark_order_refunded(
            order,
            user=request.user if request.user.is_authenticated else None,
            auth=(request.auth if isinstance(request.auth, (TeamAPIToken, OAuthAccessToken, Device)) else None),
        )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    @transaction.atomic()
    def create_invoice(self, request, **kwargs):
        order = self.get_object()
        order = Order.objects.select_for_update(of=OF_SELF).get(pk=order.pk)
        has_inv = order.invoices.exists() and not (
            order.status in (Order.STATUS_PAID, Order.STATUS_PENDING)
            and order.invoices.filter(is_cancellation=True).count() >= order.invoices.filter(is_cancellation=False).count()
        )
        if self.request.event.settings.get('invoice_generate') not in ('admin', 'user', 'paid', 'user_paid', 'True') or not invoice_qualified(order):
            return Response(
                {'detail': _('You cannot generate an invoice for this order.')},
                status=status.HTTP_400_BAD_REQUEST
            )
        elif has_inv:
            return Response(
                {'detail': _('An invoice for this order already exists.')},
                status=status.HTTP_400_BAD_REQUEST
            )

        inv = generate_invoice(order)
        order.log_action(
            'pretix.event.order.invoice.generated',
            user=self.request.user,
            auth=self.request.auth,
            data={
                'invoice': inv.pk
            }
        )
        return Response(
            InvoiceSerializer(inv).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['POST'])
    def resend_link(self, request, **kwargs):
        order = self.get_object()
        if not order.email:
            return Response({'detail': 'There is no email address associated with this order.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            order.resend_link(user=self.request.user, auth=self.request.auth)
        except SendMailException:
            return Response({'detail': _('There was an error sending the mail. Please try again later.')}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            status=status.HTTP_204_NO_CONTENT
        )

    @action(detail=True, methods=['POST'])
    @transaction.atomic
    def regenerate_secrets(self, request, **kwargs):
        order = self.get_object()
        order.secret = generate_secret()
        for op in order.all_positions.all():
            op.web_secret = generate_secret()
            op.save(update_fields=["web_secret"])
            assign_ticket_secret(
                request.event, op, force_invalidate=True, save=True
            )
        order.save(update_fields=['secret'])
        CachedTicket.objects.filter(order_position__order=order).delete()
        CachedCombinedTicket.objects.filter(order=order).delete()
        tickets.invalidate_cache.apply_async(kwargs={'event': self.request.event.pk,
                                                     'order': order.pk})
        order.log_action(
            'pretix.event.order.secret.changed',
            user=self.request.user,
            auth=self.request.auth,
        )
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def extend(self, request, **kwargs):
        new_date = request.data.get('expires', None)
        force = request.data.get('force', False)
        if not new_date:
            return Response(
                {'detail': 'New date is missing.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        df = serializers.DateField()
        try:
            new_date = df.to_internal_value(new_date)
        except:
            return Response(
                {'detail': 'New date is invalid.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        tz = ZoneInfo(self.request.event.settings.timezone)
        new_date = make_aware(datetime.datetime.combine(
            new_date,
            datetime.time(hour=23, minute=59, second=59)
        ), tz)

        order = self.get_object()

        try:
            extend_order(
                order,
                new_date=new_date,
                force=force,
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth,
            )
            return self.retrieve(request, [], **kwargs)
        except OrderError as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    def create(self, request, *args, **kwargs):
        if 'send_mail' in request.data and 'send_email' not in request.data and isinstance(request.data, dict):
            request.data['send_email'] = request.data['send_mail']
        serializer = OrderCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            try:
                self.perform_create(serializer)
            except TaxRule.SaleNotAllowed:
                raise ValidationError(_('One of the selected products is not available in the selected country.'))
            send_mail = serializer._send_mail
            order = serializer.instance

            if not order.pk:
                # Simulation -- exit here
                serializer = SimulatedOrderSerializer(order, context=serializer.context)
                return Response(serializer.data, status=status.HTTP_201_CREATED)

            order.log_action(
                'pretix.event.order.placed',
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth,
            )

        with language(order.locale, self.request.event.settings.region):
            payment = order.payments.last()
            # OrderCreateSerializer creates at most one payment
            if payment and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                order.log_action(
                    'pretix.event.order.payment.confirmed', {
                        'local_id': payment.local_id,
                        'provider': payment.provider,
                    },
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth,
                )
            order_placed.send(self.request.event, order=order)
            if order.status == Order.STATUS_PAID:
                order_paid.send(self.request.event, order=order)
                order.log_action(
                    'pretix.event.order.paid',
                    {
                        'provider': payment.provider if payment else None,
                        'info': {},
                        'date': now().isoformat(),
                        'force': False
                    },
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth,
                )

            gen_invoice = invoice_qualified(order) and (
                (order.event.settings.get('invoice_generate') == 'True') or
                (order.event.settings.get('invoice_generate') == 'paid' and order.status == Order.STATUS_PAID)
            ) and not order.invoices.last()
            invoice = None
            if gen_invoice:
                invoice = generate_invoice(order, trigger_pdf=True)

            # Refresh serializer only after running signals
            prefetch_related_objects([order], self._positions_prefetch(request))
            serializer = OrderSerializer(order, context=serializer.context)

            if send_mail:
                free_flow = (
                    payment and order.total == Decimal('0.00') and order.status == Order.STATUS_PAID and
                    not order.require_approval and payment.provider in ("free", "boxoffice")
                )
                if order.require_approval:
                    email_template = request.event.settings.mail_text_order_placed_require_approval
                    subject_template = request.event.settings.mail_subject_order_placed_require_approval
                    log_entry = 'pretix.event.order.email.order_placed_require_approval'
                    email_attendees = False
                elif free_flow:
                    email_template = request.event.settings.mail_text_order_free
                    subject_template = request.event.settings.mail_subject_order_free
                    log_entry = 'pretix.event.order.email.order_free'
                    email_attendees = request.event.settings.mail_send_order_free_attendee
                    email_attendees_template = request.event.settings.mail_text_order_free_attendee
                    subject_attendees_template = request.event.settings.mail_subject_order_free_attendee
                else:
                    email_template = request.event.settings.mail_text_order_placed
                    subject_template = request.event.settings.mail_subject_order_placed
                    log_entry = 'pretix.event.order.email.order_placed'
                    email_attendees = request.event.settings.mail_send_order_placed_attendee
                    email_attendees_template = request.event.settings.mail_text_order_placed_attendee
                    subject_attendees_template = request.event.settings.mail_subject_order_placed_attendee

                _order_placed_email(
                    request.event, order, email_template, subject_template,
                    log_entry, invoice, [payment] if payment else [], is_free=free_flow
                )
                if email_attendees:
                    for p in order.positions.all():
                        if p.addon_to_id is None and p.attendee_email and p.attendee_email != order.email:
                            _order_placed_email_attendee(request.event, order, p, email_attendees_template, subject_attendees_template,
                                                         log_entry, is_free=free_flow)

                if not free_flow and order.status == Order.STATUS_PAID and payment:
                    payment._send_paid_mail(invoice, None, '')
                    if self.request.event.settings.mail_send_order_paid_attendee:
                        for p in order.positions.all():
                            if p.addon_to_id is None and p.attendee_email and p.attendee_email != order.email:
                                payment._send_paid_mail_attendee(p, None)

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.get('partial', False)
        if not partial:
            return Response(
                {"detail": "Method \"PUT\" not allowed."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        with transaction.atomic():
            if 'comment' in self.request.data and serializer.instance.comment != self.request.data.get('comment'):
                serializer.instance.log_action(
                    'pretix.event.order.comment',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'new_comment': self.request.data.get('comment')
                    }
                )

            if 'custom_followup_at' in self.request.data and serializer.instance.custom_followup_at != self.request.data.get('custom_followup_at'):
                serializer.instance.log_action(
                    'pretix.event.order.custom_followup_at',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'new_custom_followup_at': self.request.data.get('custom_followup_at')
                    }
                )

            if 'checkin_attention' in self.request.data and serializer.instance.checkin_attention != self.request.data.get('checkin_attention'):
                serializer.instance.log_action(
                    'pretix.event.order.checkin_attention',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'new_value': self.request.data.get('checkin_attention')
                    }
                )

            if 'checkin_text' in self.request.data and serializer.instance.checkin_text != self.request.data.get('checkin_text'):
                serializer.instance.log_action(
                    'pretix.event.order.checkin_text',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'new_value': self.request.data.get('checkin_text')
                    }
                )

            if 'valid_if_pending' in self.request.data and serializer.instance.valid_if_pending != self.request.data.get('valid_if_pending'):
                serializer.instance.log_action(
                    'pretix.event.order.valid_if_pending',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'new_value': self.request.data.get('valid_if_pending')
                    }
                )

            if 'email' in self.request.data and serializer.instance.email != self.request.data.get('email'):
                serializer.instance.email_known_to_work = False
                serializer.instance.log_action(
                    'pretix.event.order.contact.changed',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'old_email': serializer.instance.email,
                        'new_email': self.request.data.get('email'),
                    }
                )

            if 'phone' in self.request.data and serializer.instance.phone != self.request.data.get('phone'):
                serializer.instance.log_action(
                    'pretix.event.order.phone.changed',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'old_phone': serializer.instance.phone,
                        'new_phone': self.request.data.get('phone'),
                    }
                )

            if 'locale' in self.request.data and serializer.instance.locale != self.request.data.get('locale'):
                serializer.instance.log_action(
                    'pretix.event.order.locale.changed',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'old_locale': serializer.instance.locale,
                        'new_locale': self.request.data.get('locale'),
                    }
                )

            if 'invoice_address' in self.request.data:
                serializer.instance.log_action(
                    'pretix.event.order.modified',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'invoice_data': self.request.data.get('invoice_address'),
                    }
                )

            serializer.save()
            tickets.invalidate_cache.apply_async(kwargs={'event': serializer.instance.event.pk, 'order': serializer.instance.pk})

        if 'invoice_address' in self.request.data:
            order_modified.send(sender=serializer.instance.event, order=serializer.instance)

    def perform_create(self, serializer):
        try:
            serializer.save()
        except IntegrityError:
            logger.exception("Integrity error while saving order")
            raise ValidationError("Integrity error, possibly duplicate submission of same order.")

    def perform_destroy(self, instance):
        if not instance.testmode:
            raise PermissionDenied('Only test mode orders can be deleted.')

        with transaction.atomic():
            self.get_object().gracefully_delete(user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)

    @action(detail=True, methods=['POST'])
    def change(self, request, **kwargs):
        order = self.get_object()

        serializer = OrderChangeOperationSerializer(
            context={'order': order, **self.get_serializer_context()},
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)

        try:
            ocm = OrderChangeManager(
                order=order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=request.auth,
                notify=serializer.validated_data.get('send_email', False),
                reissue_invoice=serializer.validated_data.get('reissue_invoice', True),
            )

            canceled_positions = set()
            for r in serializer.validated_data.get('cancel_positions', []):
                ocm.cancel(r['position'])
                canceled_positions.add(r['position'])

            for r in serializer.validated_data.get('patch_positions', []):
                if r['position'] in canceled_positions:
                    continue
                pos_serializer = OrderPositionChangeSerializer(
                    context={'ocm': ocm, 'commit': False, 'event': request.event, **self.get_serializer_context()},
                    partial=True,
                )
                pos_serializer.update(r['position'], r['body'])

            for r in serializer.validated_data.get('split_positions', []):
                if r['position'] in canceled_positions:
                    continue
                ocm.split(r['position'])

            for r in serializer.validated_data.get('create_positions', []):
                pos_serializer = OrderPositionCreateForExistingOrderSerializer(
                    context={'ocm': ocm, 'commit': False, 'event': request.event, **self.get_serializer_context()},
                )
                pos_serializer.create(r)

            canceled_fees = set()
            for r in serializer.validated_data.get('cancel_fees', []):
                ocm.cancel_fee(r['fee'])
                canceled_fees.add(r['fee'])

            for r in serializer.validated_data.get('create_fees', []):
                pos_serializer = OrderFeeCreateForExistingOrderSerializer(
                    context={'ocm': ocm, 'commit': False, 'event': request.event, **self.get_serializer_context()},
                )
                pos_serializer.create(r)

            for r in serializer.validated_data.get('patch_fees', []):
                if r['fee'] in canceled_fees:
                    continue
                pos_serializer = OrderFeeChangeSerializer(
                    context={'ocm': ocm, 'commit': False, 'event': request.event, **self.get_serializer_context()},
                )
                pos_serializer.update(r['fee'], r['body'])

            if serializer.validated_data.get('recalculate_taxes') == 'keep_net':
                ocm.recalculate_taxes(keep='net')
            elif serializer.validated_data.get('recalculate_taxes') == 'keep_gross':
                ocm.recalculate_taxes(keep='gross')

            ocm.commit()
        except OrderError as e:
            raise ValidationError(str(e))

        order.refresh_from_db()
        serializer = OrderSerializer(
            instance=order,
            context=self.get_serializer_context(),
        )
        return Response(serializer.data)


with scopes_disabled():
    class OrderPositionFilter(FilterSet):
        order = django_filters.CharFilter(field_name='order', lookup_expr='code__iexact')
        has_checkin = django_filters.rest_framework.BooleanFilter(method='has_checkin_qs')
        attendee_name = django_filters.CharFilter(method='attendee_name_qs')
        search = django_filters.CharFilter(method='search_qs')

        def search_qs(self, queryset, name, value):
            matching_media = ReusableMedium.objects.filter(identifier=value).values_list('linked_orderposition', flat=True)
            return queryset.filter(
                Q(secret__istartswith=value)
                | Q(attendee_name_cached__icontains=value)
                | Q(addon_to__attendee_name_cached__icontains=value)
                | Q(attendee_email__icontains=value)
                | Q(addon_to__attendee_email__icontains=value)
                | Q(order__code__istartswith=value)
                | Q(order__invoice_address__name_cached__icontains=value)
                | Q(order__invoice_address__company__icontains=value)
                | Q(order__email__icontains=value)
                | Q(pk__in=matching_media)
            )

        def has_checkin_qs(self, queryset, name, value):
            return queryset.alias(ce=Exists(Checkin.objects.filter(position=OuterRef('pk')))).filter(ce=value)

        def attendee_name_qs(self, queryset, name, value):
            return queryset.filter(Q(attendee_name_cached__iexact=value) | Q(addon_to__attendee_name_cached__iexact=value))

        class Meta:
            model = OrderPosition
            fields = {
                'item': ['exact', 'in'],
                'variation': ['exact', 'in'],
                'secret': ['exact'],
                'order__status': ['exact', 'in'],
                'addon_to': ['exact', 'in'],
                'subevent': ['exact', 'in'],
                'pseudonymization_id': ['exact'],
                'voucher__code': ['exact'],
                'voucher': ['exact'],
            }


class OrderPositionViewSet(viewsets.ModelViewSet):
    serializer_class = OrderPositionSerializer
    queryset = OrderPosition.all.none()
    filter_backends = (DjangoFilterBackend, RichOrderingFilter)
    ordering = ('order__datetime', 'positionid')
    ordering_fields = ('order__code', 'order__datetime', 'positionid', 'attendee_name', 'order__status',)
    filterset_class = OrderPositionFilter
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'
    ordering_custom = {
        'attendee_name': {
            '_order': F('display_name').asc(nulls_first=True),
            'display_name': Coalesce('attendee_name_cached', 'addon_to__attendee_name_cached')
        },
        '-attendee_name': {
            '_order': F('display_name').asc(nulls_last=True),
            'display_name': Coalesce('attendee_name_cached', 'addon_to__attendee_name_cached')
        },
    }

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['pdf_data'] = self.request.query_params.get('pdf_data', 'false') == 'true'
        return ctx

    def get_queryset(self):
        if self.request.query_params.get('include_canceled_positions', 'false') == 'true':
            qs = OrderPosition.all
        else:
            qs = OrderPosition.objects

        qs = qs.filter(order__event=self.request.event)
        if self.request.query_params.get('pdf_data', 'false') == 'true':
            prefetch_related_objects([self.request.organizer], 'meta_properties')
            prefetch_related_objects(
                [self.request.event],
                Prefetch('meta_values', queryset=EventMetaValue.objects.select_related('property'), to_attr='meta_values_cached'),
                'questions',
                'item_meta_properties',
            )
            qs = qs.prefetch_related(
                Prefetch('checkins', queryset=Checkin.objects.select_related("device")),
                Prefetch('print_logs', queryset=PrintLog.objects.select_related('device')),
                Prefetch('item', queryset=self.request.event.items.prefetch_related(
                    Prefetch('meta_values', ItemMetaValue.objects.select_related('property'),
                             to_attr='meta_values_cached')
                )),
                'variation',
                'answers', 'answers__options', 'answers__question',
                'item__category',
                'addon_to__answers', 'addon_to__answers__options', 'addon_to__answers__question',
                Prefetch('addons', qs.select_related('item', 'variation')),
                Prefetch('subevent', queryset=self.request.event.subevents.prefetch_related(
                    Prefetch('meta_values', to_attr='meta_values_cached',
                             queryset=SubEventMetaValue.objects.select_related('property'))
                )),
                'linked_media',
                Prefetch('order', self.request.event.orders.select_related('invoice_address').prefetch_related(
                    Prefetch(
                        'positions',
                        qs.prefetch_related(
                            Prefetch('checkins', queryset=Checkin.objects.select_related('device')),
                            Prefetch('item', queryset=self.request.event.items.prefetch_related(
                                Prefetch('meta_values', ItemMetaValue.objects.select_related('property'),
                                         to_attr='meta_values_cached')
                            )),
                            Prefetch('variation', queryset=self.request.event.items.prefetch_related(
                                Prefetch('meta_values', ItemVariationMetaValue.objects.select_related('property'),
                                         to_attr='meta_values_cached')
                            )),
                            'answers', 'answers__options', 'answers__question',
                            'item__category',
                            Prefetch('subevent', queryset=self.request.event.subevents.prefetch_related(
                                Prefetch('meta_values', to_attr='meta_values_cached',
                                         queryset=SubEventMetaValue.objects.select_related('property'))
                            )),
                            Prefetch('addons', qs.select_related('item', 'variation', 'seat'))
                        ).select_related('addon_to', 'seat', 'addon_to__seat')
                    )
                ))
            ).select_related(
                'addon_to', 'seat', 'addon_to__seat'
            )
        else:
            qs = qs.prefetch_related(
                Prefetch('checkins', queryset=Checkin.objects.select_related("device")),
                Prefetch('print_logs', queryset=PrintLog.objects.select_related('device')),
                'answers', 'answers__options', 'answers__question',
            ).select_related(
                'item', 'order', 'order__event', 'order__event__organizer', 'seat'
            )
        return qs

    def _get_output_provider(self, identifier):
        responses = register_ticket_outputs.send(self.request.event)
        for receiver, response in responses:
            prov = response(self.request.event)
            if prov.identifier == identifier:
                return prov
        raise NotFound('Unknown output provider.')

    @action(detail=True, methods=['POST'], url_name='price_calc')
    def price_calc(self, request, *args, **kwargs):
        """
        This calculates the price assuming a change of product or subevent. This endpoint
        is deliberately not documented and considered a private API, only to be used by
        pretix' web interface.

        Sample input:

        {
            "item": 2,
            "variation": null,
            "subevent": 3,
            "tax_rule": 4,
        }

        Sample output:

        {
            "gross": "2.34",
            "gross_formatted": "2,34",
            "net": "2.34",
            "tax": "0.00",
            "rate": "0.00",
            "name": "VAT"
        }
        """
        serializer = PriceCalcSerializer(data=request.data, event=request.event)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        pos = self.get_object()

        try:
            ia = pos.order.invoice_address
        except InvoiceAddress.DoesNotExist:
            ia = InvoiceAddress()

        kwargs = {
            'item': pos.item,
            'variation': pos.variation,
            'voucher': pos.voucher,
            'subevent': pos.subevent,
            'addon_to': pos.addon_to,
            'invoice_address': ia,
        }

        if data.get('item'):
            item = data.get('item')
            kwargs['item'] = item

            if item.has_variations:
                variation = data.get('variation') or pos.variation
                if not variation:
                    raise ValidationError('No variation given')
                if variation.item != item:
                    raise ValidationError('Variation does not belong to item')
                kwargs['variation'] = variation
            else:
                variation = None
                kwargs['variation'] = None

            if pos.voucher and not pos.voucher.applies_to(item, variation):
                kwargs['voucher'] = None

        if data.get('subevent'):
            kwargs['subevent'] = data.get('subevent')

        if data.get('tax_rule'):
            kwargs['tax_rule'] = data.get('tax_rule')

        price = get_price(**kwargs)
        tr = kwargs.get('tax_rule', kwargs.get('item').tax_rule)
        with language(data.get('locale') or self.request.event.settings.locale, self.request.event.settings.region):
            gross_formatted = formats.localize_input(round_decimal(price.gross, self.request.event.currency))
            return Response({
                'gross': price.gross,
                'gross_formatted': gross_formatted,
                'net': price.net,
                'rate': price.rate,
                'name': str(price.name),
                'tax': price.tax,
                'tax_rule': tr.pk if tr else None,
            })

    @action(detail=True, url_name='answer', url_path=r'answer/(?P<question>\d+)')
    def answer(self, request, **kwargs):
        pos = self.get_object()
        answer = get_object_or_404(
            QuestionAnswer,
            orderposition=self.get_object(),
            question_id=kwargs.get('question')
        )
        if not answer.file:
            raise NotFound()

        ftype, ignored = mimetypes.guess_type(answer.file.name)
        resp = FileResponse(answer.file, content_type=ftype or 'application/binary')
        resp['Content-Disposition'] = 'attachment; filename="{}-{}-{}-{}"'.format(
            self.request.event.slug.upper(),
            pos.order.code,
            pos.positionid,
            os.path.basename(answer.file.name).split('.', 1)[1]
        )
        return resp

    @action(detail=True, url_name="printlog", url_path="printlog", methods=["POST"])
    def printlog(self, request, **kwargs):
        pos = self.get_object()
        serializer = PrintLogSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            serializer.save(
                position=pos,
                device=request.auth if isinstance(request.auth, Device) else None,
                user=request.user if request.user.is_authenticated else None,
                api_token=request.auth if isinstance(request.auth, TeamAPIToken) else None,
                oauth_application=request.auth.application if isinstance(request.auth, OAuthAccessToken) else None,
            )

            pos.order.log_action(
                "pretix.event.order.print",
                data={
                    "position": pos.pk,
                    "positionid": pos.positionid,
                    **serializer.validated_data,
                },
                auth=request.auth,
                user=request.user,
            )

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, url_name='pdf_image', url_path=r'pdf_image/(?P<key>[^/]+)')
    def pdf_image(self, request, key, **kwargs):
        pos = self.get_object()

        image_vars = get_images(request.event)
        if key not in image_vars:
            raise NotFound('Unknown key')

        image_file = image_vars[key]['evaluate'](pos, pos.order, pos.subevent or self.request.event)
        if image_file is None:
            raise NotFound('No image available')

        if getattr(image_file, 'name', ''):
            ftype, ignored = mimetypes.guess_type(image_file.name)
            extension = os.path.basename(image_file.name).split('.')[-1]
        else:
            img = Image.open(image_file, formats=settings.PILLOW_FORMATS_QUESTIONS_IMAGE)
            ftype = Image.MIME[img.format]
            extensions = {
                'GIF': 'gif', 'TIFF': 'tif', 'BMP': 'bmp', 'JPEG': 'jpg', 'PNG': 'png'
            }
            extension = extensions.get(img.format, 'bin')
            if hasattr(image_file, 'seek'):
                image_file.seek(0)

        resp = FileResponse(image_file, content_type=ftype or 'application/binary')
        resp['Content-Disposition'] = 'attachment; filename="{}-{}-{}-{}.{}"'.format(
            self.request.event.slug.upper(),
            pos.order.code,
            pos.positionid,
            key,
            extension,
        )
        return resp

    @action(detail=True, url_name='download', url_path='download/(?P<output>[^/]+)')
    def download(self, request, output, **kwargs):
        provider = self._get_output_provider(output)
        pos = self.get_object()

        if pos.order.status in (Order.STATUS_CANCELED, Order.STATUS_EXPIRED):
            raise PermissionDenied("Downloads are not available for canceled or expired orders.")

        if pos.order.status == Order.STATUS_PENDING and not (pos.order.valid_if_pending or request.event.settings.ticket_download_pending):
            raise PermissionDenied("Downloads are not available for pending orders.")
        if not pos.generate_ticket:
            raise PermissionDenied("Downloads are not enabled for this product.")

        ct = CachedTicket.objects.filter(
            order_position=pos, provider=provider.identifier, file__isnull=False
        ).last()
        if not ct or not ct.file:
            generate.apply_async(args=('orderposition', pos.pk, provider.identifier))
            raise RetryException()
        else:
            if ct.type == 'text/uri-list':
                resp = HttpResponse(ct.file.file.read(), content_type='text/uri-list')
                return resp
            else:
                resp = FileResponse(ct.file.file, content_type=ct.type)
                resp['Content-Disposition'] = 'attachment; filename="{}-{}-{}-{}{}"'.format(
                    self.request.event.slug.upper(), pos.order.code, pos.positionid,
                    provider.identifier, ct.extension
                )
                return resp

    @action(detail=True, methods=['POST'])
    def regenerate_secrets(self, request, **kwargs):
        instance = self.get_object()
        try:
            ocm = OrderChangeManager(
                instance.order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=self.request.auth,
                notify=False,
                reissue_invoice=False,
            )
            ocm.regenerate_secret(instance)
            ocm.commit()
        except OrderError as e:
            raise ValidationError(str(e))
        except Quota.QuotaExceededException as e:
            raise ValidationError(str(e))
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def add_block(self, request, **kwargs):
        serializer = BlockNameSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        instance = self.get_object()
        try:
            ocm = OrderChangeManager(
                instance.order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=self.request.auth,
                notify=False,
                reissue_invoice=False,
            )
            ocm.add_block(instance, serializer.validated_data['name'])
            ocm.commit()
        except OrderError as e:
            raise ValidationError(str(e))
        except Quota.QuotaExceededException as e:
            raise ValidationError(str(e))
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def remove_block(self, request, **kwargs):
        serializer = BlockNameSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        instance = self.get_object()
        try:
            ocm = OrderChangeManager(
                instance.order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=self.request.auth,
                notify=False,
                reissue_invoice=False,
            )
            ocm.remove_block(instance, serializer.validated_data['name'])
            ocm.commit()
        except OrderError as e:
            raise ValidationError(str(e))
        except Quota.QuotaExceededException as e:
            raise ValidationError(str(e))
        return self.retrieve(request, [], **kwargs)

    def perform_destroy(self, instance):
        try:
            ocm = OrderChangeManager(
                instance.order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=self.request.auth,
                notify=False
            )
            ocm.cancel(instance)
            ocm.commit()
        except OrderError as e:
            raise ValidationError(str(e))
        except Quota.QuotaExceededException as e:
            raise ValidationError(str(e))

    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            serializer = OrderPositionCreateForExistingOrderSerializer(
                data=request.data,
                context=self.get_serializer_context(),
            )
            serializer.is_valid(raise_exception=True)
            order = serializer.validated_data['order']
            ocm = OrderChangeManager(
                order=order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=request.auth,
                notify=False,
                reissue_invoice=False,
            )
            serializer.context['ocm'] = ocm
            serializer.save()

            # Fields that can be easily patched after the position was added
            old_data = OrderPositionInfoPatchSerializer(instance=serializer.instance, context=self.get_serializer_context()).data
            serializer = OrderPositionInfoPatchSerializer(
                instance=serializer.instance,
                context=self.get_serializer_context(),
                partial=True,
                data=request.data
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            new_data = serializer.data

            if old_data != new_data:
                log_data = self.request.data
                if 'answers' in log_data:
                    for a in new_data['answers']:
                        log_data[f'question_{a["question"]}'] = a["answer"]
                    log_data.pop('answers', None)
                serializer.instance.order.log_action(
                    'pretix.event.order.modified',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'data': [
                            dict(
                                position=serializer.instance.pk,
                                **log_data
                            )
                        ]
                    }
                )
                tickets.invalidate_cache.apply_async(
                    kwargs={'event': serializer.instance.order.event.pk, 'order': serializer.instance.order.pk})
                order_modified.send(sender=serializer.instance.order.event, order=serializer.instance.order)
        return Response(
            OrderPositionSerializer(serializer.instance, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.get('partial', False)
        if not partial:
            return Response(
                {"detail": "Method \"PUT\" not allowed."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        with transaction.atomic():
            instance = self.get_object()
            ocm = OrderChangeManager(
                order=instance.order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=request.auth,
                notify=False,
                reissue_invoice=False,
            )

            # Field that need to go through OrderChangeManager
            serializer = OrderPositionChangeSerializer(
                instance=instance,
                context={'ocm': ocm, **self.get_serializer_context()},
                partial=True,
                data=request.data
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

            # Fields that can be easily patched
            old_data = OrderPositionInfoPatchSerializer(instance=instance, context=self.get_serializer_context()).data
            serializer = OrderPositionInfoPatchSerializer(
                instance=instance,
                context=self.get_serializer_context(),
                partial=True,
                data=request.data
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            new_data = serializer.data

            if old_data != new_data:
                log_data = self.request.data
                if 'answers' in log_data:
                    for a in new_data['answers']:
                        log_data[f'question_{a["question"]}'] = a["answer"]
                    log_data.pop('answers', None)
                serializer.instance.order.log_action(
                    'pretix.event.order.modified',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'data': [
                            dict(
                                position=serializer.instance.pk,
                                **log_data
                            )
                        ]
                    }
                )
                tickets.invalidate_cache.apply_async(kwargs={'event': serializer.instance.order.event.pk, 'order': serializer.instance.order.pk})
                order_modified.send(sender=serializer.instance.order.event, order=serializer.instance.order)

        return Response(self.get_serializer_class()(instance=serializer.instance, context=self.get_serializer_context()).data)


class PaymentViewSet(CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderPaymentSerializer
    queryset = OrderPayment.objects.none()
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'
    lookup_field = 'local_id'

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['order'] = get_object_or_404(Order, code=self.kwargs['order'], event=self.request.event)
        ctx['event'] = self.request.event
        return ctx

    def get_queryset(self):
        order = get_object_or_404(Order, code=self.kwargs['order'], event=self.request.event)
        return order.payments.all()

    def create(self, request, *args, **kwargs):
        send_mail = request.data.get('send_email', True) if request.data else True
        serializer = OrderPaymentCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            mark_confirmed = False
            if serializer.validated_data['state'] == OrderPayment.PAYMENT_STATE_CONFIRMED:
                serializer.validated_data['state'] = OrderPayment.PAYMENT_STATE_PENDING
                mark_confirmed = True
            self.perform_create(serializer)
            r = serializer.instance
            if mark_confirmed:
                try:
                    r.confirm(
                        user=self.request.user if self.request.user.is_authenticated else None,
                        auth=self.request.auth,
                        count_waitinglist=False,
                        force=request.data.get('force', False),
                        send_mail=send_mail,
                    )
                except Quota.QuotaExceededException:
                    pass
                except SendMailException:
                    pass

            serializer = OrderPaymentSerializer(r, context=serializer.context)

            r.order.log_action(
                'pretix.event.order.payment.started', {
                    'local_id': r.local_id,
                    'provider': r.provider,
                },
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth
            )

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save()

    @action(detail=True, methods=['POST'])
    def confirm(self, request, **kwargs):
        payment = self.get_object()
        force = request.data.get('force', False)
        send_mail = request.data.get('send_email', True) if request.data else True

        if payment.state not in (OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED):
            return Response({'detail': 'Invalid state of payment'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payment.confirm(user=self.request.user if self.request.user.is_authenticated else None,
                            auth=self.request.auth,
                            count_waitinglist=False,
                            send_mail=send_mail,
                            force=force)
        except Quota.QuotaExceededException as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except PaymentException as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SendMailException:
            pass
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def refund(self, request, **kwargs):
        payment = self.get_object()
        amount = serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(
            request.data.get('amount', str(payment.amount))
        )
        if 'mark_refunded' in request.data:
            mark_refunded = request.data.get('mark_refunded', False)
        else:
            mark_refunded = request.data.get('mark_canceled', False)

        if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
            return Response({'detail': 'Invalid state of payment.'}, status=status.HTTP_400_BAD_REQUEST)

        full_refund_possible = payment.payment_provider.payment_refund_supported(payment)
        partial_refund_possible = payment.payment_provider.payment_partial_refund_supported(payment)
        available_amount = payment.amount - payment.refunded_amount

        if amount <= 0:
            return Response({'amount': ['Invalid refund amount.']}, status=status.HTTP_400_BAD_REQUEST)
        if amount > available_amount:
            return Response(
                {'amount': ['Invalid refund amount, only {} are available to refund.'.format(available_amount)]},
                status=status.HTTP_400_BAD_REQUEST)
        if amount != payment.amount and not partial_refund_possible:
            return Response({'amount': ['Partial refund not available for this payment method.']},
                            status=status.HTTP_400_BAD_REQUEST)
        if amount == payment.amount and not full_refund_possible:
            return Response({'amount': ['Full refund not available for this payment method.']},
                            status=status.HTTP_400_BAD_REQUEST)
        r = payment.order.refunds.create(
            payment=payment,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            state=OrderRefund.REFUND_STATE_CREATED,
            amount=amount,
            provider=payment.provider,
            info='{}',
        )
        payment.order.log_action('pretix.event.order.refund.created', {
            'local_id': r.local_id,
            'provider': r.provider,
        }, user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)

        try:
            r.payment_provider.execute_refund(r)
        except PaymentException as e:
            r.state = OrderRefund.REFUND_STATE_FAILED
            r.save()
            payment.order.log_action('pretix.event.order.refund.failed', {
                'local_id': r.local_id,
                'provider': r.provider,
                'error': str(e)
            })
            return Response({'detail': 'External error: {}'.format(str(e))},
                            status=status.HTTP_400_BAD_REQUEST)
        else:
            if payment.order.pending_sum > 0:
                if mark_refunded:
                    mark_order_refunded(payment.order,
                                        user=self.request.user if self.request.user.is_authenticated else None,
                                        auth=self.request.auth)
                else:
                    payment.order.status = Order.STATUS_PENDING
                    payment.order.set_expires(
                        now(),
                        payment.order.event.subevents.filter(
                            id__in=payment.order.positions.values_list('subevent_id', flat=True))
                    )
                    payment.order.save(update_fields=['status', 'expires'])
            return Response(OrderRefundSerializer(r).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['POST'])
    def cancel(self, request, **kwargs):
        payment = self.get_object()

        if payment.state not in (OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED):
            return Response({'detail': 'Invalid state of payment'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                payment.payment_provider.cancel_payment(payment)
                payment.order.log_action('pretix.event.order.payment.canceled', {
                    'local_id': payment.local_id,
                    'provider': payment.provider,
                }, user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)
        except PaymentException as e:
            return Response({'detail': 'External error: {}'.format(str(e))},
                            status=status.HTTP_400_BAD_REQUEST)
        return self.retrieve(request, [], **kwargs)


class RefundViewSet(CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderRefundSerializer
    queryset = OrderRefund.objects.none()
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'
    lookup_field = 'local_id'

    def get_queryset(self):
        order = get_object_or_404(Order, code=self.kwargs['order'], event=self.request.event)
        return order.refunds.all()

    @action(detail=True, methods=['POST'])
    def cancel(self, request, **kwargs):
        refund = self.get_object()

        if refund.state not in (OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT,
                                OrderRefund.REFUND_STATE_EXTERNAL):
            return Response({'detail': 'Invalid state of refund'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            refund.state = OrderRefund.REFUND_STATE_CANCELED
            refund.save()
            refund.order.log_action('pretix.event.order.refund.canceled', {
                'local_id': refund.local_id,
                'provider': refund.provider,
            }, user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def process(self, request, **kwargs):
        refund = self.get_object()

        if refund.state != OrderRefund.REFUND_STATE_EXTERNAL:
            return Response({'detail': 'Invalid state of refund'}, status=status.HTTP_400_BAD_REQUEST)

        refund.done(user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)
        if 'mark_refunded' in request.data:
            mark_refunded = request.data.get('mark_refunded', False)
        else:
            mark_refunded = request.data.get('mark_canceled', False)
        if mark_refunded:
            mark_order_refunded(refund.order, user=self.request.user if self.request.user.is_authenticated else None,
                                auth=self.request.auth)
        elif not (refund.order.status == Order.STATUS_PAID and refund.order.pending_sum <= 0):
            refund.order.status = Order.STATUS_PENDING
            refund.order.set_expires(
                now(),
                refund.order.event.subevents.filter(
                    id__in=refund.order.positions.values_list('subevent_id', flat=True))
            )
            refund.order.save(update_fields=['status', 'expires'])
        return self.retrieve(request, [], **kwargs)

    @action(detail=True, methods=['POST'])
    def done(self, request, **kwargs):
        refund = self.get_object()

        if refund.state not in (OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT):
            return Response({'detail': 'Invalid state of refund'}, status=status.HTTP_400_BAD_REQUEST)

        refund.done(user=self.request.user if self.request.user.is_authenticated else None, auth=self.request.auth)
        return self.retrieve(request, [], **kwargs)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['order'] = get_object_or_404(Order, code=self.kwargs['order'], event=self.request.event)
        return ctx

    def create(self, request, *args, **kwargs):
        if 'mark_refunded' in request.data:
            mark_refunded = request.data.pop('mark_refunded', False)
        else:
            mark_refunded = request.data.pop('mark_canceled', False)
        mark_pending = request.data.pop('mark_pending', False)
        serializer = OrderRefundCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            self.perform_create(serializer)
            r = serializer.instance
            serializer = OrderRefundSerializer(r, context=serializer.context)

            r.order.log_action(
                'pretix.event.order.refund.created', {
                    'local_id': r.local_id,
                    'provider': r.provider,
                },
                user=request.user if request.user.is_authenticated else None,
                auth=request.auth
            )

            if r.state in (OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_CANCELED, OrderRefund.REFUND_STATE_FAILED):
                r.order.log_action(
                    f'pretix.event.order.refund.{r.state}', {
                        'local_id': r.local_id,
                        'provider': r.provider,
                    },
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth
                )

            if mark_refunded:
                try:
                    mark_order_refunded(
                        r.order,
                        user=request.user if request.user.is_authenticated else None,
                        auth=(request.auth if request.auth else None),
                    )
                except OrderError as e:
                    raise ValidationError(str(e))
            elif mark_pending:
                if r.order.status == Order.STATUS_PAID and r.order.pending_sum > 0:
                    r.order.status = Order.STATUS_PENDING
                    r.order.set_expires(
                        now(),
                        r.order.event.subevents.filter(
                            id__in=r.order.positions.values_list('subevent_id', flat=True))
                    )
                    r.order.save(update_fields=['status', 'expires'])

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        serializer.save()


with scopes_disabled():
    class InvoiceFilter(FilterSet):
        refers = django_filters.CharFilter(method='refers_qs')
        number = MultipleCharFilter(field_name='nr', lookup_expr='iexact')
        order = MultipleCharFilter(field_name='order', lookup_expr='code__iexact')

        def refers_qs(self, queryset, name, value):
            return queryset.annotate(
                refers_nr=Concat('refers__prefix', 'refers__invoice_no')
            ).filter(refers_nr__iexact=value)

        class Meta:
            model = Invoice
            fields = ['order', 'number', 'is_cancellation', 'refers', 'locale']


class RetryException(APIException):
    status_code = 409
    default_detail = 'The requested resource is not ready, please retry later.'
    default_code = 'retry_later'


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InvoiceSerializer
    queryset = Invoice.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('nr',)
    ordering_fields = ('nr', 'date')
    filterset_class = InvoiceFilter
    permission = 'can_view_orders'
    lookup_url_kwarg = 'number'
    lookup_field = 'nr'
    write_permission = 'can_change_orders'

    def get_queryset(self):
        perm = "can_view_orders" if self.request.method in SAFE_METHODS else "can_change_orders"
        if getattr(self.request, 'event', None):
            qs = self.request.event.invoices
        elif isinstance(self.request.auth, (TeamAPIToken, Device)):
            qs = Invoice.objects.filter(
                event__organizer=self.request.organizer,
                event__in=self.request.auth.get_events_with_permission(perm, request=self.request)
            )
        elif self.request.user.is_authenticated:
            qs = Invoice.objects.filter(
                event__organizer=self.request.organizer,
                event__in=self.request.user.get_events_with_permission(perm, request=self.request)
            )
        return qs.prefetch_related('lines').select_related('order', 'refers').annotate(
            nr=Concat('prefix', 'invoice_no')
        )

    @action(detail=True)
    def download(self, request, **kwargs):
        invoice = self.get_object()

        if not invoice.file:
            invoice_pdf(invoice.pk)
            invoice.refresh_from_db()

        if invoice.shredded:
            raise PermissionDenied('The invoice file is no longer stored on the server.')

        if not invoice.file:
            raise RetryException()

        resp = FileResponse(invoice.file.file, content_type='application/pdf')
        resp['Content-Disposition'] = 'attachment; filename="{}.pdf"'.format(invoice.number)
        return resp

    @action(detail=True, methods=['POST'])
    def regenerate(self, request, **kwargs):
        inv = self.get_object()
        if inv.canceled:
            raise ValidationError('The invoice has already been canceled.')
        if not inv.event.settings.invoice_regenerate_allowed:
            raise PermissionDenied('Invoices may not be changed after they are created.')
        elif inv.shredded:
            raise PermissionDenied('The invoice file is no longer stored on the server.')
        elif inv.sent_to_organizer:
            raise PermissionDenied('The invoice file has already been exported.')
        elif now().astimezone(inv.event.timezone).date() - inv.date > datetime.timedelta(days=1):
            raise PermissionDenied('The invoice file is too old to be regenerated.')
        else:
            inv = regenerate_invoice(inv)
            inv.order.log_action(
                'pretix.event.order.invoice.regenerated',
                data={
                    'invoice': inv.pk
                },
                user=self.request.user,
                auth=self.request.auth,
            )
            return Response(status=204)

    @action(detail=True, methods=['POST'])
    @transaction.atomic()
    def reissue(self, request, **kwargs):
        inv = self.get_object()
        if inv.canceled:
            raise ValidationError('The invoice has already been canceled.')
        elif inv.shredded:
            raise PermissionDenied('The invoice file is no longer stored on the server.')
        else:
            order = Order.objects.select_for_update(of=OF_SELF).get(pk=inv.order_id)
            c = generate_cancellation(inv)
            if inv.order.status != Order.STATUS_CANCELED:
                inv = generate_invoice(order)
            else:
                inv = c
            inv.order.log_action(
                'pretix.event.order.invoice.reissued',
                data={
                    'invoice': inv.pk
                },
                user=self.request.user,
                auth=self.request.auth,
            )
            return Response(status=204)


with scopes_disabled():
    class RevokedSecretFilter(FilterSet):
        created_since = django_filters.IsoDateTimeFilter(field_name='created', lookup_expr='gte')

        class Meta:
            model = RevokedTicketSecret
            fields = []


class RevokedSecretViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RevokedTicketSecretSerializer
    queryset = RevokedTicketSecret.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('-created',)
    ordering_fields = ('created', 'secret')
    filterset_class = RevokedSecretFilter
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'

    def get_queryset(self):
        return RevokedTicketSecret.objects.filter(event=self.request.event)


with scopes_disabled():
    class BlockedSecretFilter(FilterSet):
        updated_since = django_filters.IsoDateTimeFilter(field_name='updated', lookup_expr='gte')

        class Meta:
            model = BlockedTicketSecret
            fields = ['blocked']


class BlockedSecretViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BlockedTicketSecretSerializer
    queryset = BlockedTicketSecret.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('-updated', '-pk')
    filterset_class = BlockedSecretFilter
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'

    def get_queryset(self):
        return BlockedTicketSecret.objects.filter(event=self.request.event)


with scopes_disabled():
    class TransactionFilter(FilterSet):
        order = django_filters.CharFilter(field_name='order', lookup_expr='code__iexact')
        event = django_filters.CharFilter(field_name='order__event', lookup_expr='slug__iexact')
        datetime_since = django_filters.IsoDateTimeFilter(field_name='datetime', lookup_expr='gte')
        datetime_before = django_filters.IsoDateTimeFilter(field_name='datetime', lookup_expr='lt')
        created_since = django_filters.IsoDateTimeFilter(field_name='created', lookup_expr='gte')
        created_before = django_filters.IsoDateTimeFilter(field_name='created', lookup_expr='lt')

        class Meta:
            model = Transaction
            fields = {
                'item': ['exact', 'in'],
                'variation': ['exact', 'in'],
                'subevent': ['exact', 'in'],
                'tax_rule': ['exact', 'in'],
                'tax_code': ['exact', 'in'],
                'tax_rate': ['exact', 'in'],
                'fee_type': ['exact', 'in'],
            }


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TransactionSerializer
    queryset = Transaction.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering = ('datetime', 'pk')
    ordering_fields = ('datetime', 'created', 'id',)
    filterset_class = TransactionFilter
    permission = 'can_view_orders'

    def get_queryset(self):
        return Transaction.objects.filter(order__event=self.request.event).select_related("order")


class OrganizerTransactionViewSet(TransactionViewSet):
    serializer_class = OrganizerTransactionSerializer
    permission = None

    def get_queryset(self):
        qs = Transaction.objects.filter(
            order__event__organizer=self.request.organizer
        ).select_related("order", "order__event")

        if isinstance(self.request.auth, (TeamAPIToken, Device)):
            qs = qs.filter(
                order__event__in=self.request.auth.get_events_with_permission("can_view_orders"),
            )
        elif self.request.user.is_authenticated:
            qs = qs.filter(
                order__event__in=self.request.user.get_events_with_permission("can_view_orders", request=self.request)
            )
        else:
            raise PermissionDenied("Unknown authentication scheme")

        return qs
