# Generated by Django 3.0.11 on 2021-01-19 11:16

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sendmail', '0003_remove_rule_test_field'),
    ]

    operations = [
        migrations.AlterField(
            model_name='scheduledmail',
            name='sent',
            field=models.BooleanField(default=False),
        ),
    ]
