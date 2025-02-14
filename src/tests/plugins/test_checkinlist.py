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
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scope

from pretix.base.models import (
    Event, InvoiceAddress, Item, Order, OrderPosition, Organizer,
)
from pretix.plugins.checkinlists.exporters import CSVCheckinList


@pytest.fixture
def event():
    """Returns an event instance"""
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o, name='Dummy', slug='dummy',
            date_from=now(),
            plugins='pretix.plugins.checkinlists,tests.testdummy',
        )
        event.settings.set('attendee_names_asked', True)
        event.settings.set('name_scheme', 'title_given_middle_family')
        event.settings.set('locales', ['en', 'de'])
        event.checkin_lists.create(name="Default", all_products=True)

        order_paid = Order.objects.create(
            code='FOO', event=event, email='dummy@dummy.test', phone="+498912345678",
            status=Order.STATUS_PAID,
            datetime=datetime.datetime(2019, 2, 22, 14, 0, 0, tzinfo=datetime.timezone.utc), expires=now() + datetime.timedelta(days=10),
            total=33, locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        item_ticket = Item.objects.create(event=event, name="Ticket", default_price=23, admission=True)
        OrderPosition.objects.create(
            order=order_paid,
            item=item_ticket,
            variation=None,
            price=Decimal("23"),
            attendee_name_parts={"title": "Mr", "given_name": "Peter", "middle_name": "A", "family_name": "Jones"},
            secret='hutjztuxhkbtwnesv2suqv26k6ttytxx'
        )
        OrderPosition.objects.create(
            order=order_paid,
            item=item_ticket,
            variation=None,
            price=Decimal("13"),
            attendee_name_parts={"title": "Mrs", "given_name": "Andrea", "middle_name": "J", "family_name": "Zulu"},
            secret='ggsngqtnmhx74jswjngw3fk8pfwz2a7k'
        )
        yield event


def clean(d):
    return d.replace("\r", "").replace("\n", "")


@pytest.mark.django_db
def test_csv_simple(event):
    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title","Attendee name:
 First name","Attendee name: Middle name","Attendee name: Family name","Product","Price","Checked in","Checked out","Automatically
 checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special attention",
"Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until","Address","ZIP code",
"City","Country","State"
"FOO","Mr Peter A Jones","Mr","Peter","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"FOO","Mrs Andrea J Zulu","Mrs","Andrea","J","Zulu","Ticket","13.00","","","No","ggsngqtnmhx74jswjngw3fk8pfwz2a7k",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")


@pytest.mark.django_db
def test_csv_order_by_name_parts(event):  # noqa
    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name:given_name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title",
"Attendee name: First name","Attendee name: Middle name","Attendee name: Family name","Product","Price",
"Checked in","Checked out","Automatically checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special
 attention","Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until",
"Address","ZIP code","City","Country","State"
"FOO","Mrs Andrea J Zulu","Mrs","Andrea","J","Zulu","Ticket","13.00","","","No","ggsngqtnmhx74jswjngw3fk8pfwz2a7k",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"FOO","Mr Peter A Jones","Mr","Peter","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")
    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name:family_name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title",
"Attendee name: First name","Attendee name: Middle name","Attendee name: Family name","Product","Price",
"Checked in","Checked out","Automatically checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special
 attention","Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until",
"Address","ZIP code","City","Country","State"
"FOO","Mr Peter A Jones","Mr","Peter","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"FOO","Mrs Andrea J Zulu","Mrs","Andrea","J","Zulu","Ticket","13.00","","","No","ggsngqtnmhx74jswjngw3fk8pfwz2a7k",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")


