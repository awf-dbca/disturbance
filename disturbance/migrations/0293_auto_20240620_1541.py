# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2024-06-20 07:41
from __future__ import unicode_literals

import disturbance.components.approvals.models
import disturbance.components.compliances.models
import disturbance.components.organisations.models
import disturbance.components.proposals.models
from django.conf import settings
import django.contrib.gis.db.models.fields
import django.contrib.postgres.fields.jsonb
import django.core.files.storage
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import multiselectfield.db.fields


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('disturbance', '0292_proposal_reissued'),
    ]

    operations = [
        migrations.CreateModel(
            name='CddpQuestionGroup',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('default', models.BooleanField(default=False)),
                ('members', models.ManyToManyField(to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Spatial Question Group',
            },
        ),
        migrations.CreateModel(
            name='DASMapLayer',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('display_name', models.CharField(max_length=100)),
                ('layer_name', models.CharField(max_length=200)),
                ('layer_url', models.CharField(blank=True, max_length=256, null=True)),
                ('cache_expiry', models.IntegerField(default=300)),
                ('option_for_internal', models.BooleanField(default=True)),
                ('option_for_external', models.BooleanField(default=True)),
                ('display_all_columns', models.BooleanField(default=False)),
            ],
            options={
                'verbose_name': 'Disturbance map layer',
            },
        ),
        migrations.CreateModel(
            name='ExportDocument',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('_file', models.FileField(max_length=255, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.export_file_path)),
                ('created', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
            ],
        ),
        migrations.CreateModel(
            name='ProposalMapDocument',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, max_length=255, verbose_name='name')),
                ('description', models.TextField(blank=True, verbose_name='description')),
                ('uploaded_date', models.DateTimeField(auto_now_add=True)),
                ('_file', models.FileField(max_length=500, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.update_proposal_map_doc_filename)),
                ('input_name', models.CharField(blank=True, max_length=255, null=True)),
                ('can_delete', models.BooleanField(default=True)),
                ('can_hide', models.BooleanField(default=False)),
                ('hidden', models.BooleanField(default=False)),
            ],
        ),
        migrations.CreateModel(
            name='SpatialQueryMetrics',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('when', models.DateTimeField()),
                ('system', models.CharField(max_length=64, verbose_name='Application System Name')),
                ('request_type', models.CharField(choices=[('FULL', 'FULL'), ('PARTIAL', 'PARTIAL'), ('SINGLE', 'SINGLE')], max_length=40)),
                ('sqs_response', django.contrib.postgres.fields.jsonb.JSONField(default=[{}], verbose_name='Response from SQS')),
                ('time_taken', models.DecimalField(decimal_places=3, max_digits=9, verbose_name='Total time for request/response')),
                ('response_cached', models.NullBooleanField()),
            ],
            options={
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='SpatialQueryQuestion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('expiry', models.DateField(blank=True, null=True, verbose_name='Expiry Date')),
                ('visible_to_proponent', models.BooleanField(default=False)),
                ('buffer', models.PositiveIntegerField(blank=True, null=True)),
                ('how', models.CharField(choices=[('Overlapping', 'Overlapping'), ('Outside', 'Outside')], default='Overlapping', max_length=40, verbose_name='Overlapping/Outside')),
                ('column_name', models.CharField(max_length=100, verbose_name='Name of layer attribute/field')),
                ('operator', models.CharField(choices=[('Equals', 'Equals'), ('GreaterThan', 'Greather than'), ('LessThan', 'Less than'), ('IsNotNull', 'Is not null')], default='Equals', max_length=40, verbose_name='Operator')),
                ('value', models.CharField(blank=True, max_length=100, null=True)),
                ('prefix_answer', models.TextField(blank=True, null=True)),
                ('no_polygons_proponent', models.IntegerField(blank=True, default=-1, verbose_name='No. of polygons to process (Proponent)')),
                ('answer', models.TextField(blank=True, null=True)),
                ('prefix_info', models.CharField(blank=True, max_length=100, null=True)),
                ('no_polygons_assessor', models.IntegerField(blank=True, default=-1, verbose_name='No. of polygons to process (Assessor)')),
                ('assessor_info', models.TextField(blank=True, null=True)),
                ('show_add_info_section_prop', models.BooleanField(default=False, verbose_name='Show additional info section (Proponent)')),
                ('proponent_items', django.contrib.postgres.fields.jsonb.JSONField(default=[{}], verbose_name='Proponent response set')),
                ('assessor_items', django.contrib.postgres.fields.jsonb.JSONField(default=[{}], verbose_name='Assessor response set')),
                ('regions', models.CharField(blank=True, choices=[('All', 'All')], default='All', max_length=40, verbose_name='Regions')),
            ],
            options={
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='TaskMonitor',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_id', models.PositiveIntegerField()),
                ('status', models.CharField(choices=[('failed', 'Failed'), ('created', 'Created'), ('running', 'Running'), ('completed', 'Completed'), ('cancelled', 'Cancelled'), ('error', 'Error'), ('max_queue_time', 'Max_Queue_Time_Reached'), ('max_retries', 'Max_Retries_Reached')], default='created', max_length=32, verbose_name='Task Status')),
                ('retries', models.PositiveSmallIntegerField(default=0)),
                ('info', models.TextField(blank=True, null=True)),
                ('created', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
            ],
            options={
                'verbose_name_plural': 'Task Monitor',
            },
        ),
        migrations.AlterModelOptions(
            name='activitymatrix',
            options={'verbose_name_plural': 'Approval matrix'},
        ),
        migrations.AddField(
            model_name='proposal',
            name='add_info_applicant',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='add_info_assessor',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='gis_info',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='history_add_info_assessor',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='layer_data',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='prefill_timestamp',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='proposal_geom',
            field=django.contrib.gis.db.models.fields.MultiPolygonField(blank=True, null=True, srid=4326),
        ),
        migrations.AddField(
            model_name='proposal',
            name='refresh_timestamp',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proposal',
            name='shapefile_json',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True, verbose_name='Source/Submitter (multi) polygon geometry'),
        ),
        migrations.AlterField(
            model_name='amendmentrequestdocument',
            name='_file',
            field=models.FileField(max_length=500, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.update_amendment_request_doc_filename),
        ),
        migrations.AlterField(
            model_name='applicationtype',
            name='name',
            field=models.CharField(choices=[('Disturbance', 'Disturbance'), ('Disturbance Training', 'Disturbance Training'), ('Disturbance Demo', 'Disturbance Demo'), ('Ecological Thinning', 'Ecological Thinning'), ('Powerline Maintenance', 'Powerline Maintenance'), ('Apiary', 'Apiary'), ('Temporary Use', 'Temporary Use'), ('Site Transfer', 'Site Transfer'), ('Prescribed Burning', 'Prescribed Burning')], max_length=64, verbose_name='Application Type name'),
        ),
        migrations.AlterField(
            model_name='approvaldocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.approvals.models.update_approval_doc_filename),
        ),
        migrations.AlterField(
            model_name='approvallogdocument',
            name='_file',
            field=models.FileField(null=True, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.approvals.models.update_approval_comms_log_filename),
        ),
        migrations.AlterField(
            model_name='compliancedocument',
            name='_file',
            field=models.FileField(max_length=500, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.compliances.models.update_proposal_complaince_filename),
        ),
        migrations.AlterField(
            model_name='compliancelogdocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.compliances.models.update_compliance_comms_log_filename),
        ),
        migrations.AlterField(
            model_name='deedpolldocument',
            name='_file',
            field=models.FileField(max_length=255, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=''),
        ),
        migrations.AlterField(
            model_name='globalsettings',
            name='key',
            field=models.CharField(choices=[('assessment_reminder_days', 'Assessment reminder days'), ('das_sharepoint_page', 'DAS Sharepoint page'), ('proposal_assess_help_page', 'DAS Proposal assess help page'), ('compliance_assess_help_page', 'DAS compliance assess help page'), ('referral_assess_help_page', 'DAS referral assess help page'), ('proposal_approver_help_page', 'DAS Proposal approver help page'), ('shapefile_info', 'Shapefile further information'), ('proposal_type_help_url', 'Proposal Type help url'), ('region_help_url', 'Region help url'), ('district_help_url', 'District help url'), ('activity_type_help_url', 'Activity type help url'), ('sub_activity_1_help_url', 'Sub activity 1 help url'), ('sub_activity_2_help_url', 'Sub activity 2 help url'), ('category_help_url', 'Category help url'), ('max_no_polygon', 'Maximum number of polygons allowed in the Shapefile')], max_length=255, unique=True),
        ),
        migrations.AlterField(
            model_name='organisationlogdocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.organisations.models.update_organisation_comms_log_filename),
        ),
        migrations.AlterField(
            model_name='organisationrequest',
            name='identification',
            field=models.FileField(blank=True, null=True, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to='organisation/requests/%Y/%m/%d'),
        ),
        migrations.AlterField(
            model_name='organisationrequestlogdocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.organisations.models.update_organisation_request_comms_log_filename),
        ),
        migrations.AlterField(
            model_name='proposalapiarydocument',
            name='_file',
            field=models.FileField(max_length=512, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.update_apiary_doc_filename),
        ),
        migrations.AlterField(
            model_name='proposaldocument',
            name='_file',
            field=models.FileField(max_length=500, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.update_proposal_doc_filename),
        ),
        migrations.AlterField(
            model_name='proposallogdocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.proposals.models.update_proposal_comms_log_filename),
        ),
        migrations.AlterField(
            model_name='proposaltype',
            name='name',
            field=models.CharField(choices=[('Disturbance', 'Disturbance'), ('Disturbance Training', 'Disturbance Training'), ('Disturbance Demo', 'Disturbance Demo'), ('Ecological Thinning', 'Ecological Thinning'), ('Powerline Maintenance', 'Powerline Maintenance'), ('Apiary', 'Apiary'), ('Temporary Use', 'Temporary Use'), ('Site Transfer', 'Site Transfer'), ('Prescribed Burning', 'Prescribed Burning')], default='Disturbance', max_length=64, verbose_name='Application name (eg. Disturbance, Apiary)'),
        ),
        migrations.AlterField(
            model_name='publicliabilityinsurancedocument',
            name='_file',
            field=models.FileField(max_length=255, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=''),
        ),
        migrations.AlterField(
            model_name='questionoption',
            name='label',
            field=models.CharField(max_length=255, unique=True),
        ),
        migrations.AlterField(
            model_name='questionoption',
            name='value',
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name='renewaldocument',
            name='_file',
            field=models.FileField(storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=disturbance.components.approvals.models.update_approval_doc_filename),
        ),
        migrations.AlterField(
            model_name='sectionquestion',
            name='tag',
            field=multiselectfield.db.fields.MultiSelectField(blank=True, choices=[('isCopiedToPermit', 'isCopiedToPermit'), ('isRequired', 'isRequired'), ('canBeEditedByAssessor', 'canBeEditedByAssessor'), ('isRepeatable', 'isRepeatable'), ('isTitleColumnForDashboard', 'isTitleColumnForDashboard')], max_length=400, null=True),
        ),
        migrations.AlterField(
            model_name='supportingapplicationdocument',
            name='_file',
            field=models.FileField(max_length=255, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=''),
        ),
        migrations.AlterField(
            model_name='temporaryusepublicliabilityinsurancedocument',
            name='_file',
            field=models.FileField(max_length=255, storage=django.core.files.storage.FileSystemStorage(base_url='/private-media/', location='private-media/'), upload_to=''),
        ),
        migrations.AddField(
            model_name='taskmonitor',
            name='proposal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='disturbance.Proposal'),
        ),
        migrations.AddField(
            model_name='taskmonitor',
            name='requester',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='spatialqueryquestion',
            name='answer_mlq',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='question_options', to='disturbance.QuestionOption'),
        ),
        migrations.AddField(
            model_name='spatialqueryquestion',
            name='group',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='groups', to='disturbance.CddpQuestionGroup'),
        ),
        migrations.AddField(
            model_name='spatialqueryquestion',
            name='layer',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='layers', to='disturbance.DASMapLayer'),
        ),
        migrations.AddField(
            model_name='spatialqueryquestion',
            name='question',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='questions', to='disturbance.MasterlistQuestion'),
        ),
        migrations.AddField(
            model_name='spatialquerymetrics',
            name='proposal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='metrics', to='disturbance.Proposal'),
        ),
        migrations.AddField(
            model_name='proposalmapdocument',
            name='proposal',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='map_documents', to='disturbance.Proposal'),
        ),
        migrations.AddField(
            model_name='exportdocument',
            name='proposal',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='disturbance.Proposal'),
        ),
        migrations.AddField(
            model_name='exportdocument',
            name='requester',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterUniqueTogether(
            name='spatialqueryquestion',
            unique_together=set([('question', 'answer_mlq')]),
        ),
    ]