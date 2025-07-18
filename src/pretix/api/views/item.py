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

# This file is based on an earlier version of pretix which was released under the Apache License 2.0. The full text of
# the Apache License 2.0 can be obtained at <http://www.apache.org/licenses/LICENSE-2.0>.
#
# This file may have since been changed and any changes are released under the terms of AGPLv3 as described above. A
# full history of changes and contributors is available at <https://github.com/pretix/pretix>.
#
# This file contains Apache-licensed contributions copyrighted by: Ture Gjørup
#
# Unless required by applicable law or agreed to in writing, software distributed under the Apache License 2.0 is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under the License.

import django_filters
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.functional import cached_property
from django_filters.rest_framework import DjangoFilterBackend, FilterSet
from django_scopes import scopes_disabled
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from pretix.api.pagination import TotalOrderingFilter
from pretix.api.serializers.item import (
    ItemAddOnSerializer, ItemBundleSerializer, ItemCategorySerializer,
    ItemSerializer, ItemVariationSerializer, QuestionOptionSerializer,
    QuestionSerializer, QuotaSerializer,
)
from pretix.api.views import ConditionalListView
from pretix.base.models import (
    CartPosition, Item, ItemAddOn, ItemBundle, ItemCategory, ItemVariation,
    Question, QuestionOption, Quota,
)
from pretix.base.services.quotas import QuotaAvailability
from pretix.helpers.dicts import merge_dicts
from pretix.helpers.i18n import i18ncomp

with scopes_disabled():
    class ItemFilter(FilterSet):
        tax_rate = django_filters.CharFilter(method='tax_rate_qs')
        search = django_filters.CharFilter(method='search_qs')

        def search_qs(self, queryset, name, value):
            return queryset.filter(
                Q(internal_name__icontains=value) | Q(name__icontains=i18ncomp(value))
            )

        def tax_rate_qs(self, queryset, name, value):
            if value in ("0", "None", "0.00"):
                return queryset.filter(Q(tax_rule__isnull=True) | Q(tax_rule__rate=0))
            else:
                return queryset.filter(tax_rule__rate=value)

        class Meta:
            model = Item
            fields = ['active', 'category', 'admission', 'tax_rate', 'free_price']

    class ItemVariationFilter(FilterSet):
        search = django_filters.CharFilter(method='search_qs')

        def search_qs(self, queryset, name, value):
            return queryset.filter(
                Q(value__icontains=i18ncomp(value))
            )

        class Meta:
            model = ItemVariation
            fields = ['active']