@pytest.mark.django_db
def test_csv_order_by_inherited_name_parts(event):  # noqa
    with scope(organizer=event.organizer):
        OrderPosition.objects.filter(attendee_name_cached__icontains="Andrea").delete()
        op = OrderPosition.objects.get()
        op.attendee_name_parts = {}
        op.save()
        order2 = Order.objects.create(
            code='BAR', event=event, email='dummy@dummy.test', phone='+498912345678',
            status=Order.STATUS_PAID,
            datetime=datetime.datetime(2019, 2, 22, 14, 0, 0, tzinfo=datetime.timezone.utc), expires=now() + datetime.timedelta(days=10),
            total=33, locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        OrderPosition.objects.create(
            order=order2,
            item=event.items.first(),
            variation=None,
            company='BARCORP',
            price=Decimal("23"),
            secret='hutjztuxhkbtwnesv2suqv26k6ttytyy'
        )
        InvoiceAddress.objects.create(
            order=event.orders.get(code='BAR'),
            company='FOOCORP',
            name_parts={"title": "Mr", "given_name": "Albert", "middle_name": "J", "family_name": "Zulu", "_scheme": "title_given_middle_family"}
        )
        InvoiceAddress.objects.create(
            order=event.orders.get(code='FOO'),
            company='FOOCORP',
            name_parts={"title": "Mr", "given_name": "Paul", "middle_name": "A", "family_name": "Jones", "_scheme": "title_given_middle_family"}
        )

    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title",
"Attendee name: First name","Attendee name: Middle name","Attendee name: Family name","Product","Price",
"Checked in","Checked out","Automatically checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special
 attention","Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until",
"Address","ZIP code","City","Country","State"
"BAR","Mr Albert J Zulu","Mr","Albert","J","Zulu","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytyy",
"dummy@dummy.test","'+498912345678","BARCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"FOO","Mr Paul A Jones","Mr","Paul","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","FOOCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")
    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name:given_name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title",
"Attendee name: First name","Attendee name: Middle name","Attendee name: Family name","Product","Price",
"Checked in","Checked out","Automatically checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special
 attention","Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until",
"Address","ZIP code","City","Country","State"
"BAR","Mr Albert J Zulu","Mr","Albert","J","Zulu","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytyy",
"dummy@dummy.test","'+498912345678","BARCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"FOO","Mr Paul A Jones","Mr","Paul","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","FOOCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")
    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'name:family_name',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title",
"Attendee name: First name","Attendee name: Middle name","Attendee name: Family name","Product","Price",
"Checked in","Checked out","Automatically checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special
 attention","Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until",
"Address","ZIP code","City","Country","State"
"FOO","Mr Paul A Jones","Mr","Paul","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","FOOCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
"BAR","Mr Albert J Zulu","Mr","Albert","J","Zulu","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytyy",
"dummy@dummy.test","'+498912345678","BARCORP","","2019-02-22","14:00:00","No","","","","","","","","","","","","","","",""
""")


@pytest.mark.django_db
def test_csv_order_by_orderdatetime(event):
    order1 = event.orders.first()
    order1.checkin_text = 'meow'
    order1.save()
    order2 = Order.objects.create(
        code='FOO2', event=event, email='dummy@dummy.test', phone="+498912345678",
        status=Order.STATUS_PAID,
        datetime=datetime.datetime(2019, 2, 22, 22, 0, 0, tzinfo=datetime.timezone.utc),
        expires=now() + datetime.timedelta(days=10),
        total=33, locale='en', checkin_text='beep',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    item_ticket = Item.objects.create(event=event, name="Ticket2", default_price=23, admission=True, checkin_text='boop')
    OrderPosition.objects.create(
        order=order2,
        item=item_ticket,
        variation=None,
        price=Decimal("23"),
        attendee_name_parts={"title": "Mx", "given_name": "Alex", "middle_name": "F", "family_name": "Nord"},
        secret='asdfasdfasdfasdfasdfasdfasfdasdf'
    )

    c = CSVCheckinList(event, organizer=event.organizer)
    _, _, content = c.render({
        'list': event.checkin_lists.first().pk,
        'secrets': True,
        'sort': 'order_datetime',
        '_format': 'default',
        'questions': []
    })
    assert clean(content.decode()) == clean(""""Order code","Attendee name","Attendee name: Title","Attendee name:
 First name","Attendee name: Middle name","Attendee name: Family name","Product","Price","Checked in","Checked out","Automatically
 checked in","Secret","Email","Phone number","Company","Voucher code","Order date","Order time","Requires special attention",
"Comment","Check-in text","Seat ID","Seat name","Seat zone","Seat row","Seat number","Blocked","Valid from","Valid until","Address","ZIP code",
"City","Country","State"
"FOO2","Mx Alex F Nord","Mx","Alex","F","Nord","Ticket2","23.00","","","No","asdfasdfasdfasdfasdfasdfasfdasdf",
"dummy@dummy.test","'+498912345678","","","2019-02-22","22:00:00","No","","beep\nboop","","","","","","","","","","","","",""
"FOO","Mr Peter A Jones","Mr","Peter","A","Jones","Ticket","23.00","","","No","hutjztuxhkbtwnesv2suqv26k6ttytxx",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","meow","","","","","","","","","","","","",""
"FOO","Mrs Andrea J Zulu","Mrs","Andrea","J","Zulu","Ticket","13.00","","","No","ggsngqtnmhx74jswjngw3fk8pfwz2a7k",
"dummy@dummy.test","'+498912345678","","","2019-02-22","14:00:00","No","","meow","","","","","","","","","","","","",""
""")
