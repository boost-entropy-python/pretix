# Generated by Django 4.2.16 on 2025-01-23 11:27

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pretixbase", "0275_alter_question_valid_number_max_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="hidden_if_item_available_mode",
            field=models.CharField(default="hide", max_length=16, null=True),
        ),
    ]