class ItemViewSet(ConditionalListView, viewsets.ModelViewSet):
    serializer_class = ItemSerializer
    queryset = Item.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    ordering_fields = ('id', 'position')
    ordering = ('position', 'id')
    filterset_class = ItemFilter
    permission = None
    write_permission = 'can_change_items'

    def get_queryset(self):
        return self.request.event.items.select_related('tax_rule').prefetch_related(
            'variations', 'addons', 'bundles', 'meta_values', 'meta_values__property',
            'variations__meta_values', 'variations__meta_values__property',
            'require_membership_types', 'variations__require_membership_types',
            'limit_sales_channels', 'variations__limit_sales_channels',
        ).all()

    def perform_create(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.item.added',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        return ctx

    def perform_update(self, serializer):
        original_data = self.get_serializer(instance=serializer.instance).data

        serializer.save(event=self.request.event)

        if serializer.data == original_data:
            # Performance optimization: If nothing was changed, we do not need to save or log anything.
            # This costs us a few cycles on save, but avoids thousands of lines in our log.
            return
        serializer.instance.log_action(
            'pretix.event.item.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def perform_destroy(self, instance):
        if not instance.allow_delete():
            raise PermissionDenied('This item cannot be deleted because it has already been ordered '
                                   'by a user or currently is in a users\'s cart. Please set the item as '
                                   '"inactive" instead.')

        instance.log_action(
            'pretix.event.item.deleted',
            user=self.request.user,
            auth=self.request.auth,
        )
        CartPosition.objects.filter(addon_to__item=instance).delete()
        instance.cartposition_set.all().delete()
        super().perform_destroy(instance)


class ItemVariationViewSet(viewsets.ModelViewSet):
    serializer_class = ItemVariationSerializer
    queryset = ItemVariation.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter,)
    filterset_class = ItemVariationFilter
    ordering_fields = ('id', 'position')
    ordering = ('id',)
    permission = None
    write_permission = 'can_change_items'

    @cached_property
    def item(self):
        return get_object_or_404(Item, pk=self.kwargs['item'], event=self.request.event)

    def get_queryset(self):
        return self.item.variations.all().prefetch_related(
            'meta_values',
            'meta_values__property',
            'require_membership_types',
            'limit_sales_channels',
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['item'] = self.item
        ctx['event'] = self.request.event
        return ctx

    def perform_create(self, serializer):
        item = self.item
        if not item.has_variations:
            raise PermissionDenied('This variation cannot be created because the item does not have variations. '
                                   'Changing a product without variations to a product with variations is not allowed.')
        serializer.save(item=item)
        item.log_action(
            'pretix.event.item.variation.added',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk},
                             {'value': serializer.instance.value})
        )

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.item.log_action(
            'pretix.event.item.variation.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk},
                             {'value': serializer.instance.value})
        )

    def perform_destroy(self, instance):
        if not instance.allow_delete():
            raise PermissionDenied('This variation cannot be deleted because it has already been ordered '
                                   'by a user or currently is in a users\'s cart. Please set the variation as '
                                   '\'inactive\' instead.')
        if instance.is_only_variation():
            raise PermissionDenied('This variation cannot be deleted because it is the only variation. Changing a '
                                   'product with variations to a product without variations is not allowed.')
        super().perform_destroy(instance)
        instance.item.log_action(
            'pretix.event.item.variation.deleted',
            user=self.request.user,
            auth=self.request.auth,
            data={
                'value': instance.value,
                'id': self.kwargs['pk']
            }
        )


class ItemBundleViewSet(viewsets.ModelViewSet):
    serializer_class = ItemBundleSerializer
    queryset = ItemBundle.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter,)
    ordering_fields = ('id',)
    ordering = ('id',)
    permission = None
    write_permission = 'can_change_items'

    @cached_property
    def item(self):
        return get_object_or_404(Item, pk=self.kwargs['item'], event=self.request.event)

    def get_queryset(self):
        return self.item.bundles.all()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['item'] = self.item
        return ctx

    def perform_create(self, serializer):
        item = get_object_or_404(Item, pk=self.kwargs['item'], event=self.request.event)
        serializer.save(base_item=item)
        item.log_action(
            'pretix.event.item.bundles.added',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'id': serializer.instance.pk})
        )

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.base_item.log_action(
            'pretix.event.item.bundles.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'id': serializer.instance.pk})
        )

    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        instance.base_item.log_action(
            'pretix.event.item.bundles.removed',
            user=self.request.user,
            auth=self.request.auth,
            data={'bundled_item': instance.bundled_item.pk, 'bundled_variation': instance.bundled_variation.pk if instance.bundled_variation else None,
                  'count': instance.count, 'designated_price': instance.designated_price}
        )


