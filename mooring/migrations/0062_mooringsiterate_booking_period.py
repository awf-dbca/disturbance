# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-09-07 05:36
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mooring', '0061_auto_20180907_1130'),
    ]

    operations = [
        migrations.AddField(
            model_name='mooringsiterate',
            name='booking_period',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='mooring.BookingPeriod'),
        ),
    ]