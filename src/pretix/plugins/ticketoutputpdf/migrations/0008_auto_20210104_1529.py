# Generated by Django 3.0.11 on 2021-01-04 15:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ticketoutputpdf', '0007_auto_20181123_1059'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ticketlayout',
            name='layout',
            field=models.TextField(default='[{\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"274.60",\n        "fontsize":"16.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"175.00",\n        "content":"event_name",\n        "text":"Sample event name",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"262.90",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"110.00",\n        "content":"itemvar",\n        "text":"Sample product – sample variation",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"252.50",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"110.00",\n        "content":"attendee_name",\n        "text":"John Doe",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"242.10",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"110.00",\n        "content":"event_begin",\n        "text":"2016-05-31 20:00",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"231.70",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"110.00",\n        "content":"seat",\n        "text":"Ground floor, Row 3, Seat 4",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"204.80",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"110.00",\n        "content":"event_location",\n        "text":"Random City",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"17.50",\n        "bottom":"194.50",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"30.00",\n        "content":"order",\n        "text":"A1B2C",\n        "align":"left"\n    },\n    {\n        "type":"textarea",\n        "left":"52.50",\n        "bottom":"194.50",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"45.00",\n        "content":"price",\n        "text":"123.45 EUR",\n        "align":"right"\n    },\n    {\n        "type":"textarea",\n        "left":"102.50",\n        "bottom":"194.50",\n        "fontsize":"13.0",\n        "color":[\n            0,\n            0,\n            0,\n            1\n        ],\n        "fontfamily":"Open Sans",\n        "bold":false,\n        "italic":false,\n        "width":"90.00",\n        "content":"secret",\n        "text":"tdmruoekvkpbv1o2mv8xccvqcikvr58u",\n        "align":"left"\n    },\n    {\n        "type":"barcodearea",\n        "left":"130.40",\n        "bottom":"204.50",\n        "size":"64.00"\n    },\n    {\n        "type":"poweredby",\n        "left":"88.72",\n        "bottom":"10.00",\n        "size":"20.00",\n        "content":"dark"\n    }]'),
        ),
    ]