class ItemAddOnViewSet(viewsets.ModelViewSet):
    serializer_class = ItemAddOnSerializer
    queryset = ItemAddOn.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter,)
    ordering_fields = ('id', 'position')
    ordering = ('id',)
    permission = None
    write_permission = 'can_change_items'

    @cached_property
    def item(self):
        return get_object_or_404(Item, pk=self.kwargs['item'], event=self.request.event)

    def get_queryset(self):
        return self.item.addons.all()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['item'] = self.item
        return ctx

    def perform_create(self, serializer):
        item = self.item
        category = get_object_or_404(ItemCategory, pk=self.request.data['addon_category'])
        serializer.save(base_item=item, addon_category=category)
        item.log_action(
            'pretix.event.item.addons.added',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk})
        )

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.base_item.log_action(
            'pretix.event.item.addons.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk})
        )

    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        instance.base_item.log_action(
            'pretix.event.item.addons.removed',
            user=self.request.user,
            auth=self.request.auth,
            data={'category': instance.addon_category.pk}
        )


class ItemCategoryFilter(FilterSet):
    class Meta:
        model = ItemCategory
        fields = ['is_addon']


class ItemCategoryViewSet(ConditionalListView, viewsets.ModelViewSet):
    serializer_class = ItemCategorySerializer
    queryset = ItemCategory.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    filterset_class = ItemCategoryFilter
    ordering_fields = ('id', 'position')
    ordering = ('position', 'id')
    permission = None
    write_permission = 'can_change_items'

    def get_queryset(self):
        return self.request.event.categories.all()

    def perform_create(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.category.added',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        return ctx

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.category.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def perform_destroy(self, instance):
        for item in instance.items.all():
            item.category = None
            item.save()
        instance.log_action(
            'pretix.event.category.deleted',
            user=self.request.user,
            auth=self.request.auth,
        )
        super().perform_destroy(instance)


with scopes_disabled():
    class QuestionFilter(FilterSet):
        class Meta:
            model = Question
            fields = ['ask_during_checkin', 'required', 'identifier']


class QuestionViewSet(ConditionalListView, viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    queryset = Question.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter)
    filterset_class = QuestionFilter
    ordering_fields = ('id', 'position')
    ordering = ('position', 'id')
    permission = None
    write_permission = 'can_change_items'

    def get_queryset(self):
        return self.request.event.questions.prefetch_related('options').all()

    def perform_create(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.question.added',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        return ctx

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.question.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )

    def perform_destroy(self, instance):
        instance.log_action(
            'pretix.event.question.deleted',
            user=self.request.user,
            auth=self.request.auth,
        )
        super().perform_destroy(instance)


class QuestionOptionViewSet(viewsets.ModelViewSet):
    serializer_class = QuestionOptionSerializer
    queryset = QuestionOption.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter,)
    ordering_fields = ('id', 'position')
    ordering = ('position',)
    permission = None
    write_permission = 'can_change_items'

    def get_queryset(self):
        q = get_object_or_404(Question, pk=self.kwargs['question'], event=self.request.event)
        return q.options.all()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['question'] = get_object_or_404(Question, pk=self.kwargs['question'], event=self.request.event)
        return ctx

    def perform_create(self, serializer):
        q = get_object_or_404(Question, pk=self.kwargs['question'], event=self.request.event)
        serializer.save(question=q)
        q.log_action(
            'pretix.event.question.option.added',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk})
        )

    def perform_update(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.question.log_action(
            'pretix.event.question.option.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=merge_dicts(self.request.data, {'ORDER': serializer.instance.position}, {'id': serializer.instance.pk})
        )

    def perform_destroy(self, instance):
        instance.question.log_action(
            'pretix.event.question.option.deleted',
            user=self.request.user,
            auth=self.request.auth,
            data={'id': instance.pk}
        )
        super().perform_destroy(instance)


class NumberInFilter(django_filters.BaseInFilter, django_filters.NumberFilter):
    pass


with scopes_disabled():
    class QuotaFilter(FilterSet):
        items__in = NumberInFilter(
            field_name='items__id',
            lookup_expr='in',
        )

        class Meta:
            model = Quota
            fields = {
                'subevent': ['exact', 'in'],
            }


class QuotaViewSet(ConditionalListView, viewsets.ModelViewSet):
    serializer_class = QuotaSerializer
    queryset = Quota.objects.none()
    filter_backends = (DjangoFilterBackend, TotalOrderingFilter,)
    filterset_class = QuotaFilter
    ordering_fields = ('id', 'size')
    ordering = ('id',)
    permission = None
    write_permission = 'can_change_items'

    def get_queryset(self):
        return self.request.event.quotas.all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset()).distinct()

        page = self.paginate_queryset(queryset)

        if self.request.GET.get('with_availability') == 'true':
            if page:
                qa = QuotaAvailability()
                qa.queue(*page)
                qa.compute(allow_cache=False)
                for q in page:
                    q.available = qa.results[q][0] == Quota.AVAILABILITY_OK
                    q.available_number = qa.results[q][1]

        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def perform_create(self, serializer):
        serializer.save(event=self.request.event)
        serializer.instance.log_action(
            'pretix.event.quota.added',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )
        if serializer.instance.subevent:
            serializer.instance.subevent.log_action(
                'pretix.subevent.quota.added',
                user=self.request.user,
                auth=self.request.auth,
                data=self.request.data
            )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['event'] = self.request.event
        ctx['request'] = self.request
        return ctx

    def perform_update(self, serializer):
        original_data = self.get_serializer(instance=serializer.instance).data

        current_subevent = serializer.instance.subevent
        serializer.save(event=self.request.event)
        request_subevent = serializer.instance.subevent

        if serializer.data == original_data:
            # Performance optimization: If nothing was changed, we do not need to save or log anything.
            # This costs us a few cycles on save, but avoids thousands of lines in our log.
            return

        if original_data['closed'] is True and serializer.instance.closed is False:
            serializer.instance.log_action(
                'pretix.event.quota.opened',
                user=self.request.user,
                auth=self.request.auth,
            )
        elif original_data['closed'] is False and serializer.instance.closed is True:
            serializer.instance.log_action(
                'pretix.event.quota.closed',
                user=self.request.user,
                auth=self.request.auth,
            )

        serializer.instance.log_action(
            'pretix.event.quota.changed',
            user=self.request.user,
            auth=self.request.auth,
            data=self.request.data
        )
        if current_subevent == request_subevent:
            if current_subevent is not None:
                current_subevent.log_action(
                    'pretix.subevent.quota.changed',
                    user=self.request.user,
                    auth=self.request.auth,
                    data=self.request.data
                )
        else:
            if request_subevent is not None:
                request_subevent.log_action(
                    'pretix.subevent.quota.added',
                    user=self.request.user,
                    auth=self.request.auth,
                    data=self.request.data
                )
            if current_subevent is not None:
                current_subevent.log_action(
                    'pretix.subevent.quota.deleted',
                    user=self.request.user,
                    auth=self.request.auth,
                )
        serializer.instance.rebuild_cache()

    def perform_destroy(self, instance):
        instance.log_action(
            'pretix.event.quota.deleted',
            user=self.request.user,
            auth=self.request.auth,
        )
        if instance.subevent:
            instance.subevent.log_action(
                'pretix.subevent.quota.deleted',
                user=self.request.user,
                auth=self.request.auth,
            )
        super().perform_destroy(instance)

    @action(detail=True, methods=['get'])
    def availability(self, request, *args, **kwargs):
        quota = self.get_object()

        qa = QuotaAvailability(full_results=True)
        qa.queue(quota)
        qa.compute()
        avail = qa.results[quota]

        data = {
            'paid_orders': qa.count_paid_orders[quota],
            'pending_orders': qa.count_pending_orders[quota],
            'exited_orders': qa.count_exited_orders[quota],
            'blocking_vouchers': qa.count_vouchers[quota],
            'cart_positions': qa.count_cart[quota],
            'waiting_list': qa.count_pending_orders[quota],
            'available_number': avail[1],
            'available': avail[0] == Quota.AVAILABILITY_OK,
            'total_size': quota.size,
        }
        return Response(data)
