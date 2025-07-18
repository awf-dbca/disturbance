import logging
import copy
import subprocess
import collections
import json
import datetime
import pytz
import requests
import re
import traceback
import os

from dateutil.relativedelta import relativedelta
from django.contrib.gis.db.models.fields import PointField, MultiPolygonField, GeometryField, GeometryCollectionField
from django.contrib.gis.db.models.manager import GeoManager
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.contrib.gis.measure import Distance
from django.contrib.postgres.fields import ArrayField
from django.db import models,transaction
from django.contrib.gis.db import models as gis_models
from django.db.models import Q, Max, F
from django.dispatch import receiver
from django.db.models.signals import pre_delete, post_save
from django.utils.encoding import python_2_unicode_compatible
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields.jsonb import JSONField
from django.utils import timezone
from django.core.paginator import Paginator

from dirtyfields import DirtyFieldsMixin
from reversion.models import Version
from deepdiff import DeepDiff
from multiselectfield import MultiSelectField
from smart_selects.db_fields import ChainedForeignKey, ChainedManyToManyField
from rest_framework import serializers
from ast import literal_eval
from taggit.models import TaggedItemBase

from ledger.checkout.utils import createCustomBasket
from ledger.payments.invoice.utils import CreateInvoiceBasket
from ledger.settings_base import TIME_ZONE

from ledger.accounts.models import EmailUser, RevisionedMixin
from ledger.payments.models import Invoice
from disturbance import exceptions
from disturbance.components.organisations.models import Organisation
from disturbance.components.main.models import CommunicationsLogEntry, UserAction, Document, Region, District, \
    ApplicationType, RegionDbca, DistrictDbca, CategoryDbca, DASMapLayer, TaskMonitor, RequestTypeEnum
from disturbance.components.main.utils import get_department_user
from disturbance.components.proposals.email import (
        send_referral_email_notification,
        send_apiary_referral_email_notification,
        send_apiary_referral_complete_email_notification,
        send_proposal_decline_email_notification,
        send_proposal_approval_email_notification,
        send_amendment_email_notification,
        send_submit_email_notification,
        send_external_submit_email_notification,
        send_approver_decline_email_notification,
        send_approver_approve_email_notification,
        send_referral_complete_email_notification,
        send_proposal_approver_sendback_email_notification,
        send_referral_recall_email_notification,
        send_site_transfer_approval_email_notification,
        )
from disturbance.ordered_model import OrderedModel
import copy
import subprocess
from multiselectfield import MultiSelectField
from smart_selects.db_fields import ChainedForeignKey, ChainedManyToManyField, GroupedForeignKey
from django.core.urlresolvers import reverse


from disturbance.settings import SITE_STATUS_DRAFT, SITE_STATUS_PENDING, SITE_STATUS_APPROVED, SITE_STATUS_DENIED, \
    SITE_STATUS_CURRENT, RESTRICTED_RADIUS, SITE_STATUS_TRANSFERRED, PAYMENT_SYSTEM_ID, PAYMENT_SYSTEM_PREFIX, \
    SITE_STATUS_SUSPENDED, SITE_STATUS_NOT_TO_BE_REISSUED, \
    CRS, OGR2OGR

from django.conf import settings
from django.core.files.storage import FileSystemStorage
#private_storage = FileSystemStorage(location=settings.BASE_DIR+"/private-media/", base_url='/private-media/')
private_storage = FileSystemStorage(location="private-media/", base_url='/private-media/')

logger = logging.getLogger(__name__)

DATE_FMT = '%Y-%m-%d'
DATETIME_FMT = '%Y-%m-%d %H:%M:%S'


def update_proposal_doc_filename(instance, filename):
    return 'proposals/{}/documents/{}'.format(instance.proposal.id,filename)

def update_proposal_map_doc_filename(instance, filename):
    return 'proposals/{}/documents/map_docs/{}'.format(instance.proposal.id,filename)

def update_proposal_comms_log_filename(instance, filename):
    return 'proposals/{}/communications/{}/{}'.format(instance.log_entry.proposal.id,instance.log_entry.id,filename)

def update_amendment_request_doc_filename(instance, filename):
    return 'proposals/{}/amendment_request_documents/{}'.format(instance.amendment_request.proposal.id,filename)

def update_apiary_doc_filename(instance, filename):
    return 'proposals/{}/apiary_documents/{}'.format(instance.apiary_documents.proposal.id, filename)

#def update_temporary_use_doc_filename(instance, filename):
#    return 'proposals/{}/apiary_temporary_use_documents/{}'.format(instance.apiary_temporary_use.proposal.id, filename)
#
#def update_site_transfer_doc_filename(instance, filename):
#    return 'proposals/{}/apiary_site_transfer_documents/{}'.format(instance.apiary_site_transfer.proposal.id, filename)


class ProposalType(models.Model):

    description = models.CharField(max_length=256, blank=True, null=True)
    name = models.CharField(verbose_name='Application name (eg. Disturbance, Apiary)', max_length=64, choices=ApplicationType.APPLICATION_TYPES, default=ApplicationType.APPLICATION_TYPES[0][0])
    schema = JSONField()
    replaced_by = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    version = models.SmallIntegerField(default=1, blank=False, null=False)
    # domain_used = models.CharField(max_length=40, choices=DOMAIN_USED_CHOICES, default=DOMAIN_USED_CHOICES[0][0])

    def __str__(self):
        return '{} - v{}'.format(self.name, self.version)

    class Meta:
        app_label = 'disturbance'
        unique_together = ('name', 'version')
        verbose_name= 'Schema Proposal Type'

    @property
    def latest(self):
        if self.name:
            last_record=ProposalType.objects.filter(name=self.name).order_by('-version')[0]
            if last_record==self:
                return True
            else:
                False
        return False

    @property
    def name_with_version(self):
        return '{} - v{}'.format(self.name, self.version)

    @property
    def apiary_group_proposal_type(self):
        apiary = False
        if self.name and self.name in (
                ApplicationType.APIARY,
                ApplicationType.TEMPORARY_USE,
                ApplicationType.SITE_TRANSFER,
                ):
            apiary = True
        return apiary



class TaggedProposalAssessorGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'disturbance'

class TaggedProposalAssessorGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'disturbance'

class ProposalAssessorGroup(models.Model):
    name = models.CharField(max_length=255)
    #members = models.ManyToManyField(EmailUser,blank=True)
    #regions = TaggableManager(verbose_name="Regions",help_text="A comma-separated list of regions.",through=TaggedProposalAssessorGroupRegions,related_name = "+",blank=True)
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.",through=TaggedProposalAssessorGroupActivities,related_name = "+",blank=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalAssessorGroup.objects.get(default=True)
        except ProposalAssessorGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')
#            elif default and not self.default:
#                raise ValidationError('There can only be one default proposal assessor group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal assessor group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_officer == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_assessor','with_referral','with_assessor_requirements']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class TaggedProposalApproverGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'disturbance'

class TaggedProposalApproverGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'disturbance'

class ProposalApproverGroup(models.Model):
    name = models.CharField(max_length=255)
    #members = models.ManyToManyField(EmailUser,blank=True)
    #regions = TaggableManager(verbose_name="Regions",help_text="A comma-separated list of regions.",through=TaggedProposalApproverGroupRegions,related_name = "+",blank=True)
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.",through=TaggedProposalApproverGroupActivities,related_name = "+",blank=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalApproverGroup.objects.get(default=True)
        except ProposalApproverGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')

#            if int(self.pk) != int(default.id):
#                if default and self.default:
#                    raise ValidationError('There can only be one default proposal approver group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal approver group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_approver == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_approver']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class CddpQuestionGroup(models.Model):
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(EmailUser)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Spatial Question Group'

    def __str__(self):
        return self.name

#    def clean(self):
#        try:
#            default = CddpQuestionGroup.objects.get(default=True)
#        except CddpQuestionGroup.DoesNotExist:
#            default = None
#
#        if self.pk:
#            if not self.default and not self.region:
#                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')
#        else:
#            if default and self.default:
#                raise ValidationError('There can only be one default proposal approver group')

#    def member_is_assigned(self,member):
#        for p in self.current_proposals:
#            if p.assigned_approver == member:
#                return True
#        return False

    @property
    def current_cddp_questions(self):
        return SpatialQueryQuestion.objects.filter(group=self.name)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

#class __ApiaryAssessorGroup(models.Model):
#    #site = models.OneToOneField(Site, default='1')
#    members = models.ManyToManyField(EmailUser)
#
#    def __str__(self):
#        return 'Apiary Assessors Group'
#
#    @property
#    def all_members(self):
#        all_members = []
#        all_members.extend(self.members.all())
#        member_ids = [m.id for m in self.members.all()]
#        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
#        return all_members
#
#    @property
#    def filtered_members(self):
#        return self.members.all()
#
#    class Meta:
#        app_label = 'disturbance'
#        verbose_name_plural = 'Apiary Assessors Group'
#
#    @property
#    def members_email(self):
#        return [i.email for i in self.members.all()]



class DefaultDocument(Document):
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    class Meta:
        app_label = 'disturbance'
        abstract =True

    def delete(self):
        if self.can_delete:
            return super(DefaultDocument, self).delete()
        logger.info('Cannot delete existing document object after Application has been submitted (including document submitted before Application pushback to status Draft): {}'.format(self.name))

class ProposalMapDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='map_documents')
    _file = models.FileField(upload_to=update_proposal_map_doc_filename, max_length=500, storage=private_storage)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    can_hide= models.BooleanField(default=False) # after initial submit, document cannot be deleted but can be hidden
    hidden=models.BooleanField(default=False) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalMapDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'disturbance'

class ProposalDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_doc_filename, max_length=500, storage=private_storage)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    can_hide= models.BooleanField(default=False) # after initial submit, document cannot be deleted but can be hidden
    hidden=models.BooleanField(default=False) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'disturbance'

def fee_invoice_references_default():
    return []

class Proposal(DirtyFieldsMixin, RevisionedMixin):
    CUSTOMER_STATUS_TEMP = 'temp'
    CUSTOMER_STATUS_DRAFT = 'draft'
    CUSTOMER_STATUS_WITH_ASSESSOR = 'with_assessor'
    CUSTOMER_STATUS_AMENDMENT_REQUEST = 'amendment_required'
    CUSTOMER_STATUS_APPROVED = 'approved'
    CUSTOMER_STATUS_DECLINED = 'declined'
    CUSTOMER_STATUS_DISCARDED = 'discarded'
    CUSTOMER_STATUS_CHOICES = ((CUSTOMER_STATUS_TEMP, 'Temporary'),
                               (CUSTOMER_STATUS_DRAFT, 'Draft'),
                               (CUSTOMER_STATUS_WITH_ASSESSOR, 'Under Review'),
                               (CUSTOMER_STATUS_AMENDMENT_REQUEST, 'Amendment Required'),
                               (CUSTOMER_STATUS_APPROVED, 'Approved'),
                               (CUSTOMER_STATUS_DECLINED, 'Declined'),
                               (CUSTOMER_STATUS_DISCARDED, 'Discarded'),
                               )
    # List of statuses from above that allow a customer to edit an application.
    CUSTOMER_EDITABLE_STATE = [CUSTOMER_STATUS_TEMP, CUSTOMER_STATUS_DRAFT, CUSTOMER_STATUS_AMENDMENT_REQUEST, ]

    APPLICANT_TYPE_ORGANISATION = 'organisation'
    APPLICANT_TYPE_PROXY = 'proxy' # proxy also represents an individual making an Apiary application
    APPLICANT_TYPE_SUBMITTER = 'submitter'

    # List of statuses from above that allow a customer to view an application (read-only)
    CUSTOMER_VIEWABLE_STATE = ['with_assessor', 'under_review', 'id_required', 'returns_required', 'approved', 'declined']

    PROCESSING_STATUS_TEMP = 'temp'
    PROCESSING_STATUS_DRAFT = 'draft'
    PROCESSING_STATUS_WITH_ASSESSOR = 'with_assessor'
    PROCESSING_STATUS_WITH_REFERRAL = 'with_referral'
    PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS = 'with_assessor_requirements'
    PROCESSING_STATUS_WITH_APPROVER = 'with_approver'
    PROCESSING_STATUS_RENEWAL = 'renewal'
    PROCESSING_STATUS_LICENCE_AMENDMENT = 'licence_amendment'
    PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE = 'awaiting_applicant_response'
    PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE = 'awaiting_assessor_response'
    PROCESSING_STATUS_AWAITING_RESPONSES = 'awaiting_responses'
    PROCESSING_STATUS_READY_FOR_CONDITIONS = 'ready_for_conditions'
    PROCESSING_STATUS_READY_TO_ISSUE = 'ready_to_issue'
    PROCESSING_STATUS_APPROVED = 'approved'
    PROCESSING_STATUS_DECLINED = 'declined'
    PROCESSING_STATUS_DISCARDED = 'discarded'
    PROCESSING_STATUS_CHOICES = ((PROCESSING_STATUS_TEMP, 'Temporary'),
                                 (PROCESSING_STATUS_DRAFT, 'Draft'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR, 'With Assessor'),
                                 (PROCESSING_STATUS_WITH_REFERRAL, 'With Referral'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS, 'With Assessor (Requirements)'),
                                 (PROCESSING_STATUS_WITH_APPROVER, 'With Approver'),
                                 (PROCESSING_STATUS_RENEWAL, 'Renewal'),
                                 (PROCESSING_STATUS_LICENCE_AMENDMENT, 'Licence Amendment'),
                                 (PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE, 'Awaiting Applicant Response'),
                                 (PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE, 'Awaiting Assessor Response'),
                                 (PROCESSING_STATUS_AWAITING_RESPONSES, 'Awaiting Responses'),
                                 (PROCESSING_STATUS_READY_FOR_CONDITIONS, 'Ready for Conditions'),
                                 (PROCESSING_STATUS_READY_TO_ISSUE, 'Ready to Issue'),
                                 (PROCESSING_STATUS_APPROVED, 'Approved'),
                                 (PROCESSING_STATUS_DECLINED, 'Declined'),
                                 (PROCESSING_STATUS_DISCARDED, 'Discarded'),
                                 )

    ID_CHECK_STATUS_CHOICES = (('not_checked', 'Not Checked'), ('awaiting_update', 'Awaiting Update'),
                               ('updated', 'Updated'), ('accepted', 'Accepted'))

    COMPLIANCE_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('awaiting_returns', 'Awaiting Returns'), ('completed', 'Completed'),
        ('accepted', 'Accepted'))

    CHARACTER_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('accepted', 'Accepted'))

    REVIEW_STATUS_CHOICES = (
        ('not_reviewed', 'Not Reviewed'), ('awaiting_amendments', 'Awaiting Amendments'), ('amended', 'Amended'),
        ('accepted', 'Accepted'))

#    PROPOSAL_STATE_NEW_LICENCE = 'New Licence'
#    PROPOSAL_STATE_AMENDMENT = 'Amendment'
#    PROPOSAL_STATE_RENEWAL = 'Renewal'
#    PROPOSAL_STATE_CHOICES = (
#        (1, PROPOSAL_STATE_NEW_LICENCE),
#        (2, PROPOSAL_STATE_AMENDMENT),
#        (3, PROPOSAL_STATE_RENEWAL),
#    )

    APPLICATION_TYPE_CHOICES = (
        ('new_proposal', 'New Proposal'),
        ('amendment', 'Amendment'),
        ('renewal', 'Renewal'),
    )

    proposal_type = models.CharField('Proposal Type', max_length=40, choices=APPLICATION_TYPE_CHOICES,
                                        default=APPLICATION_TYPE_CHOICES[0][0])
    #proposal_state = models.PositiveSmallIntegerField('Proposal state', choices=PROPOSAL_STATE_CHOICES, default=1)

    data = JSONField(blank=True, null=True)
    assessor_data = JSONField(blank=True, null=True)
    comment_data = JSONField(blank=True, null=True)
    add_info_applicant=JSONField(blank=True, null=True) #To store addition info provided by applicant for the question answered by GIS.
    add_info_assessor=JSONField(blank=True, null=True) #To store addition info provided by assessor for the question answered by GIS.
    history_add_info_assessor=JSONField(blank=True, null=True) #To store history of addition info provided by assessor for the question answered by GIS.
    layer_data = JSONField(blank=True, null=True)
    refresh_timestamp = JSONField(blank=True, null=True)
    prefill_timestamp = models.DateTimeField(blank=True, null=True)
    schema = JSONField(blank=False, null=False)
    proposed_issuance_approval = JSONField(blank=True, null=True)
    gis_info = JSONField(blank=True, null=True)
    #hard_copy = models.ForeignKey(Document, blank=True, null=True, related_name='hard_copy')

    customer_status = models.CharField('Customer Status', max_length=40, choices=CUSTOMER_STATUS_CHOICES,
                                       default=CUSTOMER_STATUS_CHOICES[1][0])
    applicant = models.ForeignKey(Organisation, blank=True, null=True, related_name='proposals')

    lodgement_number = models.CharField(max_length=9, blank=True, default='')
    lodgement_sequence = models.IntegerField(blank=True, default=0)
    #lodgement_date = models.DateField(blank=True, null=True)
    lodgement_date = models.DateTimeField(blank=True, null=True)
    # 20200512 - proxy_applicant also represents an individual making an Apiary application
    proxy_applicant = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proxy')
    submitter = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals')

    assigned_officer = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals_assigned', on_delete=models.SET_NULL)
    assigned_approver = models.ForeignKey(EmailUser, blank=True, null=True, related_name='disturbance_proposals_approvals', on_delete=models.SET_NULL)
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[1][0])
    id_check_status = models.CharField('Identification Check Status', max_length=30, choices=ID_CHECK_STATUS_CHOICES,
                                       default=ID_CHECK_STATUS_CHOICES[0][0])
    compliance_check_status = models.CharField('Return Check Status', max_length=30, choices=COMPLIANCE_CHECK_STATUS_CHOICES,
                                            default=COMPLIANCE_CHECK_STATUS_CHOICES[0][0])
    character_check_status = models.CharField('Character Check Status', max_length=30,
                                              choices=CHARACTER_CHECK_STATUS_CHOICES,
                                              default=CHARACTER_CHECK_STATUS_CHOICES[0][0])
    review_status = models.CharField('Review Status', max_length=30, choices=REVIEW_STATUS_CHOICES,
                                     default=REVIEW_STATUS_CHOICES[0][0])

    approval = models.ForeignKey('disturbance.Approval',null=True,blank=True)

    previous_application = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    #self_clone = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True, related_name='proposal_current_state')
    proposed_decline_status = models.BooleanField(default=False)
    # Special Fields
    title = models.CharField(max_length=255,null=True,blank=True)
    activity = models.CharField(max_length=255,null=True,blank=True)
    #region = models.CharField(max_length=255,null=True,blank=True)
    tenure = models.CharField(max_length=255,null=True,blank=True)
    #activity = models.ForeignKey(Activity, null=True, blank=True)
    region = models.ForeignKey(Region, null=True, blank=True)
    district = models.ForeignKey(District, null=True, blank=True)
    #tenure = models.ForeignKey(Tenure, null=True, blank=True)
    application_type = models.ForeignKey(ApplicationType)
    approval_level = models.CharField('Activity matrix approval level', max_length=255,null=True,blank=True)
    approval_level_document = models.ForeignKey(ProposalDocument, blank=True, null=True, related_name='approval_level_document')
    approval_level_comment = models.TextField(blank=True)
    approval_comment = models.TextField(blank=True)
    assessment_reminder_sent = models.BooleanField(default=False)
    weekly_reminder_sent_date = models.DateField(blank=True, null=True)
    sub_activity_level1 = models.CharField(max_length=255,null=True,blank=True)
    sub_activity_level2 = models.CharField(max_length=255,null=True,blank=True)
    management_area = models.CharField(max_length=255,null=True,blank=True)

    # fee_invoice_reference = models.CharField(max_length=50, null=True, blank=True, default='')
    fee_invoice_references = ArrayField(models.CharField(max_length=50, null=True, blank=True, default=''), null=True, default=fee_invoice_references_default)
    migrated = models.BooleanField(default=False)
    shapefile_json = JSONField('Source/Submitter (multi) polygon geometry', blank=True, null=True)
    shapefile_geom = MultiPolygonField('Source/Submitter gdf.exploded (multi) polygon geometry', srid=4326, blank=True, null=True) # for 'pgsql2shp' from KB
    reissued = models.BooleanField(default=False)  
    prefill_requested = models.BooleanField(default=False)           

    class Meta:
        app_label = 'disturbance'
        #ordering = ['-id']

    def __str__(self):
        return str(self.id)
      
    
    def save(self, *args, **kwargs):
        # Store the original values of fields we want to keep track of in 
        # django reversion before they are overwritten by super() below
        original_processing_status = self._original_state['processing_status']
        original_assessor_data = self._original_state['assessor_data']
        original_comment_data = self._original_state['comment_data']

        # Populate self with the new field values
        super(Proposal, self).save(*args, **kwargs)

        # Append 'P' to Proposal id to generate Lodgement number.
        # Lodgement number and lodgement sequence are used to generate Reference.
        if self.lodgement_number == '':
            new_lodgment_id = 'P{0:06d}'.format(self.pk)
            self.lodgement_number = new_lodgment_id
            self.save()

        # If the processing_status has changed then add a reversion comment
        # so we have a way of filtering based on the status changing
        if self.processing_status != original_processing_status:
            self.save(version_comment=f'processing_status: {self.processing_status}')
#            das_geom, created = DASGeometry.objects.update_or_create(
#               
#           )

        elif self.assessor_data != original_assessor_data:
            # Although the status hasn't changed we add the text 'processing_status'
            # So we can filter based on it later (for both assessor_data and comment_data)
            self.save(version_comment='assessor_data: Has changed - tagging with processing_status')
        elif self.comment_data != original_comment_data:
            self.save(version_comment='comment_data: Has changed - tagging with processing_status')

#    def save_geom(self):
#        columns = ['org','app_no','prop_title','appissdate','appstadate','appexpdate','appstatus','assocprop','proptype','propurl','prop_activ','geometry']
#        gdf_concat = gpd.GeoDataFrame(columns=["geometry"], crs=settings.CRS, geometry="geometry") 
#        gdf = gpd.GeoDataFrame.from_features(p.shapefile_json)
#
#       das_geom, created = DASGeometry.objects.update_or_create(
#           proposal = models.ForeignKey(Proposal, unique=True),
#           org = self.applicant.name if p.applicant else None,
#           app_no = self.approval.lodgement_number if p.approval else None,
#           prop_title = self.title,
#           appissdate = self.approval.issue_date.strftime("%Y-%d-%d") if p.approval else None,
#           appstadate = self.approval.start_date.strftime("%Y-%d-%d") if p.approval else None,
#           appexpdate = self.approval.expiry_date.strftime("%Y-%d-%d") if p.approval else None,
#           appstatus = self.approval.status if p.approval else None,
#           assocprop = list(Proposal.objects.filter(approval__lodgement_number=self.approval.lodgement_number).values_list('lodgement_number', flat=True)) if self.approval else None,
#           proptype = self.proposal_type,
#           propurl = settings.BASE_URL + reverse('internal-proposal-detail',kwargs={'proposal_pk': self.id}),
#           prop_activ = self.activity,
#           geometry = GeometryField(srid=4326)
#       )


    @property
    def fee_paid(self):
        if not self.apiary_group_application_type:
            return False
        else:
            return True if self.fee_invoice_references or self.proposal_type == 'amendment' else False

    @property
    def relevant_applicant(self):
        if self.applicant:
            return self.applicant
        elif self.proxy_applicant:
            return self.proxy_applicant
        else:
            return self.submitter

    @property
    def relevant_applicant_name(self):
        if self.applicant:
            return self.applicant.name
        elif self.proxy_applicant:
            return self.proxy_applicant.get_full_name()
        else:
            return self.submitter.get_full_name()

    @property
    def relevant_applicant_description(self):
        if self.applicant:
            return self.applicant.organisation.name
        elif self.proxy_applicant:
            return "{} {}".format(
                self.proxy_applicant.first_name,
                self.proxy_applicant.last_name)
        else:
            return "{} {}".format(
                self.submitter.first_name,
                self.submitter.last_name)

    @property
    def relevant_applicant_email(self):
        if self.applicant and hasattr(self.applicant.organisation, 'email') and self.applicant.organisation.email:
            return self.applicant.organisation.email
        elif self.proxy_applicant:
            return self.proxy_applicant.email
        else:
            return self.submitter.email

    @property
    def relevant_applicant_details(self):
        if self.applicant:
            return '{} \n{}'.format(
                self.applicant.organisation.name,
                self.applicant.address)
        elif self.proxy_applicant:
            return "{} {}\n{}".format(
                self.proxy_applicant.first_name,
                self.proxy_applicant.last_name,
                self.proxy_applicant.addresses.all().first())
        else:
            return "{} {}\n{}".format(
                self.submitter.first_name,
                self.submitter.last_name,
                self.submitter.addresses.all().first())

    @property
    def relevant_applicant_address(self):
        if self.applicant:
            return self.applicant.address
        elif self.proxy_applicant:
            #return self.proxy_applicant.addresses.all().first()
            return self.proxy_applicant.residential_address
        else:
            #return self.submitter.addresses.all().first()
            return self.submitter.residential_address

    @property
    def relevant_applicant_id(self):
        return_value = None
        if self.applicant:
            print("APPLICANT")
            return_value = self.applicant.id
        elif self.proxy_applicant:
            print("PROXY_APPLICANT")
            return_value = self.proxy_applicant.id
        else:
            #return_value = self.submitter.id
            pass
        return return_value

    @property
    def relevant_applicant_type(self):
        if self.applicant:
            return self.APPLICANT_TYPE_ORGANISATION
        elif self.proxy_applicant:
            return self.APPLICANT_TYPE_PROXY
        else:
            return self.APPLICANT_TYPE_SUBMITTER

    @property
    def applicant_field(self):
        if self.applicant:
            return 'applicant'
        elif self.proxy_applicant:
            return 'proxy_applicant'
        else:
            return 'submitter'

    @property
    def reference(self):
        return '{}-{}'.format(self.lodgement_number, self.lodgement_sequence)

    @property
    def get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( dict(id=p.previous_application.id, modified=p.previous_application.modified_date) )
            p = p.previous_application
        return l


    def _get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( [p.id, p.previous_application.id] )
            p = p.previous_application
        return l

    @property
    def is_assigned(self):
        return self.assigned_officer is not None

    @property
    def is_temporary(self):
        return self.customer_status == 'temp' and self.processing_status == 'temp'

    @property
    def can_user_edit(self):
        """
        :return: True if the application is in one of the editable status.
        """
        return self.customer_status in self.CUSTOMER_EDITABLE_STATE

    @property
    def can_user_view(self):
        """
        :return: True if the application is in one of the approved status.
        """
        return self.customer_status in self.CUSTOMER_VIEWABLE_STATE

    @property
    def in_prefill_queue(self):
        """
        :return: True if the application is in the prefilling queue.
        """
        queue_status=[TaskMonitor.STATUS_CREATED, TaskMonitor.STATUS_RUNNING]
        if self.taskmonitor_set.all():
            task=self.taskmonitor_set.latest('id')
            if task.status in queue_status:
                return True        
        return False
    
    @property
    def has_prefilled_once(self):
        """
        :return: True if the application is atleast prefilled once. Otherwise, make the proposal readonly
        """
        return True if self.shapefile_json and self.prefill_timestamp else False

    @property
    def is_discardable(self):
        """
        An application can be discarded by a customer if:
        1 - It is a draft
        2- or if the application has been pushed back to the user
        """
        return self.customer_status == 'draft' or self.processing_status == 'awaiting_applicant_response'

    @property
    def is_deletable(self):
        """
        An application can be deleted only if it is a draft and it hasn't been lodged yet
        :return:
        """
        return self.customer_status == 'draft' and not self.lodgement_number

    @property
    def latest_referrals(self):
        return self.referrals.all()[:2]

    @property
    def regions_list(self):
        #return self.region.split(',') if self.region else []
        return [self.region.name] if self.region else []

    @property
    def permit(self):
        return self.approval.licence_document._file.url if self.approval else None

    @property
    def allowed_assessors(self):
        if self.processing_status == 'with_approver':
            group = self.__approver_group()
        else:
            group = self.__assessor_group()
        return group.members.all() if group else []

    #Compliance and Approvals use assessor group to show/hide compliance/approvals actions on dashboard
    @property
    def compliance_assessors(self):
        group = self.__assessor_group()
        return group.members.all() if group else []

    #Approver group required to show/hide reissue actions on Approval dashboard
    @property
    def allowed_approvers(self):
        group = self.__approver_group()
        return group.members.all() if group else []



    @property
    def can_officer_process(self):
        """
        :return: True if the application is in one of the processable status for Assessor role.
        """
        officer_view_state = ['draft','approved','declined','temp','discarded']
        if self.processing_status in officer_view_state:
            return False
        else:
            return True

    @property
    def amendment_requests(self):
        qs =AmendmentRequest.objects.filter(proposal = self)
        return qs

    @property
    def apiary_group_application_type(self):
        apiary = False
        if self.application_type and self.application_type.name in (
                ApplicationType.APIARY,
                ApplicationType.TEMPORARY_USE,
                ApplicationType.SITE_TRANSFER,
                ):
            apiary = True
        return apiary

    def get_revision(self, version_number):
        """
        Gets a full Proposal version to show when the View button is clicked.
        """

        versions = self.get_reversion_history()

        return versions[version_number].field_dict


        """
        all_revisions_list = list(self.get_reversion_history().values())
        print(all_revisions_list[version_number].field_dict["data"][0].keys())
        version1 = all_revisions_list[version_number].field_dict["data"]
        version = all_revisions_list[version_number].field_dict["data"][0]
        dic = self.flatten_json(version)

        out = {}
        for k, v in dic.items():
            out[k.split('_0_')[1]] = v
        return version1
        """

    def get_revision_flat(self, version_number):
        """
        Gets all the differences in Proposal version to show when the Compare link is clicked.
        """

        all_revisions_list = list(self.get_reversion_history().values())
        version = all_revisions_list[version_number].field_dict["data"][0]
        dic = self.flatten_json(version)

        out = {}
        for k, v in dic.items():
            out[k.split('_0_')[1]] = v
        return out

    def flatten_json(self, dictionary):
        """ 
        Flatten a nested json string.
        """
        from itertools import chain, starmap

        def unpack(parent_key, parent_value):
            # Unpack one level of nesting in json file.
            # Unpack one level only!!!
            
            if isinstance(parent_value, dict):
                for key, value in parent_value.items():
                    temp1 = parent_key + '_' + key
                    yield temp1, value
            elif isinstance(parent_value, list):
                i = 0 
                for value in parent_value:
                    temp2 = parent_key + '_'+str(i) 
                    i += 1
                    yield temp2, value
            else:
                yield parent_key, parent_value    

                
        # Keep iterating until the termination condition is satisfied
        while True:
            # Keep unpacking the json file until all values are atomic elements (not dictionary or list)
            dictionary = dict(chain.from_iterable(starmap(unpack, dictionary.items())))
            # Terminate condition: not any value in the json file is dictionary or list
            if not any(isinstance(value, dict) for value in dictionary.values()) and \
            not any(isinstance(value, list) for value in dictionary.values()):
                break

        return dictionary

    def get_revision_diff(self, newer_version, older_version):
        """
        Gets all the revision differences between the most recent revision and the revision specified.
        """

        versions = self.get_reversion_history()

        # all_revisions_list = list(self.get_reversion_history().values())

        versions_length = len(versions)

        newer_version = versions[newer_version].field_dict["data"]

        older_version = versions[older_version].field_dict["data"]

        diffs = DeepDiff(newer_version, older_version, ignore_order=True)

        diffs_list = []
        for v in diffs.items():
            if "values_changed" in v:
                for k, v in v[1].items():
                    diffs_list.append({k.split('\'')[-2]:v['new_value'],})
        return diffs_list


    def get_version_differences(self, newer_version: int, older_version: int):
        """ Returns the differences between two versions

        The most recent version is always 0.

        The second most recent version is 1 and so on all the way to the oldest
        version

        This method will raise an Exception if the newer_version argument is
        higher than or equal to the older version to make sure we are indeed
        comparing a newer version with an older version.

        See: https://django-reversion.readthedocs.io for more information.

        """

        # Fail if either argument is negative
        if(newer_version<0 or older_version<0):
            raise Exception('The newer_version and older_version arguements must be 0 or higher')

        # Refuse to compare if the newer version is not actually newer
        if(newer_version>=older_version):
            raise Exception('The newer_version arguement must be smaller than the older_version argument')

        versions = self.get_reversion_history()

        # Complain if either requested version doesn't exist
        if (newer_version>=(len(versions)-1)):
            raise IndexError(f'The newer_version you requested "{newer_version}" doesn\'t exist')

        if (older_version>(len(versions)-1)):
            raise IndexError(f'The older_version you requested "{older_version}" doesn\'t exist')

        newer_version_data = versions[newer_version].field_dict
        older_version_data = versions[older_version].field_dict

        differences = DeepDiff(newer_version_data, older_version_data, ignore_order=True)

        default_mapping = {datetime.datetime: lambda d: str(d)}

        json_differences = json.loads(differences.to_json(default_mapping=default_mapping))

        #logger.debug(f'\n\json_differences = \n\n {json_differences}')

        return json_differences

    def get_version_differences_comment_and_assessor_data(self, field, newer_version: int, older_version: int):
        """ Returns the differences between the comment data of two versions

        Due to the structure of the 'comment_data' and 'assessor_data' fields being different to the 
        structure of the 'data' field, we need some custom logic to return the section identifier and
        the email for the referrer.

        This makes it possible for the compare function on the front end to append the revision notes
        to the correct location quite easily.

        This method only works when each item in the JSON field has it's data in the following
        structure:

        {
        "name": "Section0-7Group1-1-Yes2",
        "assessor": "",
        "referrals": [
            {
            "email": "tracy.sonneman@dbca.wa.gov.au",
            "value": "",
            "full_name": "Tracy Sonneman"
            }
        ]
        },

        """

        # Fail if either argument is negative
        if(newer_version<0 or older_version<0):
            raise Exception('The newer_version and older_version arguements must be 0 or higher')

        # Refuse to compare if the newer version is not actually newer
        if(newer_version>=older_version):
            raise Exception('The newer_version arguement must be smaller than the older_version argument')

        versions = list(Version.objects.get_for_object(self).select_related('revision')\
            .filter(revision__comment__contains='processing_status').get_unique())

        older_version_data = versions[older_version].field_dict[field]
        newer_version_data = versions[newer_version].field_dict[field]

        differences = DeepDiff(older_version_data, newer_version_data, ignore_order=True)

        #logger.debug(f'differences = {differences}')

        json_differences = json.loads(differences.to_json())

        differences_list = []

        if 'values_changed' in json_differences:            
            logger.debug('\n\n values_changed ========================= ')
            values_changed = json_differences['values_changed']

            for key in values_changed:
                # Due to the structure of comment_data and assessor_data we need to get the section name
                # for both the comments and the referral comments.
                #    
                # We also need the email for the Refferal comments.
                #
                # With this information we can attach the revision notes in the right place on the frontend
                # quite easily.
                #
                # Also keep in mind that deep diff will return a different data structure once referral
                # comments have been added.
                
                # Get the number between the first set of square brackets i.e. x in 'root[x].etc[y].etc[z]
                regex = re.search('(?<=\[).+?(?=\])', str(key))
                root_level = regex.group(0)

                differences_list = self.append_to_differences_list_by_field(field, older_version_data, newer_version_data, \
                    values_changed, key, root_level,differences_list)

        return differences_list

    def append_to_differences_list_by_field(self, field, older_version_data, newer_version_data, values_changed, key, root_level, \
                                            differences_list):
        """ Returns the differences list with the appropriate keys for the assessor_data field. """
        
        older_assessor_comment = older_version_data[int(root_level)]['assessor']
        newer_assessor_comment = newer_version_data[int(root_level)]['assessor']

        #logger.debug('key = ' + str(key))

        root_level_name = newer_version_data[int(root_level)]['name']

        if 'assessor_data' == field:
            assessor_suffix = '-Assessor'
            referral_suffix = '-Referral-'
        elif 'comment_data' == field:
            assessor_suffix = '-comment-field-Assessor'
            referral_suffix = '-comment-field-Referral-'            
        else:
            raise ValueError('The field argument must be either assessor_data or comment_data')

        if older_assessor_comment:
            logger.debug('\n There is an older comment: \n' + str(older_assessor_comment))
            # if the assessor comment hasn't changed then it must be a referral comment change that deep diff picked up
            if newer_assessor_comment == older_assessor_comment:
                older_referrals = older_version_data[int(root_level)]['referrals']
                newer_referrals = newer_version_data[int(root_level)]['referrals']
                if newer_referrals:
                    if older_referrals:
                        for i, new_referral in enumerate(newer_referrals):
                            referrer_email = new_referral['email']
                            root_level_name_appended = root_level_name + f'{referral_suffix}{referrer_email}'
                            try:                
                                if new_referral['value'] != older_referrals[i]['value']:
                                    if {root_level_name_appended:older_referrals[i]['value']} not in differences_list:
                                        differences_list.append({root_level_name_appended:older_referrals[i]['value']})
                            except IndexError:
                                # This referral doesn't exist in the older version
                                if new_referral['value']:
                                    differences_list.append({root_level_name_appended:'(Previously Blank)'})
            else:
                logger.debug('key = ' + str(key))
                if 'referral' not in key:
                    root_level_name_appended = root_level_name + assessor_suffix
                    old_value = values_changed[key]['old_value']
                    logger.debug('old_value = ' + str(old_value))
                    if(type(old_value) is dict):
                        # In certain circumstances, deep diff returns a dictionary rather than just a value
                        differences_list.append({root_level_name_appended:old_value['assessor']})
                    else:
                        differences_list.append({root_level_name_appended:old_value})
                else:
                    older_referrals = older_version_data[int(root_level)]['referrals']
                    newer_referrals = newer_version_data[int(root_level)]['referrals']
                    if newer_referrals:
                        for i, new_referral in enumerate(newer_referrals):
                            logger.debug('\n new_referral \n' + str(new_referral['value'] ))
                            logger.debug('\n older_referral \n' + str(older_referrals[i]['value']))
                            referrer_email = new_referral['email']
                            root_level_name_appended = root_level_name + f'{referral_suffix}{referrer_email}'
                            try:
                                if new_referral['value'] != older_referrals[i]['value']:
                                    if {root_level_name_appended:older_referrals[i]['value']} not in differences_list:                        
                                        differences_list.append({root_level_name_appended:older_referrals[i]['value']})
                            except IndexError:
                                # This referral doesn't exist in the older version
                                if new_referral['value']:
                                    differences_list.append({root_level_name_appended:'(Previously Blank)'})
        else:
            if newer_assessor_comment:
                root_level_name_appended = root_level_name + assessor_suffix
                differences_list.append({root_level_name_appended:'(Previously Blank)'})
                older_referrals = older_version_data[int(root_level)]['referrals']
                newer_referrals = newer_version_data[int(root_level)]['referrals']
                if newer_referrals:
                    for i, new_referral in enumerate(newer_referrals):
                        try:
                            if new_referral['value'] != older_referrals[i]['value']:                           
                                referrer_email = new_referral['email']
                                root_level_name_appended = root_level_name + f'{referral_suffix}{referrer_email}'
                                if {root_level_name_appended:older_referrals[i]['value']} not in differences_list:                        
                                    differences_list.append({root_level_name_appended:older_referrals[i]['value']})
                        except IndexError:
                            # This referral doesn't exist in the older version
                            if new_referral['value']:
                                differences_list.append({root_level_name_appended:'(Previously Blank)'})
            else:
                # Edge case. Both the old assessor comment and the new assessor comment are empty
                # Which means the change deep diff picked up must be a referral comment
                older_referrals = older_version_data[int(root_level)]['referrals']
                newer_referrals = newer_version_data[int(root_level)]['referrals']
                if newer_referrals:
                    for i, new_referral in enumerate(newer_referrals):
                        referrer_email = new_referral['email']
                        root_level_name_appended = root_level_name + f'{referral_suffix}{referrer_email}'
                        try:
                            if new_referral['value'] != older_referrals[i]['value']:
                                if {root_level_name_appended:older_referrals[i]['value']} not in differences_list:                         
                                    differences_list.append({root_level_name_appended:older_referrals[i]['value']})
                        except IndexError:
                            # This referral doesn't exist in the older version
                            if new_referral['value']:
                                differences_list.append({root_level_name_appended:'(Previously Blank)'})

        return differences_list

    def get_reversion_history(self):
        """
        Get the revisions for this Proposal where the processing_status has changed
        """
        # Get all revisions that have been submitted (not just saved by user) including the original.
        #all_revisions = [v for v in Version.objects.get_for_object(self)[0:] if not v.field_dict['customer_status'] == 'draft']
        # Strip out duplicates (only take the most recent of a revision).
        #unique_revisions = collections.OrderedDict({v.field_dict['lodgement_date']:v for v in all_revisions})

        unique_revisions = Version.objects.get_for_object(self).select_related('revision').filter(revision__comment__contains='processing_status')

        return unique_revisions

    def get_full_reversion_history(self):
        """  Get all the revisions for this Proposal.
        """

        revisions = Version.objects.get_for_object(self).select_related('revision')

        return revisions

    def get_documents_for_version(self, version_number):
        # get the datetime the requested version was lodged
        versions = list(Version.objects.get_for_object(self).select_related('revision')\
            .filter(revision__comment__contains='processing_status').get_unique())
        version = versions[version_number]
        #proposal = Proposal(**version)
        version_lodgement_date = version.field_dict['lodgement_date']
        version_documents = ProposalDocument.objects.filter(proposal=self, uploaded_date__lte=version_lodgement_date)\
            .order_by('input_name', 'uploaded_date')
        return version_documents, version_lodgement_date

    def get_document_differences(self, newer_version, older_version, differences_only):
        newer_version_documents, newer_version_lodgement_date = self.get_documents_for_version(newer_version)
        older_version_documents, older_version_lodgement_date = self.get_documents_for_version(older_version)

        newer_documents_list = []
        input_name = ''
        for document in newer_version_documents:
            if not document.hidden:
                #logger.debug('newer_document.input_name = ' + str(document.input_name))
                #logger.debug('newer_document.name = ' + str(document.name))
                #logger.debug('newer_document.hidden = ' + str(document.hidden) + '\n\n')
                if input_name != document.input_name:
                    input_name = document.input_name
                    input_item = {input_name:[]}
                    newer_documents_list.append(input_item)
                    #logger.debug('adding ' + str(input_item))

                #logger.debug('input_name ' + str(input_name))

                input_item[input_name] += [{document.name:document._file.url}]

        #return newer_documents_list

        older_documents_list = []
        input_name = ''
        for document in older_version_documents:
            # We need to get the state (hidden or not) of the document as it was in the older version
            older_document_version = Version.objects.get_for_object(document)\
            .select_related('revision').filter(revision__date_created__lte=older_version_lodgement_date).order_by('-revision__date_created').first()
            older_document = ProposalDocument(**older_document_version.field_dict)
            if not older_document.hidden:
                #logger.debug('older_document.input_name = ' + str(older_document.input_name))
                #logger.debug('older_document.name = ' + str(older_document.name))
                #logger.debug('older_document.hidden = ' + str(older_document.hidden) + '\n\n')
                if input_name != older_document.input_name:
                    input_name = older_document.input_name
                    input_item = {input_name:[]}
                    older_documents_list.append(input_item)
                    #logger.debug('adding ' + str(input_item))

                #logger.debug('input_name ' + str(input_name))

                input_item[input_name] += [{older_document.name:older_document._file.url}]

        #return older_documents_list

        #logger.debug('older_documents_list = ' + str(older_documents_list))


        #logger.debug('\n\nolder_documents_list = ' + str(newer_documents_list))

        #logger.debug('newer_version_documents length = ' + str(len(list(newer_version_documents))))
        #logger.debug('older_version_documents length = ' + str(len(list(older_version_documents))))

        differences = DeepDiff(newer_documents_list, older_documents_list, ignore_order=True)

        #logger.debug('\n\ndifferences = ' + str(differences))

        #if differences_only:
        differences_list = []
        for difference in differences.items():
            if "values_changed" in difference:
                #logger.debug('\n\n values_changed -----------------> ')
                for key, value in difference[1].items():
                    key_suffix = key.split('\'')[-1]
                    #logger.debug('\n\n key = ' + str(key))
                    #logger.debug('\n\n values = ' + str(value))
                    section = key.split('\'')[-2]
                    # Add the old value document to the list as an remove
                    old_value_dict = value['old_value']
                    #logger.debug('\n\n old_value_dict = ' + str(old_value_dict))
                    operation = '-'
                    for item in old_value_dict:
                        file_name = item
                        file_path = old_value_dict[item]
                        #logger.debug('\n\n item = ' + str(item))
                        #logger.debug('\n\n file_path = ' + str(file_path))
                        differences_list.append({section:(operation, file_name, file_path)})
                    # Add the new value document to the list as an add
                    new_value_dict = value['new_value']
                    #logger.debug('\n\n new_value_dict = ' + str(new_value_dict))
                    operation = '+'
                    for item in new_value_dict:
                        file_name = item
                        file_path = new_value_dict[item]
                        #logger.debug('\n\n item = ' + str(item))
                        #logger.debug('\n\n file_path = ' + str(file_path))
                        differences_list.append({section:(operation, file_name, file_path)})

                    #differences_list.append({section:'-{},+{}'.format(old_value, new_value),})

            #logger.debug(f'difference = {difference}')
            if "iterable_item_removed" in difference:
                #logger.debug('\n\n iterable_item_removed -----------------> ')
                operation = '-'
                for item in difference[1]:
                    document = difference[1][item]
                    for x in document:
                        #logger.debug('\n\n x = ' + x)

                        section = item.split('\'')[-2]

                        #file = document[section][0]
                        for key, value in document.items():

                            file_name = key
                            file_path = document[key]
                            differences_list.append({section:(operation, file_name, file_path)})

            if "iterable_item_added" in difference:
                #logger.debug('\n\n iterable_item_added -----------------> ')
                operation = '+'
                for item in difference[1]:
                    #logger.debug('\n\n item = ' + str(item))
                    document = difference[1][item]
                    for x in document:
                        section = item.split('\'')[-2]
                        #logger.debug('\n\n section = ' + str(section))
                        #logger.debug('\n\n document = ' + str(document))

                        section = item.split('\'')[-2]

                        #file = document[section][0]
                        for key, value in document.items():

                            file_name = key
                            file_path = document[key]
                            differences_list.append({section:(operation, file_name, file_path)})

        return differences_list
        

    def __assessor_group(self):
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryAssessorGroup.objects.first()
            if group:
                return group
        # TODO get list of assessor groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalAssessorGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalAssessorGroup.DoesNotExist:
                pass
        default_group = ProposalAssessorGroup.objects.get(default=True)

        return default_group


    def __approver_group(self):
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryApproverGroup.objects.first()
            if group:
                return group
        # TODO get list of approver groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalApproverGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalApproverGroup.DoesNotExist:
                pass
        default_group = ProposalApproverGroup.objects.get(default=True)

        return default_group

    def __check_proposal_filled_out(self):
        if not self.data:
            raise exceptions.ProposalNotComplete()
        missing_fields = []
        required_fields = {
            'region':'Region/District',
        #    'title': 'Title',
        #    'activity': 'Activity'
        }
        for k,v in required_fields.items():
            val = getattr(self,k)
            if not val:
                missing_fields.append(v)
        return missing_fields

    @property
    def assessor_recipients(self):
        recipients = []
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryAssessorGroup.objects.first()
            if group:
                return group.members_email
        # Proposal logic
        try:
            recipients = ProposalAssessorGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalAssessorGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients

    @property
    def approver_recipients(self):
        recipients = []
        # Alternative logic for Apiary applications
        if self.apiary_group_application_type:
            group = ApiaryApproverGroup.objects.first()
            if group:
                return group.members_email
        # Proposal logic
        try:
            recipients = ProposalApproverGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalApproverGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients

    @property
    def hasAmendmentRequest(self):
        qs = self.amendment_requests
        qs = qs.filter(status = 'requested')
        if qs:
            return True
        return False


    def referral_email_list(self,user):
        qs=self.referrals.all()
        email_list=[]
        if self.assigned_officer:
            email_list.append(self.assigned_officer.email)
        else:
            email_list.append(user.email)
        if qs:
            for r in qs:
                email_list.append(r.referral.email)
        separator=','
        email_list_string=separator.join(email_list)
        return email_list_string



    def can_assess(self,user):
        if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements':
            if self.apiary_group_application_type:
                # Apiary logic
                return self.__assessor_group() in user.apiaryassessorgroup_set.all()
            else:
                # Proposal logic
                return self.__assessor_group() in user.proposalassessorgroup_set.all()
        elif self.processing_status == 'with_approver':
            if self.apiary_group_application_type:
                # Apiary logic
                return self.__approver_group() in user.apiaryapprovergroup_set.all()
            else:
                # Proposal logic
                return self.__approver_group() in user.proposalapprovergroup_set.all()
        else:
            return False

    def assessor_comments_view(self,user):

        if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements' or self.processing_status == 'with_approver' or self.processing_status == 'approved':
            try:
                referral = Referral.objects.get(proposal=self,referral=user)
            except:
                referral = None
            if referral:
                return True
            elif self.__assessor_group() in user.proposalassessorgroup_set.all():
                return True
            elif self.__approver_group() in user.proposalapprovergroup_set.all():
                return True
            else:
                return False
        else:
            return False

    @property   
    def status_without_assessor(self):
        status_without_assessor = ['with_approver','approved','declined','draft', 'with_referral']
        if self.processing_status in status_without_assessor:
            return True
        return False


    def has_assessor_mode(self,user):
        status_without_assessor = ['with_approver','approved','declined','draft']
        if self.processing_status in status_without_assessor:
            return False
        else:
            if self.assigned_officer:
                if self.assigned_officer == user:
                    if self.apiary_group_application_type:
                        # Apiary logic
                        return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                    else:
                        # Proposal logic
                        return self.__assessor_group() in user.proposalassessorgroup_set.all()
                else:
                    return False
            else:
                if self.apiary_group_application_type:
                    # Apiary logic
                    return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                else:
                    # Proposal logic
                    return self.__assessor_group() in user.proposalassessorgroup_set.all()
                
    def draft_assessor_mode(self,user):
        status_without_assessor = ['with_approver','approved','declined','with_assessor', 'discarded']
        draft_status = ['draft']
        if self.processing_status not in draft_status:
            return False
        else:
            if self.assigned_officer:
                if self.assigned_officer == user:
                    if self.apiary_group_application_type:
                        # Apiary logic
                        return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                    else:
                        # Proposal logic
                        return self.__assessor_group() in user.proposalassessorgroup_set.all()
                else:
                    return False
            else:
                if self.apiary_group_application_type:
                    # Apiary logic
                    return self.__assessor_group() in user.apiaryassessorgroup_set.all()
                else:
                    # Proposal logic
                    return self.__assessor_group() in user.proposalassessorgroup_set.all()

    def log_user_action(self, action, request):
        return ProposalUserAction.log_action(self, action, request.user)

    def log_metrics(self, when, sqs_response, time_taken, response_cached=False):
        system = sqs_response['system'] if sqs_response and 'system' in sqs_response else 'Unknown'
        request_type = sqs_response['request_type'] if sqs_response and 'request_type' in sqs_response else 'Unknown'
        return SpatialQueryMetrics.objects.create(proposal=self, when=when, system=system, request_type=request_type, sqs_response=sqs_response, time_taken=time_taken, response_cached=response_cached)

    def validate_map_files(self, request):
        import geopandas as gpd
        try:
            shp_file_qs=self.map_documents.filter(name__endswith='.shp')
            MAX_NO_POLYGONS=15
            try:
                from disturbance.components.main.models import GlobalSettings
                gs_key=GlobalSettings.objects.get(key=GlobalSettings.MAX_NO_POLYGONS)
                if gs_key and gs_key.value.isdigit():
                   MAX_NO_POLYGONS=int(gs_key.value) 
            except:
                MAX_NO_POLYGONS=15
            #TODO : validate shapefile and all the other related filese are present
            if shp_file_qs:
                shp_file_obj= shp_file_qs.last()
                # shp_file=shp_file_obj._file
                # shp= gpd.read_file(shp_file_obj.path)
                # shp_transform=shp.to_crs(crs=4326)
                # shp_json=shp_transform.to_json()

                #result = subprocess.run(f'{OGR2OGR} -f GeoJSON -lco COORDINATE_PRECISION={GEOM_PRECISION} /vsistdout/ {shp_file_obj.path}', capture_output=True, text=True, check=True, shell=True)
                #result = subprocess.run(f'{OGR2OGR} -f GeoJSON /vsistdout/ {shp_file_obj.path}', capture_output=True, text=True, check=True, shell=True)
                result = subprocess.run(f'{OGR2OGR} -t_srs EPSG:4326 -f GeoJSON /vsistdout/ {shp_file_obj.path}', capture_output=True, text=True, check=True, shell=True)
                shp_json = json.loads(result.stdout)
                shapefile_json=None
                if type(shp_json)==str:
                    shapefile_json=json.loads(shp_json)
                else:
                    shapefile_json=shp_json

                #The features id has to be unique for each shapefile_json
                if shapefile_json and 'features' in shapefile_json:
                    num_features = len(shapefile_json['features'])
                    if num_features > 0 and num_features <= MAX_NO_POLYGONS:
                        if 'id' in shapefile_json['features'][0]:
                            shapefile_json['features'][0]['id']=self.id
                        self.shapefile_json=shapefile_json
                        self.prefill_requested=False
                    else:
                        msg = 'no features found in shapefile' if num_features == 0 else f'too many features: {num_features} (max {MAX_NO_POLYGONS})' 
                        raise ValidationError(f'Cannot upload a Shapefile - {msg}')
                else:
                    raise ValidationError('Please upload a valid shapefile')


#                # Explode multi-part geometries into multiple single geometries. 'pgsql2shp' cannot handle multi-part geometries (mix of Polygon and MultiPolygon Geometries)
#                gdf = gpd.GeoDataFrame.from_features(self.shapefile_json)
#                exploded_shapefile = json.loads(gdf.explode(index_parts=True, ignore_index=False).to_json()) 
#                # Set proposal_geom field for shapefile export sql query (from KB)
#                geoms = []         
#                #for ft in self.shapefile_json['features']:
#                for ft in exploded_shapefile['features']:
#                    geom = GEOSGeometry(json.dumps(ft['geometry']))        
#                    geoms.append(geom)       
#                self.shapefile_geom = MultiPolygon(geoms)

                # Explode multi-part geometries into multiple single geometries.
                self.set_shapefile_geom()

                self.save(version_comment='New Shapefile JSON saved.')                
            else:
                raise ValidationError('Please upload a valid shapefile') 
        except Exception as e:
            #Delete the uploaded shapefile as it is invalid
            map_docs=self.map_documents.all()
            if map_docs:
                for document in map_docs:
                    if document._file and os.path.isfile(document._file.path) and document.can_delete:
                        os.remove(document._file.path)
                        document.delete()
                    else:
                        document.hidden=True
                        document.save()
            self.shapefile_json=None
            self.shapefile_exp_json=None
            self.save(version_comment='Shapefile json cleared as invalid shapefile uploaded.')
            raise ValidationError(f'{e}')


    def set_shapefile_geom(self):
        ''' Explode multi-part geometries into multiple single geometries. 
            pgsql2shp cannot handle multi-part geometries (mix of Polygon and MultiPolygon Geometries)
            
            Test:
                pgsql2shp -f DAS_WA -h localhost -u <username> -p 5432 -P <passwd> db_name  'select p.lodgement_number AS app_no, p.shapefile_geom AS geometry from disturbance_proposal p where p.id=<p.id>;'
        '''
        import geopandas as gpd
        if self.shapefile_geom:
            return self.shapefile_geom

        geoms = []         
        try:
            gdf = gpd.GeoDataFrame.from_features(self.shapefile_json)
            exploded_shapefile = json.loads(gdf.explode(index_parts=True, ignore_index=False).to_json()) 
            #exploded_shapefile = self.shapefile_json
            #for ft in self.shapefile_json['features']:
            for ft in exploded_shapefile['features']:
                geom = GEOSGeometry(json.dumps(ft['geometry']))        
                geoms.append(geom)       

            self.shapefile_geom = MultiPolygon(geoms)
        except Exception as e:
            logger.error(geoms)
            raise ValidationError(f'Please upload a valid shapefile. \n{e}')


    def get_lonlat(self):
       ''' Get longitude and latitude from centroid of polygon
           Returns Point(x,y)
       ''' 
       import geopandas as gpd 
       try:
           if self.shapefile_json is None:
               logger.warn(f'No shapefile found. Upload shapefile to the Proposal first')
               return
           gdf = gpd.read_file(json.dumps(self.shapefile_json), driver='GeoJSON')
           return gdf.centroid[0]
       except Exception as e:
           logger.error(f'Error getting lon/lat from shapefile {str(e)}\n' + str(traceback.print_exc()))

       return None
        
    def prefill_proposal(self, request):
        import geopandas as gpd 
        try:
            #TODO : validate shapefile and all the other related filese are present
            if self.shapefile_json:
                print('yes')
                
            else:
                raise ValidationError('Please upload a valid shapefile') 
        except:
            raise ValidationError('Please upload a valid shapefile')

    def get_history_add_info_assessor(self):
        try:
            history={}
            if self.add_info_assessor:
                for key in self.add_info_assessor:
                    if self.history_add_info_assessor and key in self.history_add_info_assessor:
                        new_value=''
                        if type(self.add_info_assessor[key])==str:
                            new_value=self.history_add_info_assessor[key]+'/r/n/n'+self.add_info_assessor[key]
                        else:
                            new_value=self.history_add_info_assessor[key]
                        history[key]=new_value
                    else:
                        history[key]=self.add_info_assessor[key]
            return history
        except:
            raise
    
    def get_layers_info():
        import geopandas as gpd

        qs=DASMapLayer.objects.filter(layer_url__isnull=False)
        gdf = gpd.GeoDataFrame()
        if qs:
            for layer in qs:
                if layer.layer_url:
                    if 'public' not in layer.layer_url:
                        response = requests.get('{}'.format(layer.layer_url), auth=(settings.LEDGER_USER,settings.LEDGER_PASS), verify=None)
                    else:
                        response=requests.get('{}'.format(layer.layer_url), verify=None)
                    layer_gdf = gpd.GeoDataFrame.from_features(response.json()["features"])
                    gdf = gdf.append(layer_gdf)

        output_file='/data/data/projects/disturbance_das_gis/media/proposals/1734/documents/map_docs/output/layers.geojson'
        gdf.to_file(output_file, driver="GeoJSON")

        #res = requests.get('{}'.format(self.url), auth=(settings.LEDGER_USER,settings.LEDGER_PASS), verify=None)


    def combine_shapefile_json():
        import geopandas as gpd

        qs=Proposal.objects.filter(shapefile_json__isnull=False)
        gdf = gpd.GeoDataFrame()
        combined_features=[]
        if qs:
            for proposal in qs:
                if proposal.shapefile_json:
                    features=proposal.shapefile_json.get('features',[])
                    combined_features.extend(features)
        combined_geojson={
            'type': 'FeatureCollection',
            'features': combined_features
        }
                    # if 'public' not in proposal.layer_url:
                    #     response = requests.get('{}'.format(layer.layer_url), auth=(settings.LEDGER_USER,settings.LEDGER_PASS), verify=None)
                    # else:
                    #     response=requests.get('{}'.format(layer.layer_url), verify=None)
                    # layer_gdf = gpd.GeoDataFrame.from_features(re sponse.json()["features"])
                    # gdf = gdf.append(layer_gdf)

        output_file_path='/data/data/projects/disturbance_das_gis/media/proposals/1734/documents/map_docs/output/proposal.geojson'
        with open(output_file_path, 'w') as output_file:
            json.dump(combined_geojson, output_file)
        #gdf.to_file(output_file, driver="GeoJSON")


    def submit(self,request,viewset):
        from disturbance.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            if not self.shapefile_json:
                raise ValidationError('Error: Must first upload a shapefile')
            
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                if self.application_type.name != ApplicationType.APIARY:
                    # Check if the special fields have been completed
                    missing_fields = self.__check_proposal_filled_out()
                    if missing_fields:
                        error_text = 'The proposal has these missing fields, {}'.format(','.join(missing_fields))
                        raise exceptions.ProposalMissingFields(detail=error_text)
                self.submitter = request.user
                #self.lodgement_date = datetime.datetime.strptime(timezone.now().strftime('%Y-%m-%d'),'%Y-%m-%d').date()
                self.lodgement_date = timezone.now()
                if (self.amendment_requests):
                    qs = self.amendment_requests.filter(status = "requested")
                    if (qs):
                        for q in qs:
                            q.status = 'amended'
                            q.save()

                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.lodgement_number), request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.lodgement_number), request)

                ret1 = send_submit_email_notification(request, self)
                ret2 = send_external_submit_email_notification(request, self)

                if ret1 and ret2:
                    self.processing_status = Proposal.PROCESSING_STATUS_WITH_ASSESSOR
                    self.customer_status = Proposal.CUSTOMER_STATUS_WITH_ASSESSOR
                    self.documents.all().update(can_delete=False)
                    self.save()
                else:
                    raise ValidationError('An error occurred while submitting proposal (Submit email notifications failed)')
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')
        return self

    def update(self,request,viewset):
        from disturbance.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                self.save()
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')


    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                referral_email = referral_email.lower()
                if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral':
                    self.processing_status = 'with_referral'
                    self.save()
                    referral = None

                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email)
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    try:
                        Referral.objects.get(referral=user,proposal=self)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self,
                            referral=user,
                            sent_by=request.user,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id, self.lodgement_number, '{}({})'.format(user.get_full_name(), user.email)), request)
                    # Create a log entry for the organisation
                    if self.applicant:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id, self.lodgement_number, '{}({})'.format(user.get_full_name(), user.email)), request)
                    # send email
                    send_referral_email_notification(referral,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    def assign_officer(self,request,officer):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if not self.can_assess(officer):
                    raise ValidationError('The selected person is not authorised to be assigned to this proposal')
                if self.processing_status == 'with_approver':
                    if officer != self.assigned_approver:
                        self.assigned_approver = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.lodgement_number, '{}({})'.format(officer.get_full_name(),officer.email)), request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.lodgement_number, '{}({})'.format(officer.get_full_name(), officer.email)), request)
                else:
                    if officer != self.assigned_officer:
                        self.assigned_officer = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.lodgement_number, '{}({})'.format(officer.get_full_name(), officer.email)), request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.lodgement_number, '{}({})'.format(officer.get_full_name(), officer.email)), request)
            except:
                raise

    def assing_approval_level_document(self, request):
        with transaction.atomic():
            try:
                approval_level_document = request.data['approval_level_document']
                if approval_level_document != 'null':
                    try:
                        document = self.documents.get(input_name=str(approval_level_document))
                    except ProposalDocument.DoesNotExist:
                        document = self.documents.get_or_create(input_name=str(approval_level_document), name=str(approval_level_document))[0]
                    document.name = str(approval_level_document)
                    # commenting out below tow lines - we want to retain all past attachments - reversion can use them
                    #if document._file and os.path.isfile(document._file.path):
                    #    os.remove(document._file.path)
                    document._file = approval_level_document
                    document.save()
                    d=ProposalDocument.objects.get(id=document.id)
                    self.approval_level_document = d
                    comment = 'Approval Level Document Added: {}'.format(document.name)
                else:
                    self.approval_level_document = None
                    comment = 'Approval Level Document Deleted: {}'.format(request.data['approval_level_document_name'])
                #self.save()
                self.save(version_comment=comment) # to allow revision to be added to reversion history
                self.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.lodgement_number), request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.lodgement_number), request)
                return self
            except:
                raise

    def save_approval_level_comment(self, request):
        with transaction.atomic():
            try:
                approval_level_comment = request.data['approval_level_comment']
                self.approval_level_comment=approval_level_comment
                self.save()
                self.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_COMMENT.format(self.lodgement_number), request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_COMMENT.format(self.lodgement_number), request)
                return self
            except:
                raise

    def unassign(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status == 'with_approver':
                    if self.assigned_approver:
                        self.assigned_approver = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.lodgement_number), request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.lodgement_number), request)
                else:
                    if self.assigned_officer:
                        self.assigned_officer = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.lodgement_number), request)
                        # Create a log entry for the organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.lodgement_number), request)
            except:
                raise

    def move_to_status(self,request,status, approver_comment):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if status in ['with_assessor','with_assessor_requirements','with_approver']:
            if self.processing_status == 'with_referral' or self.can_user_edit:
                raise ValidationError('You cannot change the current status at this time')
            if self.processing_status != status:
                if self.processing_status =='with_approver':
                    if approver_comment:
                        self.approver_comment = approver_comment
                        self.save()
                        send_proposal_approver_sendback_email_notification(request, self)
                self.processing_status = status
                self.save()

                # Create a log entry for the proposal
                if self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR:
                    self.log_user_action(ProposalUserAction.ACTION_BACK_TO_PROCESSING.format(self.lodgement_number), request)
                elif self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS:
                    self.log_user_action(ProposalUserAction.ACTION_ENTER_REQUIREMENTS.format(self.lodgement_number), request)
        else:
            raise ValidationError('The provided status cannot be found.')

    def reissue_approval(self,request,status):
        with transaction.atomic():
            if not self.processing_status=='approved' :
                raise ValidationError('You cannot change the current status at this time')
            elif self.application_type.name == 'Site Transfer' and self.__approver_group() in request.user.apiaryapprovergroup_set.all():
                # track changes to apiary sites and proposal requirements in save() methods instead
                self.processing_status = status
                #self.self_clone = copy.deepcopy(self)
                #self.self_clone.id = None
                #self.self_clone.save()
                self.save()
                #self.proposal_apiary.self_clone = copy.deepcopy(self.proposal_apiary)
                #self.proposal_apiary.self_clone.id = None
                #self.proposal_apiary.self_clone.save()
                self.proposal_apiary.reissue_originating_approval = False
                self.proposal_apiary.reissue_target_approval = False
                self.proposal_apiary.save()
                self.proposal_apiary.originating_approval.reissued = True
                self.proposal_apiary.originating_approval.save()
                self.proposal_apiary.target_approval.reissued = True
                self.proposal_apiary.target_approval.save()
            elif self.approval and self.approval.can_reissue:
                # Apiary logic in first condition
                if self.apiary_group_application_type and self.__approver_group() in request.user.apiaryapprovergroup_set.all():
                    self.processing_status = status
                    self.save()
                    self.approval.reissued=True
                    self.approval.save()
                    # Create a log entry for the proposal
                    self.log_user_action(ProposalUserAction.ACTION_REISSUE_APPROVAL.format(self.lodgement_number), request)
                elif self.__approver_group() in request.user.proposalapprovergroup_set.all():
                    self.processing_status = status
                    self.reissued=True
                    self.save()
                    self.approval.reissued=True
                    self.approval.save()
                    # Create a log entry for the proposal
                    self.log_user_action(ProposalUserAction.ACTION_REISSUE_APPROVAL.format(self.lodgement_number), request)
                else:
                    raise ValidationError('Cannot reissue Approval')
            else:
                raise ValidationError('Cannot reissue Approval')

    def proposed_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != Proposal.PROCESSING_STATUS_WITH_ASSESSOR:
                    raise ValidationError('You cannot propose to decline if it is not with assessor')

                reason = details.get('reason')
                ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer': request.user, 'reason': reason, 'cc_email': details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.lodgement_number), request)

                send_approver_decline_email_notification(reason, request, self)
            except:
                raise

    def final_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot decline if it is not with approver')

                proposal_decline, success = ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer':request.user,'reason':details.get('reason'),'cc_email':details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                self.processing_status = 'declined'
                self.customer_status = 'declined'
                self.save()
                reason = details.get('reason')

                if hasattr(self, 'proposal_apiary') and self.proposal_apiary:
                    # Update apiary site status
                    self.proposal_apiary.final_decline()

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.lodgement_number), request)
                send_proposal_decline_email_notification(self,request, proposal_decline)
            except:
                raise

    def preview_approval(self,request,details):
        from disturbance.components.approvals.models import PreviewTempApproval
        from disturbance.components.approvals.models import Approval
        with transaction.atomic():
            try:
                if self.processing_status != 'with_approver':
                    raise ValidationError('Licence preview only available when processing status is with_approver. Current status {}'.format(self.processing_status))
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                #if not self.applicant.organisation.postal_address:
                if not self.relevant_applicant_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal. (Applicant: {})'.format(self.relevant_applicant))

                lodgement_number = self.previous_application.approval.lodgement_number if self.proposal_type in ['renewal', 'amendment'] else '' # renewals/amendments keep same licence number
                # Apiary Site Transfer logic
                form_data_str = request.POST.get('formData')
                form_data = json.loads(form_data_str)
                #if isinstance(form_data, list):
                originating_approval_id = form_data.get('originating_approval_id')
                target_approval_id = form_data.get('target_approval_id')
                licence_buffer = None
                if originating_approval_id:
                    preview_approval = Approval.objects.get(id=originating_approval_id)
                    licence_buffer = preview_approval.generate_apiary_site_transfer_doc(
                            request.user,
                            site_transfer_proposal=self,
                            preview=True
                            )
                elif target_approval_id:
                    preview_approval = Approval.objects.get(id=target_approval_id)
                    licence_buffer = preview_approval.generate_apiary_site_transfer_doc(
                            request.user,
                            site_transfer_proposal=self,
                            preview=True
                            )
                # All other logic
                else:
                    preview_approval = PreviewTempApproval.objects.create(
                        current_proposal = self,
                        issue_date = timezone.now(),
                        expiry_date = datetime.datetime.strptime(details.get('due_date'), '%d/%m/%Y').date(),
                        start_date = datetime.datetime.strptime(details.get('start_date'), '%d/%m/%Y').date(),
                        #submitter = self.submitter,
                        #org_applicant = self.applicant if isinstance(self.applicant, Organisation) else None,
                        #proxy_applicant = self.applicant if isinstance(self.applicant, EmailUser) else None,
                        applicant = self.applicant,
                        proxy_applicant = self.proxy_applicant,
                        lodgement_number = lodgement_number,
                        apiary_approval = self.apiary_group_application_type,
                    )

                    # Generate the preview document - get the value of the BytesIO buffer
                    licence_buffer = preview_approval.generate_doc(request.user, preview=True)

                    # clean temp preview licence object
                    transaction.set_rollback(True)

                return licence_buffer
            except:
                raise

    def proposed_approval(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor_requirements':
                    raise ValidationError('You cannot propose for approval if it is not with assessor for requirements')
                # Do not accept new start and expiry dates for Apiary group applications with a licence, unless the licence has been reissued
                start_date = details.get('start_date').strftime('%d/%m/%Y') if details.get('start_date') else None
                expiry_date = details.get('expiry_date').strftime('%d/%m/%Y') if details.get('expiry_date') else None
                #cpc_date = details.get('cpc_date').strftime('%d/%m/%Y') if details.get('cpc_date') else None
                #minister_date = details.get('minister_date').strftime('%d/%m/%Y') if details.get('minister_date') else None
                #if self.apiary_group_application_type:
                if self.application_type.name == 'Apiary':
                    if self.approval and (self.approval.reissued or self.proposal_type == 'renewal'):
                        self.proposed_issuance_approval = {
                            'start_date' : start_date,
                            'expiry_date' : expiry_date,
                            'details' : details.get('details'),
                            'cc_email' : details.get('cc_email'),

                            #'cpc_date' : cpc_date,
                            #'minister_date' : minister_date,
#                            'batch_no' : details.get('batch_no'),
#                            'map_ref' : details.get('map_ref'),
#                            'forest_block' : details.get('forest_block'),
#                            'cog' : details.get('cog'),
#                            'roadtrack' : details.get('roadtrack'),
#                            'zone' : details.get('zone'),
#                            'catchment' : details.get('catchment'),
#                            'dra_permit' : details.get('dra_permit'),
                        }
                    elif self.proposed_issuance_approval:
                        self.proposed_issuance_approval = {
                                'start_date' : self.proposed_issuance_approval.get('start_date') if self.proposed_issuance_approval.get('start_date') else details.get('start_date').strftime('%d/%m/%Y'),
                                'expiry_date' : self.proposed_issuance_approval.get('expiry_date') if self.proposed_issuance_approval.get('expiry_date') else details.get('expiry_date').strftime('%d/%m/%Y'),
                                'details' : details.get('details'),
                                'cc_email' : details.get('cc_email'),

                                #'cpc_date' : self.proposed_issuance_approval.get('cpc_date') if self.proposed_issuance_approval.get('cpc_date') else details.get('cpc_date').strftime('%d/%m/%Y'),
                                #'minister_date' : self.proposed_issuance_approval.get('minister_date') if self.proposed_issuance_approval.get('minister_date') else details.get('minister_date').strftime('%d/%m/%Y'),
#                                'batch_no' : details.get('batch_no'),
#                                'map_ref' : details.get('map_ref'),
#                                'forest_block' : details.get('forest_block'),
#                                'cog' : details.get('cog'),
#                                'roadtrack' : details.get('roadtrack'),
#                                'zone' : details.get('zone'),
#                                'catchment' : details.get('catchment'),
#                                'dra_permit' : details.get('dra_permit'),
                        }
                    else:
                        self.proposed_issuance_approval = {
                                'start_date' : start_date,
                                'expiry_date' : expiry_date,
                                'details' : details.get('details'),
                                'cc_email' : details.get('cc_email'),

                                #'cpc_date' : cpc_date,
                                #'minister_date' : minister_date,
#                                'batch_no' : details.get('batch_no'),
#                                'map_ref' : details.get('map_ref'),
#                                'forest_block' : details.get('forest_block'),
#                                'cog' : details.get('cog'),
#                                'roadtrack' : details.get('roadtrack'),
#                                'zone' : details.get('zone'),
#                                'catchment' : details.get('catchment'),
#                                'dra_permit' : details.get('dra_permit'),
                        }
                # non-apiary Proposals & Apiary Site Transfers
                else:
                    self.proposed_issuance_approval = {
                            'start_date' : start_date,
                            'expiry_date' : expiry_date,
                            'details' : details.get('details'),
                            'cc_email' : details.get('cc_email'),
                            'confirmation': details.get('confirmation'),

                            #'cpc_date' : cpc_date,
                            #'minister_date' : minister_date,
#                            'batch_no' : details.get('batch_no'),
#                            'map_ref' : details.get('map_ref'),
#                            'forest_block' : details.get('forest_block'),
#                            'cog' : details.get('cog'),
#                            'roadtrack' : details.get('roadtrack'),
#                            'zone' : details.get('zone'),
#                            'catchment' : details.get('catchment'),
#                            'dra_permit' : details.get('dra_permit'),
                    }

                self.proposed_decline_status = False
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                #self.assigned_officer = None

                apiary_sites = request.data.get('apiary_sites', None)
                apiary_sites_list = []
                if apiary_sites:
                    # When new apiary proposal
                    if self.application_type.name == ApplicationType.APIARY:
                        for apiary_site in apiary_sites:
                            my_site = ApiarySite.objects.get(id=apiary_site['id'])
                            # my_site.workflow_selected_status = apiary_site['checked']
                            self.proposal_apiary.set_workflow_selected_status(my_site, apiary_site.get('checked'))
                            if apiary_site.get('checked'):
                                apiary_sites_list.append(apiary_site.get('id'))
                                relation = self.proposal_apiary.get_relation(my_site)
                                from disturbance.components.proposals.serializers_apiary import ApiarySiteOnProposalProcessedLicensedSiteSaveSerializer
                                #serializer = ApiarySiteOnProposalProcessedLicensedSiteSaveSerializer(relation, data={'licensed_site': apiary_site['properties'].get('licensed_site')})
                                serializer = ApiarySiteOnProposalProcessedLicensedSiteSaveSerializer(relation, data=apiary_site['properties'])
                                serializer.is_valid(raise_exception=True)
                                serializer.save()
                            # my_site.save()

                            if apiary_site.get('checked') and 'coordinates_moved' in apiary_site:
                                relation = self.proposal_apiary.get_relation(my_site)
                                prev_coordinates = relation.wkb_geometry_processed.get_coords()

                                # Update coordinate (Assessor and Approver can move the proposed site location)
                                geom_str = GEOSGeometry('POINT(' + str(apiary_site['coordinates_moved']['lng']) + ' ' + str(apiary_site['coordinates_moved']['lat']) + ')', srid=4326)
                                # from disturbance.components.proposals.serializers_apiary import ApiarySiteSavePointPendingSerializer
                                # serializer = ApiarySiteSavePointPendingSerializer(my_site, data={'wkb_geometry_pending': geom_str}, context={'validate_distance': True})
                                from disturbance.components.proposals.serializers_apiary import ApiarySiteOnProposalProcessedGeometrySaveSerializer
                                serializer = ApiarySiteOnProposalProcessedGeometrySaveSerializer(relation, data={'wkb_geometry_processed': geom_str, 'licensed_site': apiary_site['properties'].get('licensed_site')})
                                serializer.is_valid(raise_exception=True)
                                serializer.save()

                                # Log it
                                self.log_user_action(ProposalUserAction.APIARY_SITE_MOVED.format(apiary_site['id'], prev_coordinates, (apiary_site['coordinates_moved']['lng'], apiary_site['coordinates_moved']['lat'])), request)

                    # Site transfer
                    elif self.application_type.name == ApplicationType.SITE_TRANSFER:
                        for apiary_site in apiary_sites:
                            transfer_site = SiteTransferApiarySite.objects.get(
                                    proposal_apiary=self.proposal_apiary,
                                    apiary_site_on_approval__apiary_site__id=apiary_site.get('id'),
                                    #apiary_site_on_approval__licensed_site=apiary_site.get('properties')['licensed_site'],
                                    )
                            transfer_site.internal_selected = apiary_site.get('checked') if transfer_site.customer_selected else False
                            if apiary_site.get('checked'):
                                apiary_sites_list.append(apiary_site.get('id'))
                            transfer_site.save()

                            asoa = transfer_site.apiary_site_on_approval
                            asoa.licensed_site = apiary_site.get('properties')['licensed_site']
                            asoa.save()

                self.save()
                # Log proposal action
                if self.apiary_group_application_type:
                    if self.application_type and self.application_type.name == ApplicationType.SITE_TRANSFER:
                        target_approval_lodgement_number = (self.proposal_apiary.target_approval.lodgement_number if 
                                self.proposal_apiary.target_approval else '')
                        self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APIARY_APPROVAL_SITE_TRANSFER.format(
                            self.lodgement_number,
                            self.proposal_apiary.originating_approval.lodgement_number,
                            target_approval_lodgement_number,
                            str(apiary_sites_list).lstrip('[').rstrip(']')
                        ), request)
                    else:
                        self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APIARY_APPROVAL.format(
                            self.lodgement_number,
                            self.proposed_issuance_approval.get('start_date'),
                            self.proposed_issuance_approval.get('expiry_date'),
                            str(apiary_sites_list).lstrip('[').rstrip(']')
                            ), request)
                else:
                    self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    if self.apiary_group_application_type:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_APIARY_APPROVAL.format(
                            self.lodgement_number,
                            self.proposed_issuance_approval.get('start_date'),
                            self.proposed_issuance_approval.get('expiry_date'),
                            str(apiary_sites_list).lstrip('[').rstrip(']')
                            #', '.join(apiary_sites_list)
                            ),request)
                    else:
                        self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.lodgement_number), request)

                send_approver_approve_email_notification(request, self)
            except:
                raise

    def final_approval_temp_use(self, request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != Proposal.PROCESSING_STATUS_WITH_ASSESSOR:
                    # For temporary Use Application, assessor approves it
                    raise ValidationError('You cannot approve the proposal if it is not with an assessor')

                self.proposed_decline_status = False
                self.processing_status = Proposal.PROCESSING_STATUS_APPROVED
                self.customer_status = 'approved'

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.lodgement_number), request)

                # TODO: Email?

                self.save()

            except:
                raise

    def final_decline_temp_use(self, request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != Proposal.PROCESSING_STATUS_WITH_ASSESSOR:
                    # For temporary Use Application, assessor approves it
                    raise ValidationError('You cannot approve the proposal if it is not with an assessor')

                # TODO: Is it required to show a modal and get the reason of the delinature or so?  If so, we need following 4 lines
                # proposal_decline, success = ProposalDeclinedDetails.objects.update_or_create(
                #     proposal = self,
                #     defaults={'officer':request.user,'reason':details.get('reason'),'cc_email':details.get('cc_email',None)}
                # )
                self.proposed_decline_status = True
                self.processing_status = 'declined'
                self.customer_status = 'declined'
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.lodgement_number), request)

                # TODO: Email?
                # send_proposal_decline_email_notification(self,request, proposal_decline)

            except:
                raise

    def final_approval(self,request,details):
        from disturbance.components.approvals.models import Approval
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot issue the approval if it is not with an approver')
                #if not self.applicant.organisation.postal_address:
                if not self.relevant_applicant_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal. (Applicant: {})'.format(self.relevant_applicant))

                self.proposed_issuance_approval = {
                    'start_date' : details.get('start_date').strftime('%d/%m/%Y'),
                    'expiry_date' : details.get('expiry_date').strftime('%d/%m/%Y'),
                    'details': details.get('details'),
                    'cc_email':details.get('cc_email'),
                    'confirmation': details.get('confirmation')

#                    'cpc_date' : details.get('cpc_date').strftime('%d/%m/%Y'),
#                    'minister_date' : details.get('minister_date').strftime('%d/%m/%Y'),
#                    'batch_no': details.get('batch_no'),
#                    'map_ref' : details.get('map_ref'),
#                    'forest_block' : details.get('forest_block'),
#                    'cog' : details.get('cog'),
#                    'roadtrack' : details.get('roadtrack'),
#                    'zone' : details.get('zone'),
#                    'catchment' : details.get('catchment'),
#                    'dra_permit' : details.get('dra_permit'),

                }
                self.proposed_decline_status = False
                self.processing_status = 'approved'
                self.customer_status = 'approved'
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.lodgement_number), request)
                # Log entry for organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.lodgement_number), request)

                if self.processing_status == 'approved':
                    # TODO if it is an ammendment proposal then check appropriately
                    checking_proposal = self
                    if self.proposal_type == 'renewal':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'proxy_applicant' : self.proxy_applicant,
                                    'lodgement_number': previous_approval.lodgement_number,
                                    'apiary_approval': self.apiary_group_application_type,
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()

                    elif self.proposal_type == 'amendment':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'proxy_applicant' : self.proxy_applicant,
                                    'lodgement_number': previous_approval.lodgement_number,
                                    'apiary_approval': self.apiary_group_application_type,
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()
                    else:
                        approval,created = Approval.objects.update_or_create(
                            current_proposal = checking_proposal,
                            defaults = {
                                #'activity' : self.activity,
                                #'region' : self.region.name,
                                #'tenure' : self.tenure.name,
                                #'title' : self.title,
                                'issue_date' : timezone.now(),
                                'expiry_date' : details.get('expiry_date'),
                                'start_date' : details.get('start_date'),
                                'applicant' : self.applicant,
                                'proxy_applicant' : self.proxy_applicant,
                                'apiary_approval': self.apiary_group_application_type,
                                #'extracted_fields' = JSONField(blank=True, null=True)
                            }
                        )
                        #print approval,approval.id, created


                    # Generate compliances
                    #self.generate_compliances(approval, request)
                    from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                    if created:
                        if self.proposal_type == 'amendment':
                            approval_compliances = Compliance.objects.filter(approval= previous_approval, proposal = self.previous_application, processing_status='future')
                            if approval_compliances:
                                for c in approval_compliances:
                                    c.delete()
                        # Log creation
                        # Generate the document
                        approval.generate_doc(request.user)
                        self.generate_compliances(approval, request)
                        # send the doc and log in approval and org
                    else:
                        #approval.replaced_by = request.user
                        #approval.replaced_by = self.approval
                        # Generate the document
                        approval.generate_doc(request.user)
                        #Delete the future compliances if Approval is reissued and generate the compliances again.
                        approval_compliances = Compliance.objects.filter(approval= approval, proposal = self, processing_status='future')
                        if approval_compliances:
                            for c in approval_compliances:
                                c.delete()
                        self.generate_compliances(approval, request)
                        # Log proposal action
                        self.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.lodgement_number), request)
                        # Log entry for organisation
                        if self.applicant:
                            self.applicant.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.lodgement_number), request)
                    self.approval = approval
                #send Proposal approval email with attachment
                send_proposal_approval_email_notification(self,request)
                self.save(version_comment='Final Approval: {}'.format(self.approval.lodgement_number))
                self.approval.documents.all().update(can_delete=False)

            except:
                raise



    '''def generate_compliances(self,approval):
        from disturbance.components.compliances.models import Compliance
        today = timezone.now().date()
        timedelta = datetime.timedelta

        for req in self.requirements.all():
            if req.recurrence and req.due_date > today:
                current_date = req.due_date
                while current_date < approval.expiry_date:
                    for x in range(req.recurrence_schedule):
                    #Weekly
                        if req.recurrence_pattern == 1:
                            current_date += timedelta(weeks=1)
                    #Monthly
                        elif req.recurrence_pattern == 2:
                            current_date += timedelta(weeks=4)
                            pass
                    #Yearly
                        elif req.recurrence_pattern == 3:
                            current_date += timedelta(days=365)
                    # Create the compliance
                    if current_date <= approval.expiry_date:
                        Compliance.objects.create(
                            proposal=self,
                            due_date=current_date,
                            processing_status='future',
                            approval=approval,
                            requirement=req.requirement,
                        )
                        #TODO add logging for compliance'''


    def generate_compliances(self,approval, request):
        today = timezone.now().date()
        timedelta = datetime.timedelta
        from disturbance.components.compliances.models import Compliance, ComplianceUserAction
        #For amendment type of Proposal, check for copied requirements from previous proposal
        if self.proposal_type == 'amendment':
            try:
                for r in self.requirements.filter(copied_from__isnull=False):
                    cs=[]
                    cs=Compliance.objects.filter(requirement=r.copied_from, proposal=self.previous_application, processing_status='due')
                    if cs:
                        if r.is_deleted == True:
                            for c in cs:
                                c.processing_status='discarded'
                                c.customer_status = 'discarded'
                                c.reminder_sent=True
                                c.post_reminder_sent=True
                                c.save()
                        if r.is_deleted == False:
                            for c in cs:
                                c.proposal= self
                                c.approval=approval
                                c.requirement=r
                                c.save()
            except:
                raise
        #requirement_set= self.requirements.filter(copied_from__isnull=True).exclude(is_deleted=True)
        requirement_set= self.requirements.all().exclude(is_deleted=True)

        #for req in self.requirements.all():
        for req in requirement_set:
            try:
                if req.due_date and req.due_date >= today:
                    current_date = req.due_date
                    #create a first Compliance
                    try:
                        compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                    except Compliance.DoesNotExist:
                        compliance =Compliance.objects.create(
                                    proposal=self,
                                    due_date=current_date,
                                    processing_status='future',
                                    approval=approval,
                                    requirement=req,
                        )
                        compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.lodgement_number), request)
                    if req.recurrence:
                        while current_date < approval.expiry_date:
                            for x in range(req.recurrence_schedule):
                            #Weekly
                                if req.recurrence_pattern == 1:
                                    current_date += timedelta(weeks=1)
                            #Monthly
                                elif req.recurrence_pattern == 2:
                                    current_date += timedelta(weeks=4)
                                    pass
                            #Yearly
                                elif req.recurrence_pattern == 3:
                                    current_date += timedelta(days=365)
                            # Create the compliance
                            if current_date <= approval.expiry_date:
                                try:
                                    compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                                except Compliance.DoesNotExist:
                                    compliance =Compliance.objects.create(
                                                proposal=self,
                                                due_date=current_date,
                                                processing_status='future',
                                                approval=approval,
                                                requirement=req,
                                    )
                                    compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.lodgement_number), request)
            except:
                raise

    def renew_approval(self,request):
        with transaction.atomic():
            previous_proposal = self
            try:
                proposal=Proposal.objects.get(previous_application = previous_proposal)
                if proposal.customer_status=='with_assessor':
                    if not proposal.apiary_group_application_type:
                        raise ValidationError('A renewal or amendment proposal for this approval has already been lodged and is awaiting review.')
                    else:
                        raise ValidationError('A renewal or amendment application for this licence has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                if previous_proposal.apiary_group_application_type:
                    proposal = clone_apiary_proposal_with_status_reset(previous_proposal)
                else:
                    previous_proposal = Proposal.objects.get(id=self.id)
                    proposal = clone_proposal_with_status_reset(previous_proposal)
                    #proposal.schema = ProposalType.objects.first().schema
                    # Commented Below - USE existing proposal_type for consistency - section names can change between ptype's particularly with schema gen tool
                    #ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                    #proposal.schema = ptype.schema

                proposal.proposal_type = 'renewal'
                proposal.submitter = request.user
                proposal.previous_application = self
                if not previous_proposal.apiary_group_application_type:
                    # for Apiary, we copy requirements in the clone method above
                    req=self.requirements.all().exclude(is_deleted=True)
                    from copy import deepcopy
                    if req:
                        for r in req:
                            old_r = deepcopy(r)
                            r.proposal = proposal
                            r.copied_from=None
                            r.copied_for_renewal=True
                            if r.due_date:
                                r.due_date=None
                                r.require_due_date=True
                            r.id = None
                            r.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.lodgement_number), request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.lodgement_number), request)
                #Log entry for approval
                from disturbance.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_RENEW_APPROVAL.format(self.approval.lodgement_number), request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal

    def amend_approval(self,request):
        with transaction.atomic():
            previous_proposal = self
            try:
                amend_conditions = {
                'previous_application': previous_proposal,
                'proposal_type': 'amendment'

                }
                proposal=Proposal.objects.get(**amend_conditions)
                if proposal.customer_status=='with_assessor':
                    raise ValidationError('An amendment proposal for this approval has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                previous_proposal = Proposal.objects.get(id=self.id)
                proposal = clone_proposal_with_status_reset(previous_proposal)
                proposal.proposal_type = 'amendment'
                #proposal.schema = ProposalType.objects.first().schema
                # Commented Below - USE existing proposal_type for consistency - section names can change between ptype's particularly with schema gen tool
                #ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                #proposal.schema = ptype.schema
                proposal.submitter = request.user
                proposal.previous_application = self
                #copy all the requirements from the previous proposal
                #req=self.requirements.all()
                req=self.requirements.all().exclude(is_deleted=True)
                from copy import deepcopy
                if req:
                    for r in req:
                        old_r = deepcopy(r)
                        r.proposal = proposal
                        r.copied_from=old_r
                        r.id = None
                        r.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.lodgement_number), request)
                # Create a log entry for the organisation
                if self.applicant:
                    self.applicant.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.lodgement_number), request)
                #Log entry for approval
                from disturbance.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_AMEND_APPROVAL.format(self.approval.lodgement_number), request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal

    def internal_view_log(self,request):
        self.log_user_action(ProposalUserAction.ACTION_VIEW_PROPOSAL.format(self.lodgement_number), request)
        return self


    def apiary_requirements(self, approval=None):
        if self.application_type.name == ApplicationType.SITE_TRANSFER and approval:
            #return self.requirements.filter(apiary_approval=approval)
            return self.requirements.filter(sitetransfer_approval=approval)
        elif self.application_type.name == ApplicationType.SITE_TRANSFER:
            return self.requirements.filter(sitetransfer_approval=None)
        else:
            return self.requirements.all()


class ProposalLogDocument(Document):
    log_entry = models.ForeignKey('ProposalLogEntry',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_comms_log_filename, storage=private_storage)

    class Meta:
        app_label = 'disturbance'

class ProposalLogEntry(CommunicationsLogEntry):
    proposal = models.ForeignKey(Proposal, related_name='comms_logs')

    class Meta:
        app_label = 'disturbance'

    def save(self, **kwargs):
        # save the application reference if the reference not provided
        if not self.reference:
            self.reference = self.proposal.reference
        super(ProposalLogEntry, self).save(**kwargs)

class AmendmentRequestDocument(Document):
    amendment_request = models.ForeignKey('AmendmentRequest',related_name='amendment_request_documents')
    _file = models.FileField(upload_to=update_amendment_request_doc_filename, max_length=500, storage=private_storage)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(AmendmentRequestDocument, self).delete()

class ProposalRequest(models.Model):
    proposal = models.ForeignKey(Proposal)
    subject = models.CharField(max_length=200, blank=True)
    text = models.TextField(blank=True)
    officer = models.ForeignKey(EmailUser, null=True)

    class Meta:
        app_label = 'disturbance'

class ComplianceRequest(ProposalRequest):
    REASON_CHOICES = (('outstanding', 'There are currently outstanding returns for the previous licence'),
                      ('other', 'Other'))
    reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])

    class Meta:
        app_label = 'disturbance'


class AmendmentReason(models.Model):
    reason = models.CharField('Reason', max_length=125)

    class Meta:
        app_label = 'disturbance'
        verbose_name = "Proposal Amendment Reason" # display name in Admin
        verbose_name_plural = "Proposal Amendment Reasons"

    def __str__(self):
        return self.reason



class AmendmentRequest(ProposalRequest):
    STATUS_CHOICES = (('requested', 'Requested'), ('amended', 'Amended'))
    #REASON_CHOICES = (('insufficient_detail', 'The information provided was insufficient'),
    #                  ('missing_information', 'There was missing information'),
    #                  ('other', 'Other'))
    # try:
    #     # model requires some choices if AmendmentReason does not yet exist or is empty
    #     REASON_CHOICES = list(AmendmentReason.objects.values_list('id', 'reason'))
    #     if not REASON_CHOICES:
    #         REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                           (1, 'There was missing information'),
    #                           (2, 'Other'))
    # except:
    #     REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                       (1, 'There was missing information'),
    #                       (2, 'Other'))


    status = models.CharField('Status', max_length=30, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    #reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])
    reason = models.ForeignKey(AmendmentReason, blank=True, null=True)
    #reason = models.ForeignKey(AmendmentReason)

    class Meta:
        app_label = 'disturbance'

    def generate_amendment(self,request):
        with transaction.atomic():
            try:
                if not self.proposal.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.status == 'requested':
                    proposal = self.proposal
                    if proposal.processing_status != 'draft':
                        proposal.processing_status = 'draft'
                        proposal.customer_status = 'draft'
                        proposal.save()
                        proposal.documents.all().update(can_hide=True)

                    # Create a log entry for the proposal
                    proposal.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS, request)
                    # Create a log entry for the organisation
                    if proposal.applicant:
                        proposal.applicant.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS, request)

                    # send email

                    send_amendment_email_notification(self,request, proposal)

                self.save()
            except:
                raise

    def add_documents(self, request):
        with transaction.atomic():
            try:
                # save the files
                data = json.loads(request.data.get('data'))
                if not data.get('update'):
                    documents_qs = self.amendment_request_documents.filter(input_name='amendment_request_doc', visible=True)
                    documents_qs.delete()
                for idx in range(data['num_files']):
                    _file = request.data.get('file-'+str(idx))
                    document = self.amendment_request_documents.create(_file=_file, name=_file.name)
                    document.input_name = data['input_name']
                    document.can_delete = True
                    document.save()
                # end save documents
                self.save()
            except:
                raise
        return

class Assessment(ProposalRequest):
    STATUS_CHOICES = (('awaiting_assessment', 'Awaiting Assessment'), ('assessed', 'Assessed'),
                      ('assessment_expired', 'Assessment Period Expired'))
    assigned_assessor = models.ForeignKey(EmailUser, blank=True, null=True)
    status = models.CharField('Status', max_length=20, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    date_last_reminded = models.DateField(null=True, blank=True)
    #requirements = models.ManyToManyField('Requirement', through='AssessmentRequirement')
    comment = models.TextField(blank=True)
    purpose = models.TextField(blank=True)

    class Meta:
        app_label = 'disturbance'

class ProposalDeclinedDetails(models.Model):
    proposal = models.OneToOneField(Proposal)
    officer = models.ForeignKey(EmailUser, null=False)
    reason = models.TextField(blank=True)
    cc_email = models.TextField(null=True)

    class Meta:
        app_label = 'disturbance'

@python_2_unicode_compatible
#class ProposalStandardRequirement(models.Model):
class ProposalStandardRequirement(RevisionedMixin):
    SYSTEM_CHOICES = (
            ('disturbance', 'Disturbance'),
            ('apiary', 'Apiary'),
                      )
    system = models.CharField('System', max_length=20, choices=SYSTEM_CHOICES, default=SYSTEM_CHOICES[0][0])
    text = models.TextField()
    code = models.CharField(max_length=10, unique=True)
    obsolete = models.BooleanField(default=False)

    def __str__(self):
        return self.code

    class Meta:
        app_label = 'disturbance'


#class ReferralRecipientGroup(models.Model):
class ApiaryReferralGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, blank=True, null=True, on_delete=models.PROTECT)
    district = ChainedForeignKey(
        District,
        chained_field="region",
        chained_model_field="region",
        show_all=False,
        auto_choose=True,
        sort=True,
        #on_delete=models.PROTECT
    )

    def __str__(self):
        #return 'Referral Recipient Group'
        return self.name

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    @property
    def members_list(self):
            return list(self.members.all().values_list('email', flat=True))

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]


    class Meta:
        app_label = 'disturbance'
        verbose_name = "Apiary Referral Group"
        verbose_name_plural = "Apiary Referral groups"

class ProposalRequirement(OrderedModel):
    #from disturbance.components.approvals.models import Approval
    RECURRENCE_PATTERNS = [(1, 'Weekly'), (2, 'Monthly'), (3, 'Yearly')]
    standard_requirement = models.ForeignKey(ProposalStandardRequirement,null=True,blank=True)
    free_requirement = models.TextField(null=True,blank=True)
    standard = models.BooleanField(default=True)
    proposal = models.ForeignKey(Proposal,related_name='requirements')
    due_date = models.DateField(null=True,blank=True)
    recurrence = models.BooleanField(default=False)
    recurrence_pattern = models.SmallIntegerField(choices=RECURRENCE_PATTERNS,default=1)
    recurrence_schedule = models.IntegerField(null=True,blank=True)
    copied_from = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    copied_for_renewal = models.BooleanField(default=False)
    require_due_date = models.BooleanField(default=False)
    # temporary location during Site Transfer applications - copied to apiary_approval during final_approval()
    sitetransfer_approval = models.ForeignKey('disturbance.Approval',null=True,blank=True, related_name='sitetransferapproval_requirement')
    # permanent location for apiary / site transfer approvals
    apiary_approval = models.ForeignKey('disturbance.Approval',null=True,blank=True, related_name='proposalrequirement_set')
    #order = models.IntegerField(default=1)
    # referral_group is no longer required for Apiary
    referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='apiary_requirement_referral_groups')

    class Meta:
        app_label = 'disturbance'


    @property
    def requirement(self):
        return self.standard_requirement.text if self.standard else self.free_requirement

    def save(self, *args, **kwargs):
        super(ProposalRequirement, self).save(*args,**kwargs)
        # update reissue flags as needed
        if self.proposal and hasattr(self.proposal, 'proposal_apiary') and self.proposal.proposal_apiary and self.proposal.application_type.name == 'Site Transfer':
            #if self.sitetransfer_approval == self.apiary_approval:
                # therefore, we know that the requirement is already attached to the target/originating approval, i.e. is not new
                # now find out whether it is target/originating
            # update relevant reissue flag
            if self.sitetransfer_approval == self.proposal.proposal_apiary.originating_approval:
                self.proposal.proposal_apiary.reissue_originating_approval = True
            elif self.sitetransfer_approval == self.proposal.proposal_apiary.target_approval:
                self.proposal.proposal_apiary.reissue_target_approval = True
            self.proposal.proposal_apiary.save()
            #self.save()

# no longer required for Apiary
#class RequirementDocument(Document):
#    #requirement = models.ForeignKey('ProposalRequirement',related_name='requirement_documents')
#    requirement = models.ForeignKey('ProposalRequirement',related_name='documents')
#    #_file = models.FileField(upload_to=update_requirement_doc_filename, max_length=512)
#    _file = models.FileField(max_length=512)
#    input_name = models.CharField(max_length=255,null=True,blank=True)
#    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
#    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history
#
#    def delete(self):
#        if self.can_delete:
#            return super(RequirementDocument, self).delete()
#
#    class Meta:
#        app_label = 'disturbance'


class ProposalUserAction(UserAction):
    ACTION_CREATE_CUSTOMER_ = "Create customer {}"
    ACTION_CREATE_PROFILE_ = "Create profile {}"
    ACTION_LODGE_APPLICATION = "Lodge proposal {}"
    ACTION_SAVE_APPLICATION = "Save proposal {}"
    ACTION_ASSIGN_TO_ASSESSOR = "Assign proposal {} to {} as the assessor"
    ACTION_UNASSIGN_ASSESSOR = "Unassign assessor from proposal {}"
    ACTION_ASSIGN_TO_APPROVER = "Assign proposal {} to {} as the approver"
    ACTION_UNASSIGN_APPROVER = "Unassign approver from proposal {}"
    ACTION_ACCEPT_ID = "Accept ID"
    ACTION_RESET_ID = "Reset ID"
    ACTION_ID_REQUEST_UPDATE = 'Request ID update'
    ACTION_ACCEPT_CHARACTER = 'Accept character'
    ACTION_RESET_CHARACTER = "Reset character"
    ACTION_ACCEPT_REVIEW = 'Accept review'
    ACTION_RESET_REVIEW = "Reset review"
    ACTION_ID_REQUEST_AMENDMENTS = "Request amendments"
    ACTION_SEND_FOR_ASSESSMENT_TO_ = "Send for assessment to {}"
    ACTION_SEND_ASSESSMENT_REMINDER_TO_ = "Send assessment reminder to {}"
    ACTION_DECLINE = "Decline proposal {}"
    ACTION_ENTER_CONDITIONS = "Enter requirement"
    ACTION_CREATE_CONDITION_ = "Create requirement {}"
    ACTION_ISSUE_APPROVAL_ = "Issue Approval for proposal {}"
    ACTION_ISSUE_APIARY_APPROVAL = "Application {} has been approved with start date {}, expirty date {} for the apiary sites {}"
    ACTION_UPDATE_APPROVAL_ = "Update Approval for proposal {}"
    ACTION_UPDATE_APPROVAL_FOR_PROPOSAL = "Update Approval {} for proposal {}"
    ACTION_EXPIRED_APPROVAL_ = "Expire Approval for proposal {}"
    ACTION_DISCARD_PROPOSAL = "Discard proposal {}"
    ACTION_APPROVAL_LEVEL_DOCUMENT = "Assign Approval level document {}"
    ACTION_APPROVAL_LEVEL_COMMENT = "Save Approval level comment {}"
    ACTION_VIEW_PROPOSAL = "View Proposal {}"
    # Assessors
    ACTION_SAVE_ASSESSMENT_ = "Save assessment {}"
    ACTION_CONCLUDE_ASSESSMENT_ = "Conclude assessment {}"
    ACTION_PROPOSED_APPROVAL = "Proposal {} has been proposed for approval"
    ACTION_PROPOSED_APIARY_APPROVAL = "Proposal {} has been proposed for issue with start date {}, expiry date {} for apiary sites {}"
    ACTION_PROPOSED_APIARY_APPROVAL_SITE_TRANSFER = "Proposal {} has been proposed for issue with originating approval {}, target approval {} and apiary sites {}"
    ACTION_PROPOSED_DECLINE = "Proposal {} has been proposed for decline"
    # Referrals
    ACTION_SEND_REFERRAL_TO = "Send referral {} for proposal {} to {}"
    ACTION_RESEND_REFERRAL_TO = "Resend referral {} for proposal {} to {}"
    ACTION_REMIND_REFERRAL = "Send reminder for referral {} for proposal {} to {}"
    ACTION_ENTER_REQUIREMENTS = "Enter Requirements for proposal {}"
    ACTION_BACK_TO_PROCESSING = "Back to processing for proposal {}"
    RECALL_REFERRAL = "Referral {} for proposal {} has been recalled"
    CONCLUDE_REFERRAL = "Referral {} for proposal {} has been concluded by {}"
    #Approval
    ACTION_REISSUE_APPROVAL = "Reissue approval for proposal {}"
    ACTION_CANCEL_APPROVAL = "Cancel approval for proposal {}"
    ACTION_SUSPEND_APPROVAL = "Suspend approval for proposal {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate approval for proposal {}"
    ACTION_SURRENDER_APPROVAL = "Surrender approval for proposal {}"
    ACTION_RENEW_PROPOSAL = "Create Renewal proposal for proposal {}"
    ACTION_AMEND_PROPOSAL = "Create Amendment proposal for proposal {}"
    # Apiary Actions
    APIARY_ACTION_SEND_REFERRAL_TO = "Send Apiary referral {} for application {} to {}"
    APIARY_ACTION_RESEND_REFERRAL_TO = "Resend Apiary referral {} for application {} to {}"
    APIARY_ACTION_REMIND_REFERRAL = "Send reminder for Apiary referral {} for application {} to {}"
    APIARY_ACTION_ENTER_REQUIREMENTS = "Enter Requirements for application {}"
    APIARY_ACTION_BACK_TO_PROCESSING = "Back to processing for application {}"
    APIARY_RECALL_REFERRAL = "Apiary Referral {} for application {} has been recalled"
    APIARY_CONCLUDE_REFERRAL = "Apiary Referral {} for application {} has been concluded by {}"
    APIARY_ACTION_SAVE_APPLICATION = "Save Apiary application {}"
    APIARY_SITE_MOVED = "Apiary Site {} has been moved from {} to {}"
    APIARY_REFERRAL_ASSIGN_TO_ASSESSOR = "Assign Referral {} of application {} to {} as the assessor"
    APIARY_REFERRAL_UNASSIGN_ASSESSOR = "Unassign assessor from Referral {} of application {}"
    # SQS
    ACTION_PREFILL_PROPOSAL = "Prefill Proposal {}"
    ACTION_REFRESH_PROPOSAL = "Refresh data for Proposal {}"
    ACTION_SEND_PREFILL_REQUEST_TO = "Prefill request for proposal {} sent. (DAS TaskMonitor ID {}, SQS Task ID {}, SQS Queue Position {})"
    ACTION_SEND_PREFILL_COMPLETED_TO = "Prefill for proposal {} completed. (DAS TaskMonitor ID {}, SpatialQueryMetric ID {}, SQS Task ID {})"
    ACTION_SEND_PREFILL_ERROR_TO = "ERROR: Prefill for proposal {}"
    ACTION_SEND_REFRESH_REQUEST_TO = "Refresh request for proposal {} sent. (DAS TaskMonitor ID {}, SQS Task ID {}, SQS Queue Position {})"
    ACTION_SEND_REFRESH_COMPLETED_TO = "Refresh for proposal {} completed. (DAS TaskMonitor ID {}, SpatialQueryMetric ID {}, SQS Task ID {})"
    ACTION_SEND_REFRESH_ERROR_TO = "ERROR: Refresh for proposal {}"
    ACTION_SEND_TEST_SQQ_REQUEST_TO = "Test SQQ request for proposal {} sent. (DAS TaskMonitor ID {}, SQS Task ID {}, SQS Queue Position {})"
    ACTION_SEND_TEST_SQQ_COMPLETED_TO = "Test SQQ for proposal {} completed. (DAS TaskMonitor ID {}, SpatialQueryMetric ID {}, SQS Task ID {})"
    ACTION_SEND_TEST_SQQ_ERROR_TO = "ERROR: TEST SQQ for proposal {}"

    class Meta:
        app_label = 'disturbance'
        ordering = ('-when',)

    @classmethod
    def log_action(cls, proposal, action, user):
        if proposal.apiary_group_application_type:
            action = action.replace('Approval', 'Licence').replace('approval', 'licence').replace('proposal', 'application').replace('Proposal', 'Application')
        return cls.objects.create(
            proposal=proposal,
            who=user,
            what=str(action)
        )

    proposal = models.ForeignKey(Proposal, related_name='action_logs')



class Referral(models.Model):
    SENT_CHOICES = (
        (1,'Sent From Assessor'),
        (2,'Sent From Referral')
    )
    PROCESSING_STATUS_CHOICES = (
                                 ('with_referral', 'Awaiting'),
                                 ('recalled', 'Recalled'),
                                 ('completed', 'Completed'),
                                 )
    lodged_on = models.DateTimeField(auto_now_add=True)
    proposal = models.ForeignKey(Proposal,related_name='referrals')
    sent_by = models.ForeignKey(EmailUser,related_name='disturbance_assessor_referrals')
    referral = models.ForeignKey(EmailUser,null=True,blank=True,related_name='disturbance_referalls')
    linked = models.BooleanField(default=False)
    sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[0][0])
    text = models.TextField(blank=True) #Assessor text
    referral_text = models.TextField(blank=True)


    class Meta:
        app_label = 'disturbance'
        ordering = ('-lodged_on',)

    def __str__(self):
        return 'Proposal {} - Referral {}'.format(self.proposal.id,self.id)

    # Methods
    @property
    def latest_referrals(self):
        return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[:2]

    @property
    def can_be_completed(self):
        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
        qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
        if qs:
            return False
        else:
            return True

    def recall(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'recalled'
            self.save()
            send_referral_recall_email_notification(self, request)
            # TODO Log proposal action
            self.proposal.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id, self.proposal.lodgement_number), request)
            # TODO log organisation action
            self.proposal.applicant.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id, self.proposal.lodgement_number), request)

    def remind(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            # Create a log entry for the proposal
            self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # Create a log entry for the organisation
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # send email
            send_referral_email_notification(self,request,reminder=True)

    def resend(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'with_referral'
            self.proposal.processing_status = 'with_referral'
            self.proposal.save()
            self.sent_from = 1
            self.save()
            # Create a log entry for the proposal
            self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # Create a log entry for the organisation
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            # send email
            send_referral_email_notification(self,request)

    def complete(self,request, referral_comment):
        with transaction.atomic():
            try:
                if request.user != self.referral:
                    raise exceptions.ReferralNotAuthorized()
                self.processing_status = 'completed'
                self.referral_text = referral_comment
                self.save()
                # TODO Log proposal action
                self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                # TODO log organisation action
                self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.lodgement_number,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                send_referral_complete_email_notification(self,request)
            except:
                raise

    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                referral_email = referral_email.lower()
                if self.proposal.processing_status == 'with_referral':
                    if request.user != self.referral:
                        raise exceptions.ReferralNotAuthorized()
                    if self.sent_from != 1:
                        raise exceptions.ReferralCanNotSend()
                    self.proposal.processing_status = 'with_referral'
                    self.proposal.save()
                    referral = None
                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email)
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    qs=Referral.objects.filter(sent_by=user, proposal=self.proposal)
                    if qs:
                        raise ValidationError('You cannot send referral to this user')
                    try:
                        Referral.objects.get(referral=user,proposal=self.proposal)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self.proposal,
                            referral=user,
                            sent_by=request.user,
                            sent_from=2,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    self.proposal.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.lodgement_number,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # Create a log entry for the organisation
                    self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.lodgement_number,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # send email
                    send_referral_email_notification(referral,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    # Properties
    @property
    def region(self):
        return self.proposal.region

    @property
    def activity(self):
        return self.proposal.activity

    @property
    def title(self):
        return self.proposal.title

    @property
    def applicant(self):
        return self.proposal.applicant.name

    @property
    def can_be_processed(self):
        return self.processing_status == 'with_referral'

    def can_assess_referral(self,user):
        return self.processing_status == 'with_referral'

@receiver(pre_delete, sender=Proposal)
def delete_documents(sender, instance, *args, **kwargs):
    for document in instance.documents.all():
        document.delete()

def clone_proposal_with_status_reset(proposal):
        with transaction.atomic():
            try:
                proposal.customer_status = 'draft'
                proposal.processing_status = 'draft'
                proposal.assessor_data = None
                proposal.comment_data = None

                #proposal.id_check_status = 'not_checked'
                #proposal.character_check_status = 'not_checked'
                #proposal.compliance_check_status = 'not_checked'
                #Sproposal.review_status = 'not_reviewed'

                proposal.lodgement_number = ''
                proposal.lodgement_sequence = 0
                proposal.lodgement_date = None

                proposal.assigned_officer = None
                proposal.assigned_approver = None

                proposal.approval = None

                original_proposal_id = proposal.id

                #proposal.previous_application = Proposal.objects.get(id=original_proposal_id)

                proposal.id = None
                proposal.approval_level_document = None
                proposal.reissued=False

                proposal.save(no_revision=True)

                # clone documents
                for proposal_document in ProposalDocument.objects.filter(proposal=original_proposal_id):
                    proposal_document.proposal = proposal
                    proposal_document.id = None
                    proposal_document._file.name = u'proposals/{}/documents/{}'.format(proposal.id, proposal_document.name)
                    proposal_document.can_delete = True
                    proposal_document.save()

                # copy documents on file system and reset can_delete flag
                subprocess.call('cp -pr media/proposals/{} media/proposals/{}'.format(original_proposal_id, proposal.id), shell=True)

                return proposal
            except:
                raise


def clone_apiary_proposal_with_status_reset(original_proposal):
    # called for Apiary renewals
    with transaction.atomic():
        try:
            proposal = copy.deepcopy(original_proposal)
            proposal.id = None
            proposal.application_type = ApplicationType.objects.get(name=ApplicationType.APIARY)

            proposal.save(no_revision=True)
            # create proposal_apiary and associate it with the proposal
            proposal_apiary = ProposalApiary.objects.create(proposal=proposal)
            proposal_apiary.save()

            proposal.customer_status = 'draft'
            proposal.processing_status = 'draft'
            proposal.assessor_data = None
            proposal.comment_data = None
            proposal.lodgement_number = ''
            proposal.lodgement_sequence = 0
            proposal.lodgement_date = None

            proposal.assigned_officer = None
            proposal.assigned_approver = None

            proposal.approval_level_document = None
            # proposal.fee_invoice_reference = None
            proposal.fee_invoice_references = []
            proposal.activity = 'Apiary Renewal'

            proposal.save(no_revision=True)
            # clone requirements - ensure due dates are None
            approval = original_proposal.proposal_apiary.retrieve_approval
            req = approval.proposalrequirement_set.exclude(is_deleted=True)
            if req:
                for r in req:
                    old_r = copy.deepcopy(r)
                    r.proposal = proposal
                    r.apiary_approval = None
                    r.copied_from=old_r
                    r.copied_for_renewal=True
                    if r.due_date:
                        r.due_date=None
                        r.require_due_date=True
                    r.id = None
                    r.save()

            # update apiary_sites with new proposal
            approval.add_apiary_sites_to_proposal_apiary_for_renewal(proposal_apiary)

            # Checklist questions
            for question in ApiaryChecklistQuestion.objects.filter(
                    checklist_type='apiary',
                    checklist_role='applicant'
                    ):
                new_answer = ApiaryChecklistAnswer.objects.create(proposal = proposal.proposal_apiary,
                                                                           question = question)

            return proposal
        except:
            raise

def searchKeyWords(searchWords, searchProposal, searchApproval, searchCompliance, is_internal= True):
    from disturbance.utils import search, search_approval, search_compliance
    from disturbance.components.approvals.models import Approval
    from disturbance.components.compliances.models import Compliance
    qs = []
    if is_internal:
        proposal_list = Proposal.objects.filter(application_type__name='Disturbance').exclude(processing_status__in=[Proposal.PROCESSING_STATUS_DISCARDED, Proposal.PROCESSING_STATUS_DRAFT])
        approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
        compliance_list = Compliance.objects.all()

        #print(proposal_list.count()+approval_list.count()+compliance_list.count())

    if searchWords:

        search_words_regex = "(?:"
        for i in range(0,len(searchWords)):
            search_words_regex = search_words_regex + searchWords[i]
            if i == len(searchWords)-1:
                search_words_regex = search_words_regex + ")"
            else:
                search_words_regex = search_words_regex + "|"

        filter_regex = ".*\".*\":\s\"(\\\\\"|[^\"])*"+search_words_regex+"(\\\\\"|[^\"])*\".*"
        #extract_regex = "(?i)\'*\':\s\'(?:\\\\\'|[^\'])*"+search_words_regex+"(?:\\\\\'|[^\'])*\'" #attempted to further optimise but additional regex had a negligable impact at the cost of the data key
        if searchProposal:
            
            proposal_list = proposal_list.filter(data__iregex=filter_regex)
            for p in proposal_list:
                name = ""
                if p.applicant:
                    name = p.applicant.name
                #TODO consider below code and extract regex: it takes fewer lines and may be slightly(?) faster - but we lose the text dict key value (which is not currently in use, but may later be required)
                #value = re.findall(extract_regex,str(p.data))
                #if len(value):
                #    value = value[-1][3:-1]
                #res = {
                #'number': p.lodgement_number,
                #'id': p.id,
                #'type': 'Proposal',
                #'applicant': p.applicant.name,
                #'text': {"key":"key","value":value},
                #}
                #qs.append(res)
                if p.data:
                    try:
                        results = search(p.data[0], searchWords)
                        final_results = {}
                        if results:
                            for r in results:
                                for key, value in r.items():
                                    final_results.update({'key': key, 'value': value})                           
                            res = {
                                'number': p.lodgement_number,
                                'id': p.id,
                                'type': 'Proposal',
                                'applicant': name,
                                'text': final_results,
                                }
                            qs.append(res)
                    except:
                        raise
        if searchApproval:
            approval_list = approval_list.filter(Q(surrender_details__iregex=filter_regex) | Q(suspension_details__iregex=filter_regex) | Q(cancellation_details__iregex=search_words_regex))
            for a in approval_list:
                try:
                    results = search_approval(a, searchWords)
                    qs.extend(results)
                except:
                    raise
        if searchCompliance:
            compliance_list = compliance_list.filter(Q(text__iregex=search_words_regex) | Q(requirement__free_requirement__iregex=search_words_regex) | Q(requirement__standard_requirement__text__iregex=search_words_regex))
            for c in compliance_list:
                try:
                    results = search_compliance(c, searchWords)
                    qs.extend(results)
                except:
                    raise
    #print(len(qs))
    return qs

def search_reference(reference_number):
    from disturbance.components.approvals.models import Approval
    from disturbance.components.compliances.models import Compliance
    proposal_list = Proposal.objects.all().exclude(processing_status__in=[Proposal.PROCESSING_STATUS_DISCARDED,])
    approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
    compliance_list = Compliance.objects.all().exclude(processing_status__in=['future'])
    record = {}
    try:
        result = proposal_list.get(lodgement_number = reference_number)
        record = {  'id': result.id,
                    'type': 'proposal' }
    except Proposal.DoesNotExist:
        try:
            result = approval_list.get(lodgement_number = reference_number)
            record = {  'id': result.id,
                        'type': 'approval' }
        except Approval.DoesNotExist:
            try:
                for c in compliance_list:
                    if c.reference == reference_number:
                        record = {  'id': c.id,
                                    'type': 'compliance' }
            except:
                raise ValidationError('Record with provided reference number does not exist')
    if record:
        return record
    else:
        raise ValidationError('Record with provided reference number does not exist')

def search_sections(proposal_type_id, section_label,question_id,option_label,is_internal= True, region=None,district=None,activity=None):
    from disturbance.utils import search_section
    #print(application_type_name, section_label,question_label,option_label,is_internal)
    res_qs = []
    if is_internal:
        if(not proposal_type_id or not section_label or not question_id or not option_label):
            raise ValidationError('Some of the mandatory fields are missing')
        proposal_type=ProposalType.objects.get(id=proposal_type_id)
        proposal_type_name=proposal_type.name
        qs = Proposal.objects.filter(application_type__name=proposal_type_name, data__isnull=False).exclude(processing_status__in=[Proposal.PROCESSING_STATUS_DISCARDED, Proposal.PROCESSING_STATUS_DRAFT])

        question=MasterlistQuestion.objects.get(id=question_id)
        filter_conditions={}
        if region:
            filter_conditions['region']=region
        if district:
            filter_conditions['district']=district
        if activity:
            filter_conditions['activity']=activity
        if filter_conditions:
            qs=qs.filter(**filter_conditions)

        paginator = Paginator(qs, settings.QS_PAGINATOR_SIZE) # chunks
        for page_num in paginator.page_range:
            for p in paginator.page(page_num).object_list:
                if p.data:
                    try:
                        results = search_section(p.schema, section_label, question, p.data, option_label)
                        #print(f'{idx}: {p} - {results}')
                        final_results = {}
                        if results:
                            # for r in results:
                            #     for key, value in r.items():
                            #         final_results.update({'key': key, 'value': value})
                            res = {
                                'number': p.lodgement_number,
                                'id': p.id,
                                'type': 'Proposal',
                                'applicant': p.applicant.name,
                                'text': results[0],
                                }
                            res_qs.append(res)
                    except Exception as e:
                        print(e)
                        raise

    return res_qs

def add_properties_to_feature(features, proposal, request):
    new_properties={}
    new_properties['proposal_number']=proposal.lodgement_number
    new_properties['organisation'] = proposal.applicant.name
    new_properties['proposal_title']= proposal.title
    new_properties['proposal_type']=proposal.proposal_type
    new_properties['proposal_url'] = request.build_absolute_uri(reverse('internal-proposal-detail',kwargs={'proposal_pk': proposal.id}))
    

    if proposal.approval:
        qs=Proposal.objects.filter(approval__lodgement_number=proposal.approval.lodgement_number).values_list('lodgement_number', flat=True)
        if qs:
            new_properties['associated_proposals']= [proposal for proposal in qs]
        new_properties['approval_number']=proposal.approval.lodgement_number
        new_properties['approval_issue_date']=proposal.approval.issue_date
        new_properties['approval_start_date']=proposal.approval.start_date
        new_properties['approval_expiry_date']=proposal.approval.expiry_date
        new_properties['approval_status']=proposal.approval.status

    if features:
        for feature in features:
            if 'properties' in feature:
                feature['properties'].update(new_properties)
            else:
                feature['properties']=new_properties
    return features

def get_search_geojson(proposal_lodgement_numbers,request):
    combined_geojson=None
    try:
        import geopandas as gpd

        qs=Proposal.objects.filter(lodgement_number__in=proposal_lodgement_numbers, shapefile_json__isnull=False)
        combined_features=[]
        if qs:
            for proposal in qs:
                if proposal.shapefile_json:
                    gpd_shp=gpd.read_file(json.dumps(proposal.shapefile_json))
                    shp_transform=gpd_shp.to_crs(crs=4326)
                    shp_json=shp_transform.to_json()
                    if type(shp_json)==str:
                        shp_json=json.loads(shp_json)
                    else:
                        shp_json=shp_json
                    features=shp_json.get('features',[])
                    updated_features=add_properties_to_feature(features, proposal, request)
                    combined_features.extend(updated_features)
            combined_geojson={
                'type': 'FeatureCollection',
                'crs': {
                    'type': 'name',
                    'properties': {
                        'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'
                    }
                },
                'features': combined_features
            }
        return combined_geojson
    except:
        raise

from ckeditor.fields import RichTextField
class HelpPage(models.Model):
    HELP_TEXT_EXTERNAL = 1
    HELP_TEXT_INTERNAL = 2
    HELP_TYPE_CHOICES = (
        (HELP_TEXT_EXTERNAL, 'External'),
        (HELP_TEXT_INTERNAL, 'Internal'),
    )

    application_type = models.ForeignKey(ApplicationType)
    content = RichTextField()
    description = models.CharField(max_length=256, blank=True, null=True)
    help_type = models.SmallIntegerField('Help Type', choices=HELP_TYPE_CHOICES, default=HELP_TEXT_EXTERNAL)
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    class Meta:
        app_label = 'disturbance'
        unique_together = ('application_type', 'help_type', 'version')


# --------------------------------------------------------------------------------------
# Apiary Models Start
# --------------------------------------------------------------------------------------

#class ApiaryPermitIssuance(RevisionedMixin):
#    apiary_site_on_proposal = models.ForeignKey('ApiarySiteOnProposal')
#    batch_no = models.CharField(max_length=40, blank=True, null=True)
#    approval_cpc_date = models.DateTimeField(blank=True, null=True)
#    approval_minister_date = models.DateTimeField(blank=True, null=True)
#
#    def __str__(self):
#        return 'id:{}: (apiary_site: {}, proposal_apiary: {})'.format(self.id, self.apiary_site.id, self.proposal_apiary.id)
#
#    class Meta:
#        app_label = 'disturbance'
#        unique_together = ['apiary_site', 'proposal_apiary',]


class ApiarySiteOnProposal(RevisionedMixin):
    apiary_site = models.ForeignKey('ApiarySite',)
    proposal_apiary = models.ForeignKey('ProposalApiary',)
    apiary_site_status_when_submitted = models.CharField(max_length=40, blank=True)
    apiary_site_is_vacant_when_submitted = models.BooleanField(default=False)
    for_renewal = models.BooleanField(default=False)
    site_status = models.CharField(default=SITE_STATUS_DRAFT, max_length=20, db_index=True)
    making_payment = models.BooleanField(default=False)
    workflow_selected_status = models.BooleanField(default=False)  # This field is used only during approval process to select/deselect the site to be approved
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    wkb_geometry_draft = PointField(srid=4326, blank=True, null=True)  # store the coordinates before submit
    wkb_geometry_processed = PointField(srid=4326, blank=True, null=True)  # store approved coordinates
    site_category_draft = models.ForeignKey('SiteCategory', null=True, blank=True, related_name='intermediate_draft')
    site_category_processed = models.ForeignKey('SiteCategory', null=True, blank=True, related_name='intermediate_processed')
    application_fee_paid = models.BooleanField(default=False)  # To avoid overcharging when the proposal is sent back to the customer, we need this flag
    licensed_site = models.BooleanField(default=False)  # used only during approval process, licensed site, have an independent PDF Licence page
    issuance_details = JSONField(blank=True, null=True)

    # permit issuance details
    batch_no = models.CharField(max_length=40, blank=True, null=True)
    approval_cpc_date = models.DateField(blank=True, null=True)
    approval_minister_date = models.DateField(blank=True, null=True)
    map_ref = models.CharField(max_length=40, blank=True, null=True)
    forest_block = models.CharField(max_length=40, blank=True, null=True)
    cog = models.CharField(max_length=40, blank=True, null=True)
    roadtrack = models.CharField(max_length=40, blank=True, null=True)
    zone = models.CharField(max_length=40, blank=True, null=True)
    catchment = models.CharField(max_length=40, blank=True, null=True)
    dra_permit = models.BooleanField(default=False)

    objects = GeoManager()

    def __str__(self):
        return 'id:{}: (apiary_site: {}, proposal_apiary: {})'.format(self.id, self.apiary_site.id, self.proposal_apiary.id)

    def get_relevant_applicant_name(self):
        if self.proposal_apiary and self.proposal_apiary.proposal:
            return self.proposal_apiary.proposal.relevant_applicant_name
        return ''

    class Meta:
        app_label = 'disturbance'
        unique_together = ['apiary_site', 'proposal_apiary',]


class ProposalApiary(RevisionedMixin):
    title = models.CharField('Title', max_length=200, null=True)
    location = gis_models.PointField(srid=4326, blank=True, null=True)
    proposal = models.OneToOneField(Proposal, related_name='proposal_apiary', null=True)

    # We don't use GIS field, because these are just fields user input into the <input> field
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # required for Site Transfer applications
    # transferee used to store EmailUser without existing licence
    transferee = models.ForeignKey(EmailUser, blank=True, null=True, related_name='apiary_transferee')
    transferee_email_text = models.CharField(max_length=200, null=True)
    originating_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name="site_transfer_originating_approval")
    target_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name="site_transfer_target_approval")
    target_approval_organisation = models.ForeignKey(Organisation, blank=True, null=True)
    target_approval_start_date = models.DateField(blank=True, null=True)
    target_approval_expiry_date = models.DateField(blank=True, null=True)
    reissue_originating_approval = models.BooleanField(default=False)
    reissue_target_approval = models.BooleanField(default=False)

    apiary_sites = models.ManyToManyField('ApiarySite', through=ApiarySiteOnProposal, related_name='proposal_apiary_set')
    #self_clone = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True)
    public_liability_insurance_expiry_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return 'id:{} - {}'.format(self.id, self.title)

    class Meta:
        app_label = 'disturbance'

    def validate_apiary_sites(self, raise_exception=False):
        validity = True

        # Check if the site has been already taken by someone else
        for apiary_site in self.apiary_sites.all():
            if apiary_site.is_vacant:
                # The site is 'vacant'
                others = ApiarySiteOnProposal.objects.filter(Q(apiary_site=apiary_site), (Q(making_payment=True) | Q(site_status=SITE_STATUS_PENDING))).exclude(proposal_apiary=self)
                if others:
                    # Someone has been making payment for this apiary site
                    validity = False
            else:
                # The site is not 'vacant'
                relation = self.get_relation(apiary_site)
                if relation != apiary_site.latest_proposal_link:
                    validity = False

            if not validity and raise_exception:
                # raise ValidationError(message='The vacant apiary site: {} is no longer available.'.format(apiary_site.id), params={'apiary_site_id': apiary_site.id})
                raise serializers.ValidationError({
                    'type': 'site_no_longer_available',
                    'message': 'The vacant apiary site: {} is no longer available.'.format(apiary_site.id),
                    'apiary_site_id': apiary_site.id})

        # Check the distance among the requested sites
        for apiary_site in self.apiary_sites.all():
            relation = self.get_relation(apiary_site)
            # Check among the apiary sites in this proposal except current one of the loop
            q_objects = Q(apiary_site__in=self.apiary_sites.all())
            q_objects &= Q(wkb_geometry_draft__distance_lte=(relation.wkb_geometry_draft, Distance(m=RESTRICTED_RADIUS)))
            qs_sites_within = ApiarySiteOnProposal.objects.filter(q_objects).exclude(apiary_site=apiary_site)
            if qs_sites_within:
                # In this proposal, there are apiary sites which are too close to each other
                if raise_exception:
                    # raise serializers.ValidationError(['There are apiary sites in this proposal which are too close to each other.',])
                    raise ValidationError('There are apiary sites in this proposal which are too close to each other.')
                validity = False

        return validity

    def final_decline(self):
        relations = self.get_relations()
        relations.update(site_status=SITE_STATUS_DENIED)
        self.apiary_sites.all().update(is_vacant=False)  # Once delclined or approved, is_vacant status must be set to False

    def post_payment_success(self):
        """
        Run this function just after the payment success
        """
        for relation in self.get_relations():
            # if relation.apiary_site.is_vacant:
            #     relation.apiary_site.is_vacant = False
            relation.apiary_site_status_when_submitted = relation.site_status
            relation.apiary_site_is_vacant_when_submitted = relation.apiary_site.is_vacant
            relation.wkb_geometry_processed = relation.wkb_geometry_draft
            relation.site_category_processed = relation.site_category_draft
            relation.site_status = SITE_STATUS_PENDING
            relation.making_payment = False  # This should replace the above line
            relation.application_fee_paid = True
            relation.save()

    def set_workflow_selected_status(self, apiary_site, selected_status):
        relation_obj = self.get_relation(apiary_site)
        relation_obj.workflow_selected_status = selected_status
        relation_obj.save()

    def get_wkb_geometry_processed(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.wkb_geometry_processed

    def get_wkb_geometry_draft(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.wkb_geometry_draft

    def get_workflow_selected_status(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.workflow_selected_status

    def get_status(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        return relation_obj.site_status

    def set_status(self, apiary_site, status):
        relation_obj = self.get_relation(apiary_site)
        relation_obj.site_status = status
        relation_obj.save()

    def get_relation(self, apiary_site):
        if isinstance(apiary_site, dict):
            apiary_site = ApiarySite.objects.get(id=apiary_site['id'])
        relation_obj = ApiarySiteOnProposal.objects.get(apiary_site=apiary_site, proposal_apiary=self)
        return relation_obj

    def get_relations(self):
        if self.proposal.application_type.name == 'Site Transfer':
            from disturbance.components.approvals.models import ApiarySiteOnApproval
            relation_objs = ApiarySiteOnApproval.objects.filter(id__in=SiteTransferApiarySite.objects.filter(proposal_apiary=self).values('apiary_site_on_approval_id'))
        else:
            relation_objs = ApiarySiteOnProposal.objects.filter(apiary_site__in=self.apiary_sites.all(), proposal_apiary=self)
        return relation_objs

    def delete_relation(self, apiary_site):
        relation_obj = self.get_relation(apiary_site)
        site_status_to_remove = relation_obj.site_status

        # Remove the relationship to the apiary_site
        relation_obj.delete()

        # Delete the apiary site itself if the status of it is 'draft'
        if site_status_to_remove == SITE_STATUS_DRAFT:
            if apiary_site.is_vacant:
                # 'vacant' site should not be deleted, the process should not reach here though
                pass
            else:
                # When removing the relation to the draft site, we don't need both the relation to the site and the site itself
                apiary_site.delete()
    # proposal_apiary send_referral
    def send_referral(self, request, group_id, referral_text):
        with transaction.atomic():
            try:
                if self.proposal.processing_status == 'with_assessor' or self.proposal.processing_status == 'with_referral':
                    self.proposal.processing_status = 'with_referral'
                    self.proposal.save()
                    self.save()
                    referral = None

                    # Check if the user is in ledger
                    try:
                        #referral_group = ApiaryReferralGroup.objects.get(name__icontains=referral_email)
                        referral_group = ApiaryReferralGroup.objects.get(id=group_id)
                    except ApiaryReferralGroup.DoesNotExist:
                        raise exceptions.ProposalReferralCannotBeSent()
                    #try:
                    existing_referrals = Referral.objects.filter(proposal=self.proposal)
                    #if existing_referral:
                    apiary_referral_list = ApiaryReferral.objects.filter(referral_group=referral_group,referral__in=existing_referrals) if existing_referrals else None
                    if apiary_referral_list:
                        raise ValidationError('A referral has already been sent to this group')
                    #except Referral.DoesNotExist:
                    # Create referral if it does not exist for referral_group
                    else:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self.proposal,
                            #referral=user,
                            #referral_group=referral_group,
                            sent_by=request.user,
                            text=referral_text
                        )
                        # Create corresponding ApiaryReferral
                        apiary_referral = ApiaryReferral.objects.create(
                            #proposal = self.referral.proposal,
                            referral=referral,
                            referral_group=referral_group,
                            #sent_by=request.user,
                            #text=referral_text
                        )
                        if self.proposal.application_type.name == 'Apiary':
                            # create referral checklist answers
                            for question in ApiaryChecklistQuestion.objects.filter(
                                    checklist_type='apiary',
                                    checklist_role='referrer'
                                    ):
                                new_answer = ApiaryChecklistAnswer.objects.create(
                                        proposal = self,
                                        apiary_referral = apiary_referral,
                                        question = question
                                        )

                            for question in ApiaryChecklistQuestion.objects.filter(
                                    checklist_type='apiary_per_site',
                                    checklist_role='referrer'
                                    ):
                                for site in self.get_relations():
                                    new_answer = ApiaryChecklistAnswer.objects.create(
                                            proposal = self,
                                            apiary_referral = apiary_referral,
                                            question = question,
                                            apiary_site=site.apiary_site
                                            )
                        elif self.proposal.application_type.name == 'Site Transfer':
                            # create referral checklist answers
                            for question in ApiaryChecklistQuestion.objects.filter(
                                    checklist_type='site_transfer',
                                    checklist_role='referrer'
                                    ):
                                new_answer = ApiaryChecklistAnswer.objects.create(
                                        proposal = self,
                                        apiary_referral = apiary_referral,
                                        question = question
                                        )

                            for question in ApiaryChecklistQuestion.objects.filter(
                                    checklist_type='site_transfer_per_site',
                                    checklist_role='referrer'
                                    ):
                                for site in self.get_relations():
                                    new_answer = ApiaryChecklistAnswer.objects.create(
                                            proposal = self,
                                            apiary_referral = apiary_referral,
                                            question = question,
                                            apiary_site=site.apiary_site
                                            )

                        # Create a log entry for the proposal
                        #self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                        self.proposal.log_user_action(
                                ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                    referral.id,
                                    self.proposal.lodgement_number,
                                    '{}'.format(referral_group.name)
                                    ),
                                request
                                )
                        # Create a log entry for the organisation
                        #self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                        applicant_field=getattr(self.proposal, self.proposal.applicant_field)
                        applicant_field.log_user_action(
                                ProposalUserAction.APIARY_ACTION_SEND_REFERRAL_TO.format(
                                    referral.id,
                                    self.proposal.lodgement_number,
                                    '{}'.format(referral_group.name)),
                                request
                                )
                        # send email
                        recipients = referral_group.members_list
                        send_apiary_referral_email_notification(referral, recipients, request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    @property
    def customer_status(self):
        return self.proposal.customer_status

    @property
    def processing_status(self):
        return self.proposal.processing_status

    @property
    def retrieve_approval(self):
        from disturbance.components.approvals.models import Approval
        approval = None
        if self.proposal.applicant:
            approval = Approval.objects.filter(applicant=self.proposal.applicant, status__in=[Approval.STATUS_CURRENT, Approval.STATUS_SUSPENDED], apiary_approval=True).first()
        elif self.proposal.proxy_applicant:
            approval = Approval.objects.filter(proxy_applicant=self.proposal.proxy_applicant, status__in=[Approval.STATUS_CURRENT, Approval.STATUS_SUSPENDED], apiary_approval=True).first()
        return approval

    def create_transferee_approval(self, details, applicant=None, proxy_applicant=None):
        from disturbance.components.approvals.models import Approval
        approval = Approval.objects.create(
            current_proposal = self.proposal,
            issue_date= timezone.now(),
            start_date= details.get('start_date'),
            expiry_date= details.get('expiry_date'),
            applicant= applicant,
            proxy_applicant= proxy_applicant,
            apiary_approval= self.proposal.apiary_group_application_type,
        )
        return approval

    # ProposalApiary final approval
    def final_approval(self,request,details,preview=False):
        from disturbance.components.approvals.models import Approval
        try:
            approval_created = None
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            if self.proposal.processing_status != Proposal.PROCESSING_STATUS_WITH_APPROVER:
                raise ValidationError('You cannot issue the approval if it is not with an approver')
            if not self.proposal.relevant_applicant_address:
                raise ValidationError('The applicant needs to have set their postal address before approving this proposal (Applicant: {})'.format(self.proposal.relevant_applicant))
            start_date = details.get('start_date').strftime('%d/%m/%Y') if details.get('start_date') else None
            expiry_date = details.get('expiry_date').strftime('%d/%m/%Y') if details.get('expiry_date') else None
            self.proposal.proposed_issuance_approval = {
                    'start_date': start_date,
                    'expiry_date': expiry_date,
                    'details': details.get('details'),
                    'cc_email': details.get('cc_email'),
            }

            sites_received = request.data.get('apiary_sites', [])
            sites_approved = [site for site in sites_received if site['checked']]
            if len(sites_approved) == 0:
                raise ValidationError("There must be at least one apiary site to approve")
            self.save()

            approval = None
            if self.proposal.application_type.name == ApplicationType.APIARY:
                approval = self.retrieve_approval
            elif self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                #approval = self.proposal.approval
                target_approval = self.target_approval
                originating_approval = self.originating_approval
                # New Licence creation for target_approval
                if not target_approval:
                    if self.target_approval_organisation:
                        target_approval = self.create_transferee_approval(details, applicant=Organisation.objects.get(id=self.target_approval_organisation_id))
                    else:
                        target_approval = self.create_transferee_approval(details, proxy_applicant=EmailUser.objects.get(id=self.transferee_id))
                    self.target_approval = target_approval
                    # set proposal_apiary requirements with sitetransfer_approval set to None to target_approval
                    transferee_requirements = self.proposal.requirements.filter(sitetransfer_approval=None).exclude(is_deleted=True)
                    for req in transferee_requirements:
                        req.sitetransfer_approval = target_approval
                        req.save()
                    # if we are creating a target licence, both licences must be reissued
                    self.reissue_originating_approval = True
                    self.reissue_target_approval = True
                    # ensure ProposalApiary object has been updated
                    self.save()
                elif self.transferee:
                    if self.target_approval.start_date != details.get('start_date'):
                        self.reissue_target_approval = True
                        self.target_approval.start_date = details.get('start_date')
                    if self.target_approval.expiry_date != details.get('expiry_date'):
                        self.reissue_target_approval = True
                        self.target_approval.expiry_date = details.get('expiry_date')
                    self.target_approval.save()

            self.proposal.proposed_decline_status = False
            self.proposal.processing_status = Proposal.PROCESSING_STATUS_APPROVED
            self.proposal.customer_status = Proposal.CUSTOMER_STATUS_APPROVED
            # Log proposal action

            checking_proposal = self.proposal
            checking_proposal.proposed_issuance_approval = self.proposal.proposed_issuance_approval
            checking_proposal.save()

            if self.proposal.proposal_type == 'amendment':
                # TODO - fix for apiary approval
                pass
            #    if self.proposal.previous_application:
            #        previous_approval = self.proposal.previous_application.approval
            #        approval,created = Approval.objects.update_or_create(
            #            current_proposal = checking_proposal,
            #            defaults = {
            #                #'activity' : self.activity,
            #                #'region' : self.region,
            #                #'tenure' : self.tenure,
            #                #'title' : self.title,
            #                'issue_date' : timezone.now(),
            #                'expiry_date' : details.get('expiry_date'),
            #                'start_date' : details.get('start_date'),
            #                'applicant' : self.proposal.applicant,
            #                'proxy_applicant' : self.proposal.proxy_applicant,
            #                'lodgement_number': previous_approval.lodgement_number,
            #                'apiary_approval': self.proposal.apiary_group_application_type,
            #                #'extracted_fields' = JSONField(blank=True, null=True)
            #            }
            #        )
            #        if created:
            #            previous_approval.replaced_by = approval
            #            previous_approval.save()
            #            # Get apiary sites from proposal
            #            #if self.proposal.application_type == ApplicationType.APIARY:
            #            #    for site in self.apiary_sites.all():
            #            #        site.approval = approval
            #            #elif self.proposal.application_type == ApplicationType.SITE_TRANSFER:
            #            #    for site in self.apiary_site_transfer.apiary_sites.all():
            #            #        site.approval = approval
            #            for site in self.apiary_sites.all():
            #                site.approval = approval

            if self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                # approval must already exist - we reissue with same start and expiry dates
                # does thhis need to be reissued with self.reissue_approval() ?
                #if originating_approval.reissued:
                 #   originating_approval.expiry_date = details.get('expiry_date')
                  #  originating_approval.start_date = details.get('start_date')
                # always reset this flag
                #originating_approval.reissued = False
                #self.proposal.proposed_issuance_approval['start_date'] = originating_approval.start_date.strftime('%d/%m/%Y')
                #self.proposal.proposed_issuance_approval['expiry_date'] = originating_approval.expiry_date.strftime('%d/%m/%Y')
                #self.proposal.proposed_issuance_approval['details'] = ''
                #self.proposal.proposed_issuance_approval['cc_email'] = ''
                #originating_approval.save()
                #target_approval.current_proposal = checking_proposal
                #target_approval.reissued = False
                #target_approval.save()
                if preview:
                    # do this instead of generate compliances section below
                    self.link_apiary_approval_requirements(originating_approval)
                    self.link_apiary_approval_requirements(target_approval)
            else:
                # Apiary approval
                from disturbance.components.approvals.models import ApprovalUserAction
                if not approval:
                    # There are no existing approvals.  Create a new one.
                    approval, approval_created = Approval.objects.update_or_create(
                        current_proposal = checking_proposal,
                        defaults = {
                        #'activity' : self.activity,
                        #'region' : self.region.name,
                        #'tenure' : self.tenure.name,
                        #'title' : self.title,
                        'issue_date' : timezone.now(),
                        'expiry_date' : details.get('expiry_date'),
                        'start_date' : details.get('start_date'),
                        'applicant' : self.proposal.applicant,
                        'proxy_applicant' : self.proposal.proxy_applicant,
                        'apiary_approval': self.proposal.apiary_group_application_type,
                        #'extracted_fields' = JSONField(blank=True, null=True)
                        }
                    )
                    if approval_created:
                        ApprovalUserAction.log_action(approval, ApprovalUserAction.ACTION_CREATE_APPROVAL.format(approval.lodgement_number), request.user)
                    else:
                        # approval already exist
                        # But should not reach here
                        ApprovalUserAction.log_action(approval, ApprovalUserAction.ACTION_UPDATE_APPROVAL.format(approval.lodgement_number), request.user)
                else:
                    approval.issue_date = timezone.now()
                    # ensure current_proposal is updated with this proposal
                    #if self.proposal.application_type.name != ApplicationType.SITE_TRANSFER:
                    approval.current_proposal = checking_proposal
                    if approval.reissued:
                        approval.expiry_date = details.get('expiry_date')
                        approval.start_date = details.get('start_date')
                    elif self.proposal.proposal_type == 'renewal':
                        approval.expiry_date = details.get('expiry_date')
                    # always reset this flag
                    approval.renewal_sent = False  # For the apiary, we have to rest this to False for the next renewal
                    #self.proposal.proposed_issuance_approval['start_date'] = approval.start_date.strftime('%d/%m/%Y')
                    #self.proposal.proposed_issuance_approval['expiry_date'] = approval.expiry_date.strftime('%d/%m/%Y')
                    #self.proposal.proposed_issuance_approval['details'] = ''
                    #self.proposal.proposed_issuance_approval['cc_email'] = ''
                    approval.save()
                    ApprovalUserAction.log_action(approval, ApprovalUserAction.ACTION_UPDATE_APPROVAL.format(approval.lodgement_number), request.user)
                if preview:
                    # do this instead of generate compliances section below
                    self.link_apiary_approval_requirements(approval)


            # Get apiary sites from proposal
            #if self.proposal.application_type == ApplicationType.APIARY:
            #    for site in self.proposal_apiary.apiary_sites.all():
            #        site.approval = approval
            #elif self.proposal.application_type == ApplicationType.SITE_TRANSFER:
            #    for site in self.apiary_site_transfer.apiary_sites.all():
            #        site.approval = approval
            # for site in self.apiary_sites.all():
            if self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                # updated apiary_site.selected with 'checked' flag status
                apiary_sites = request.data.get('apiary_sites', [])
                for apiary_site in apiary_sites:
                    transfer_site = SiteTransferApiarySite.objects.get(
                            proposal_apiary=self,
                            apiary_site_on_approval__apiary_site_id=apiary_site.get('id')
                            )
                    transfer_site.internal_selected = apiary_site.get('checked') if transfer_site.customer_selected else False
                    transfer_site.save()
                # update approval for all selected apiary sites
                transfer_sites = SiteTransferApiarySite.objects.filter(
                        proposal_apiary=self,
                        internal_selected=True,
                        customer_selected=True
                        )
                for site_transfer_apiary_site in transfer_sites:
                    relation_original = site_transfer_apiary_site.apiary_site_on_approval
                    from disturbance.components.approvals.models import ApiarySiteOnApproval

                    relation_target, asoa_created = ApiarySiteOnApproval.objects.get_or_create(
                        apiary_site=relation_original.apiary_site,
                        approval=target_approval,
                        #licensed_site = relation_original.licensed_site,
                    )
                    if asoa_created:
                        for apiary_site in apiary_sites:
                            site_id = apiary_site.get('id')
                            #licensed_site = apiary_site.get('properties')['licensed_site']
                            if site_id == relation_target.apiary_site_id:
                                #relation_target.licensed_site = licensed_site
                                relation_target.licensed_site = apiary_site.get('properties')['licensed_site']
                                relation_target.batch_no = apiary_site.get('properties')['batch_no']

                                if apiary_site.get('properties')['approval_cpc_date']:
                                    relation_target.approval_cpc_date = datetime.datetime.strptime(apiary_site.get('properties')['approval_cpc_date'], '%Y-%M-%d')
                                else:
                                    relation_target.approval_cpc_date = None

                                if apiary_site.get('properties')['approval_minister_date']:
                                    relation_target.approval_minister_date = datetime.datetime.strptime(apiary_site.get('properties')['approval_minister_date'], '%Y-%M-%d')
                                else:
                                    relation_target.approval_minister_date = None

                                relation_target.map_ref = apiary_site.get('properties')['map_ref']
                                relation_target.forest_block = apiary_site.get('properties')['forest_block']
                                relation_target.cog = apiary_site.get('properties')['cog']
                                relation_target.roadtrack = apiary_site.get('properties')['roadtrack']
                                relation_target.zone = apiary_site.get('properties')['zone']
                                relation_target.catchment = apiary_site.get('properties')['catchment']
                                relation_target.dra_permit = apiary_site.get('properties')['dra_permit']

                                relation_target.save()

                    if relation_original.site_status != SITE_STATUS_TRANSFERRED:  # Reissue both licences
                        relation_target.site_status = relation_original.site_status  # Copy the site status from the original to the target
                        # if at least one site is transferred, both licences should be reissued
                        self.reissue_originating_approval = True
                        self.reissue_target_approval = True
                    relation_original.site_status = SITE_STATUS_TRANSFERRED  # Set the site status of the original site to 'transferred'
                    relation_original.available = False
                    relation_original.save()
                    relation_target.wkb_geometry = relation_original.wkb_geometry
                    relation_target.site_category = relation_original.site_category
                    relation_target.save()
                    self.save()

            else:
                # could this be refactored into a separate method?
                from disturbance.management.commands.send_annual_rental_fee_invoice import get_annual_rental_fee_period
                from disturbance.components.das_payments.models import AnnualRentalFeePeriod
                from disturbance.components.das_payments.utils import generate_line_items_for_annual_rental_fee
                from disturbance.management.commands.send_annual_rental_fee_invoice import make_serializable
                from disturbance.components.das_payments.models import AnnualRentalFee, AnnualRentalFeeApiarySite
                from disturbance.components.approvals.email import send_annual_rental_fee_awaiting_payment_confirmation

                # sites_received = request.data.get('apiary_sites', [])
                # sites_approved = [site for site in sites_received if site['checked']]
                # if len(sites_approved) == 0:
                #     raise ValidationError("There must be at least one apiary site to approve")

                self._update_apiary_sites(approval, sites_received, request)

                # Check the current annual site fee period
                # Determine the start and end date of the annual site fee, for which the invoices should be issued
                today_now_local = datetime.datetime.now(pytz.timezone(TIME_ZONE))
                today_date_local = today_now_local.date()
                period_start_date, period_end_date = get_annual_rental_fee_period(today_date_local)

                # Retrieve annual site fee period object for the period calculated above
                # This period should not overwrap the existings, otherwise you will need a refund
                annual_rental_fee_period, perioed_created = AnnualRentalFeePeriod.objects.get_or_create(period_start_date=period_start_date, period_end_date=period_end_date)

                run_date = ApiaryAnnualRentalFeeRunDate.objects.get(name=ApiaryAnnualRentalFeeRunDate.NAME_CRON)
                if run_date.enabled_for_new_site:
                    line_items, apiary_sites_charged, invoice_period = generate_line_items_for_annual_rental_fee(
                        approval,
                        today_now_local,
                        (annual_rental_fee_period.period_start_date, annual_rental_fee_period.period_end_date),
                        sites_approved
                    )

                    if line_items:
                        basket = createCustomBasket(line_items, approval.relevant_applicant_email_user, PAYMENT_SYSTEM_ID)
                        order = CreateInvoiceBasket(
                            payment_method='other', system=PAYMENT_SYSTEM_PREFIX
                        ).create_invoice_and_order(basket, 0, None, None, user=approval.relevant_applicant_email_user,
                                                   invoice_text='Payment Invoice')
                        invoice = Invoice.objects.get(order_number=order.number)

                        line_items = make_serializable(line_items)  # Make line items serializable to store in the JSONField
                        annual_rental_fee = AnnualRentalFee.objects.create(
                            approval=approval,
                            annual_rental_fee_period=annual_rental_fee_period,
                            invoice_reference=invoice.reference,
                            invoice_period_start_date=invoice_period[0],
                            invoice_period_end_date=invoice_period[1],
                            lines=line_items,
                        )

                        for site in sites_approved:
                            # Store the apiary sites which the invoice created above has been issued for
                            apiary_site = ApiarySite.objects.get(id=site['id'])
                            annual_rental_fee_apiary_site = AnnualRentalFeeApiarySite(apiary_site=apiary_site, annual_rental_fee=annual_rental_fee)
                            annual_rental_fee_apiary_site.save()

                            # Add approved sites to the existing temporary use proposal with status 'draft'
                            proposal_apiary_temporary_use_qs = ProposalApiaryTemporaryUse.objects.filter(loaning_approval=approval, proposal__processing_status=Proposal.PROCESSING_STATUS_DRAFT)
                            for proposal_apiary_temporary_use in proposal_apiary_temporary_use_qs:
                                temp_use_apiary_site, temp_created = TemporaryUseApiarySite.objects.get_or_create(apiary_site=site, proposal_apiary_temporary_use=proposal_apiary_temporary_use)

                        if not preview:
                            email_data = send_annual_rental_fee_awaiting_payment_confirmation(approval, annual_rental_fee, invoice)

                            from disturbance.components.approvals.serializers import ApprovalLogEntrySerializer
                            email_data['approval'] = u'{}'.format(approval.id)
                            serializer = ApprovalLogEntrySerializer(data=email_data)
                            serializer.is_valid(raise_exception=True)
                            serializer.save()

                #print approval,approval.id, created
            # Generate compliances
            #self.generate_compliances(approval, request)
            if self.proposal.application_type.name == ApplicationType.APIARY and not preview:
                from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                if approval_created:
                    # ProposalType set during def create api method
                    #if self.proposal.proposal_type == 'amendment':
                    ## Delete future dated compliances for approval

                    #if self.proposal.previous_application:
                    #    approval_compliances = Compliance.objects.filter(
                    #            approval= approval,
                    #            #proposal = self.proposal.previous_application,
                    #            processing_status='future'
                    #            )
                    #    if approval_compliances:
                    #        for c in approval_compliances:
                    #            c.delete()

                    # Log creation
                    # Generate the document
                    self.link_apiary_approval_requirements(approval)
                    approval.generate_doc(request.user)
                    self.generate_apiary_compliances(approval, request)
                    # send the doc and log in approval and org
                    self.proposal.log_user_action(ProposalUserAction.ACTION_ISSUE_APIARY_APPROVAL.format(
                        self.proposal.lodgement_number,
                        start_date,
                        expiry_date,
                        str([site['id'] for site in sites_approved]).lstrip('[').rstrip(']')
                    ), request)
                    # Log entry for organisation
                    if self.proposal.applicant:
                        self.proposal.applicant.log_user_action(
                            ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.proposal.lodgement_number), request)
                else:
                    #approval.replaced_by = request.user
                    #approval.replaced_by = self.approval
                    # Generate the document

                    #Delete the future compliances if Approval is reissued and generate the compliances again.
                    approval_compliances = Compliance.objects.filter(
                            approval= approval,
                            #proposal = self.proposal,

                            processing_status='future'
                            )
                    if approval_compliances:
                        for c in approval_compliances:
                            c.delete()
                    self.link_apiary_approval_requirements(approval)
                    approval.generate_doc(request.user)
                    self.generate_apiary_compliances(approval, request)
                    # Log proposal action
                    self.proposal.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.lodgement_number), request)
                    # Log entry for organisation
                    if self.proposal.applicant:
                        self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.proposal.lodgement_number), request)

                self.proposal.approval = approval
                #send Proposal approval email with attachment
                send_proposal_approval_email_notification(self.proposal,request)
                # flag must be reset after email is sent
                approval.reissued = False
                approval.save()
                self.proposal.save(version_comment='Final Approval: {}'.format(self.proposal.approval.lodgement_number))
                self.proposal.approval.documents.all().update(can_delete=False)
            elif self.proposal.application_type.name == ApplicationType.SITE_TRANSFER and not preview:
                # add Site Transfer Compliance/Requirements logic here
                from disturbance.components.compliances.models import Compliance, ComplianceUserAction
                ## Originating approval
                if self.reissue_originating_approval or not originating_approval.reissued:
                    originating_approval.issue_date = timezone.now()
                    originating_approval.current_proposal = checking_proposal
                    originating_approval.save()
                    #Delete the future compliances if Approval is reissued and generate the compliances again.
                    approval_compliances = Compliance.objects.filter(
                            approval= originating_approval,
                            #proposal = self.proposal,
                            processing_status='future'
                            )
                    if approval_compliances:
                        for c in approval_compliances:
                            c.delete()
                    #self.generate_apiary_site_transfer_compliances(originating_approval, request)
                    self.link_apiary_approval_requirements(originating_approval)
                    originating_approval.generate_apiary_site_transfer_doc(request.user, site_transfer_proposal=self.proposal)
                    self.generate_apiary_compliances(originating_approval, request)
                    # Log proposal action
                    self.proposal.log_user_action(
                        ProposalUserAction.ACTION_UPDATE_APPROVAL_FOR_PROPOSAL.format(
                            originating_approval.lodgement_number,
                            self.proposal.id), request)
                    # Log entry for organisation
                    if self.proposal.applicant:
                        self.proposal.applicant.log_user_action(
                            ProposalUserAction.ACTION_UPDATE_APPROVAL_FOR_PROPOSAL.format(
                                originating_approval.lodgement_number,
                                self.proposal.id), request)
                    #send Proposal approval email with attachment
                    send_site_transfer_approval_email_notification(self.proposal, request, originating_approval)
                    # reset flag after email is sent
                    originating_approval.reissued = False
                    originating_approval.save()
                ## Target approval
                if self.reissue_target_approval or not target_approval.reissued:
                    target_approval.issue_date = timezone.now()
                    target_approval.current_proposal = checking_proposal
                    target_approval.save()
                    #Delete the future compliances if Approval is reissued and generate the compliances again.
                    approval_compliances = Compliance.objects.filter(
                            approval=target_approval,
                            #proposal = self.proposal,
                            processing_status='future'
                            )
                    if approval_compliances:
                        for c in approval_compliances:
                            c.delete()
                    self.link_apiary_approval_requirements(target_approval)
                    target_approval.generate_apiary_site_transfer_doc(request.user, site_transfer_proposal=self.proposal)
                    self.generate_apiary_compliances(target_approval, request)
                    # Log proposal action
                    self.proposal.log_user_action(
                        ProposalUserAction.ACTION_UPDATE_APPROVAL_FOR_PROPOSAL.format(
                            target_approval.lodgement_number,
                            self.proposal.id), request)
                    # Log entry for organisation
                    if self.proposal.applicant:
                        self.proposal.applicant.log_user_action(
                                ProposalUserAction.ACTION_UPDATE_APPROVAL_FOR_PROPOSAL.format(
                                    target_approval.lodgement_number,
                                    self.proposal.id), request)
                    #send Proposal approval email with attachment
                    send_site_transfer_approval_email_notification(self.proposal, request, target_approval)
                    # reset flag after approval is sent
                    target_approval.reissued = False
                    target_approval.save()
                    #self.proposal.save(version_comment='Final Approval: {}'.format(self.proposal.approval.lodgement_number))
                    self.proposal.save(version_comment='Originating Approval: {}, Target Approval: {}'.format(
                        originating_approval.lodgement_number,
                        target_approval.lodgement_number,
                        )
                    )
                    #self.proposal.approval.documents.all().update(can_delete=False)
                    originating_approval.documents.all().update(can_delete=False)
                    target_approval.documents.all().update(can_delete=False)

            return self
        except:
            raise

    def link_apiary_approval_requirements(self, approval):
        # Ensure current requirements are associated with apiary approval / site transfer
        link_requirement_set = self.proposal.requirements.all()
        for link_r in link_requirement_set:
            if self.proposal.application_type.name == ApplicationType.SITE_TRANSFER:
                if not link_r.is_deleted and link_r.sitetransfer_approval == approval:
                    link_r.apiary_approval = approval
                    link_r.save()
            else:
                link_r.apiary_approval = approval
                link_r.save()

        # Remove apiary approval link from previous requirements
        unlink_requirement_set = approval.proposalrequirement_set.exclude(proposal=self.proposal)
        for unlink_r in unlink_requirement_set:
            unlink_r.apiary_approval = None
            unlink_r.save()

    def generate_apiary_compliances(self, approval, request):
        today = timezone.now().date()
        timedelta = datetime.timedelta
        from disturbance.components.compliances.models import Compliance, ComplianceUserAction

        #For amendment type of Proposal, check for copied requirements from previous proposal
        if self.proposal.previous_application:
            try:
                for r in self.proposal.requirements.filter(apiary_approval=approval).filter(copied_from__isnull=False):
                    cs = []
                    # Now discard all of the due compliances
                    cs = Compliance.objects.filter(
                            requirement=r.copied_from,
                            approval=approval,
                            processing_status='due'
                            )
                    if cs:
                        if r.is_deleted:
                            for c in cs:
                                c.processing_status = 'discarded'
                                c.customer_status = 'discarded'
                                c.reminder_sent = True
                                c.post_reminder_sent = True
                                c.save()
            except:
                raise
        #requirement_set= self.requirements.filter(copied_from__isnull=True).exclude(is_deleted=True)
        requirement_set= self.proposal.requirements.filter(apiary_approval=approval).exclude(is_deleted=True)

        #for req in self.requirements.all():
        for req in requirement_set:
            try:
                if req.due_date and req.due_date >= today:
                    current_date = req.due_date
                    #create a first Compliance
                    try:
                        compliance = Compliance.objects.get(requirement=req, due_date=current_date)
                    except Compliance.DoesNotExist:
                        compliance =Compliance.objects.create(
                                    proposal=self.proposal,
                                    due_date=current_date,
                                    processing_status='future',
                                    approval=approval,
                                    requirement=req,
                                    apiary_compliance=True
                        )
                        compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.lodgement_number), request)
                    if req.recurrence:
                        while current_date < approval.expiry_date:
                            for x in range(req.recurrence_schedule):
                            #Weekly
                                if req.recurrence_pattern == 1:
                                    current_date += timedelta(weeks=1)
                            #Monthly
                                elif req.recurrence_pattern == 2:
                                    current_date += timedelta(weeks=4)
                                    pass
                            #Yearly
                                elif req.recurrence_pattern == 3:
                                    current_date += timedelta(days=365)
                            # Create the compliance
                            if current_date <= approval.expiry_date:
                                try:
                                    compliance = Compliance.objects.get(requirement = req, due_date = current_date)
                                except Compliance.DoesNotExist:
                                    compliance = Compliance.objects.create(
                                                proposal=self.proposal,
                                                due_date=current_date,
                                                processing_status='future',
                                                approval=approval,
                                                requirement=req,
                                                apiary_compliance=True
                                    )
                                    compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.lodgement_number), request)
            except:
                raise

    def _update_apiary_sites(self, approval, sites_approved, request):
        for site in request.data.get('apiary_sites'):
            # During final approval - Approver may have updated these values
            if not site['properties'].get('licensed_site'):
                a_site = ApiarySite.objects.get(id=site['id'])
                apiary_site_on_proposal = self.get_relation(a_site)

                apiary_site_on_proposal.licensed_site = site['properties'].get('licensed_site')
                apiary_site_on_proposal.batch_no = site['properties'].get('batch_no')
                apiary_site_on_proposal.approval_cpc_date = datetime.datetime.strptime(site['properties'].get('approval_cpc_date'), '%Y-%M-%d') if site['properties'].get('approval_cpc_date') else None
                apiary_site_on_proposal.approval_minister_date = datetime.datetime.strptime(site['properties'].get('approval_minister_date'), '%Y-%M-%d') if site['properties'].get('approval_minister_date') else None
                apiary_site_on_proposal.map_ref = site['properties'].get('map_ref')
                apiary_site_on_proposal.forest_block = site['properties'].get('forest_block')
                apiary_site_on_proposal.cog = site['properties'].get('cog')
                apiary_site_on_proposal.roadtrack = site['properties'].get('roadtrack')
                apiary_site_on_proposal.zone = site['properties'].get('zone')
                apiary_site_on_proposal.catchment = site['properties'].get('catchment')
                apiary_site_on_proposal.dra_permit = site['properties'].get('dra_permit')
                apiary_site_on_proposal.save()
           
        for my_site in sites_approved:
            a_site = ApiarySite.objects.get(id=my_site['id'])
            apiary_site_on_proposal = self.get_relation(a_site)

            if my_site['checked']:
                # relation.approval = approval
                apiary_site_on_proposal.site_status = SITE_STATUS_APPROVED
            else:
                apiary_site_on_proposal.site_status = SITE_STATUS_DENIED
            # Reset selected status to make the checkboxes unticked when renewal or so
            apiary_site_on_proposal.workflow_selected_status = False
            apiary_site_on_proposal.save()
            a_site.make_vacant(False, apiary_site_on_proposal)
            a_site.save()

            # Apiary Site can be moved by assessor and/or approver
            if 'coordinates_moved' in my_site:
                prev_coordinates = apiary_site_on_proposal.wkb_geometry_processed.get_coords()
                geom_str = GEOSGeometry('POINT(' + str(my_site['coordinates_moved']['lng']) + ' ' + str(my_site['coordinates_moved']['lat']) + ')', srid=4326)
                from disturbance.components.proposals.serializers_apiary import ApiarySiteOnProposalProcessedGeometrySaveSerializer
                serializer = ApiarySiteOnProposalProcessedGeometrySaveSerializer(apiary_site_on_proposal, data={'wkb_geometry_processed': geom_str})
                serializer.is_valid(raise_exception=True)
                serializer.save()

                # Log it
                self.proposal.log_user_action(ProposalUserAction.APIARY_SITE_MOVED.format(my_site['id'], prev_coordinates, (my_site['coordinates_moved']['lng'], my_site['coordinates_moved']['lat'])), request)

            # Because this is final approval, copy the data from the proposal to the approval
            from disturbance.components.approvals.models import ApiarySiteOnApproval
            if apiary_site_on_proposal.site_status == SITE_STATUS_APPROVED:
                # Create a relation between the approved apairy site and the approval
                apiary_site_on_approval, asoa_created = ApiarySiteOnApproval.objects.get_or_create(apiary_site=a_site, approval=approval)
                apiary_site_on_approval.wkb_geometry = apiary_site_on_proposal.wkb_geometry_processed
                apiary_site_on_approval.site_category = apiary_site_on_proposal.site_category_processed
                apiary_site_on_approval.licensed_site = apiary_site_on_proposal.licensed_site
                apiary_site_on_approval.batch_no = apiary_site_on_proposal.batch_no
                apiary_site_on_approval.approval_cpc_date = apiary_site_on_proposal.approval_cpc_date
                apiary_site_on_approval.approval_minister_date = apiary_site_on_proposal.approval_minister_date
                apiary_site_on_approval.map_ref = apiary_site_on_proposal.map_ref
                apiary_site_on_approval.forest_block = apiary_site_on_proposal.forest_block
                apiary_site_on_approval.cog = apiary_site_on_proposal.cog
                apiary_site_on_approval.roadtrack = apiary_site_on_proposal.roadtrack
                apiary_site_on_approval.zone = apiary_site_on_proposal.zone
                apiary_site_on_approval.catchment = apiary_site_on_proposal.catchment
                apiary_site_on_approval.dra_permit = apiary_site_on_proposal.dra_permit
                apiary_site_on_approval.site_status = SITE_STATUS_CURRENT
                apiary_site_on_approval.save()
            else:
                try:
                    qs = ApiarySiteOnApproval.objects.filter(apiary_site=a_site, approval=approval)
                    if qs:
                        apiary_site_on_approval = qs[0]
                        apiary_site_on_approval.delete()
                except:
                    pass

class SiteCategory(models.Model):
    CATEGORY_SOUTH_WEST = 'south_west'
    CATEGORY_REMOTE = 'remote'
    CATEGORY_CHOICES = (
        (CATEGORY_SOUTH_WEST, 'South West'),
        (CATEGORY_REMOTE, 'Remote')
    )
    # This model is used to distinguish the application gtfees' differences
    name = models.CharField(unique=True, max_length=50, choices=CATEGORY_CHOICES)

    def retrieve_current_fee_per_site_by_type(self, fee_type_name):
        today_local = datetime.datetime.now(pytz.timezone(TIME_ZONE)).date()
        ret_date = self.retrieve_fee_by_date_and_type(today_local, fee_type_name)
        return ret_date

    def retrieve_fee_by_date_and_type(self, target_date, fee_type_name):
        fee_type_application = ApiarySiteFeeType.objects.get(name=fee_type_name)
        if not fee_type_application:
            raise Exception("Please select 'new_application' and save it at the Apiary Site Fee Type admin page")

        site_fee = ApiarySiteFee.objects.filter(
                    Q(apiary_site_fee_type=fee_type_application) &
                    Q(site_category=self) &
                    Q(date_of_enforcement__lte=target_date)
                    ).order_by('date_of_enforcement', ).last()

        if site_fee:
            return site_fee.amount
        else:
            return None

    @property
    def display_name(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if self.name == item[0]:
                return item[1]
        return '---'

    @property
    def fee_application_per_site(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if item[0] == self.name:
                fee_application = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_APPLICATION)
                return fee_application
        return '---'

    @property
    def fee_renewal_per_site(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if item[0] == self.name:
                fee_renewal = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_RENEWAL)
                return fee_renewal
        return '---'

    @property
    def fee_transfer_per_site(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if item[0] == self.name:
                fee_transfer = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_TRANSFER)
                return fee_transfer
        return '---'



    def __str__(self):
        for item in SiteCategory.CATEGORY_CHOICES:
            if item[0] == self.name:
                fee_application = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_APPLICATION)
                # fee_amendment = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_AMENDMENT)
                fee_renewal = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_RENEWAL)
                fee_transfer = self.retrieve_current_fee_per_site_by_type(ApiarySiteFeeType.FEE_TYPE_TRANSFER)
                return '{} - new application: ${}, transfer: ${}, renewal: ${}'.format(item[1], fee_application, fee_transfer, fee_renewal)
        return '---'

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'apiary site fee'


class ApiarySiteFeeType(RevisionedMixin):
    FEE_TYPE_APPLICATION = 'new_application'
    # FEE_TYPE_AMENDMENT = 'amendment'
    FEE_TYPE_RENEWAL = 'renewal'
    FEE_TYPE_TRANSFER = 'transfer'
    FEE_TYPE_CHOICES = (
        (FEE_TYPE_APPLICATION, 'New Application'),
        # (FEE_TYPE_AMENDMENT, 'Amendment'),
        (FEE_TYPE_RENEWAL, 'Renewal'),
        (FEE_TYPE_TRANSFER, 'Transfer'),
    )
    name = models.CharField(unique=True, max_length=50, choices=FEE_TYPE_CHOICES,)
    description = models.TextField(blank=True)

    def __str__(self):
        for item in ApiarySiteFeeType.FEE_TYPE_CHOICES:
            if item[0] == self.name:
                return '{}'.format(item[1])
        return '---'

    class Meta:
        app_label = 'disturbance'


class ApiarySiteFee(RevisionedMixin):
    amount = models.DecimalField(max_digits=8, decimal_places=2, default='0.00')
    date_of_enforcement = models.DateField(blank=True, null=True)
    site_category = models.ForeignKey(SiteCategory, related_name='site_fees')
    apiary_site_fee_type = models.ForeignKey(ApiarySiteFeeType, null=True, blank=True)

    class Meta:
        app_label = 'disturbance'
        ordering = ('date_of_enforcement', )  # oldest record first, latest record last

    def __str__(self):
        return '${} ({}:{})'.format(self.amount, self.date_of_enforcement, self.site_category)


class ApiaryAnnualRentalFee(RevisionedMixin):
    """
    This amount is applied from the date_from
    """
    amount_south_west = models.DecimalField(max_digits=8, decimal_places=2, default='0.00')
    amount_remote = models.DecimalField(max_digits=8, decimal_places=2, default='0.00')
    date_from = models.DateField(blank=True, null=True)

    class Meta:
        app_label = 'disturbance'
        ordering = ('date_from', )  # oldest record first, latest record last
        verbose_name = 'Annual Site Fee'

    def __str__(self):
        return 'Amount(SW): ${}, Amount(Remote): ${}, From: {}'.format(self.amount_south_west, self.amount_remote, self.date_from)

    # @staticmethod
    # def get_fee_at_target_date(target_date):
    #     fee_applied = ApiaryAnnualRentalFee.objects.filter(date_from__lte=target_date).order_by('-date_from').first()
    #     return fee_applied

    @staticmethod
    def get_fees_by_period(start_date, end_date):
        fee_first = ApiaryAnnualRentalFee.objects.filter(date_from__lte=start_date)
        fees_rest = ApiaryAnnualRentalFee.objects.filter(date_from__gt=start_date, date_from__lte=end_date).order_by('date_from')
        if not fee_first:
            raise ValidationError("No annual site fee amounts found.  Please configure at least one annual site fee amount at the admin page.")
        else:
            fee_first = fee_first.latest('date_from')

        temp_end_date = end_date if not fees_rest else fees_rest[0].date_from - datetime.timedelta(days=1)
        fees = [{
            'amount_south_west_per_year': fee_first.amount_south_west,
            'amount_remote_per_year': fee_first.amount_remote,
            'date_start': start_date,
            'date_end': temp_end_date,
            'num_of_days': temp_end_date - (start_date - datetime.timedelta(days=1))
        }]
        for idx, annual_rental_fee in enumerate(fees_rest):
            temp_end_date = end_date if idx == len(fees_rest) - 1 else fees_rest[idx + 1].date_from - datetime.timedelta( days=1)
            fee = {
                'amount_south_west_per_year': annual_rental_fee.amount_south_west,
                'amount_remote_per_year': annual_rental_fee.amount_remote,
                'date_start': annual_rental_fee.date_from,
                'date_end': temp_end_date,
                'num_of_days': temp_end_date - (annual_rental_fee.date_from - datetime.timedelta(days=1))
            }
            fees.append(fee)

        return fees


class ApiaryAnnualRentalFeePeriodStartDate(RevisionedMixin):
    """
    Calculation of the annual site fee starts from this date
    """
    NAME_PERIOD_START = 'period_start_date'
    NAME_CHOICES = (
        (NAME_PERIOD_START, 'Start date of the annual site fee'),
    )
    name = models.CharField(unique=True, max_length=50, choices=NAME_CHOICES, )
    period_start_date = models.DateField(blank=True, null=True, help_text='Although year, month and date are entered, the system uses only the month and the date internally')

    def __str__(self):
        try:
            return '{}: {} {}'.format(self.name, self.period_start_date.strftime('%B'), self.period_start_date.day)
        except:
            return '{}'.format(self.name)

    class Meta:
        app_label = 'disturbance'
        ordering = ('period_start_date', )  # oldest record first, latest record last
        verbose_name = 'Annual Site Fee Period Start Date'


class ApiaryAnnualRentalFeeRunDate(RevisionedMixin):
    """
    This is the date to issue the annual site fee invoices
    """
    NAME_CRON = 'date_to_run_cron_job'
    NAME_CHOICES = (
        (NAME_CRON, 'Date to Issue'),
    )
    name = models.CharField(unique=True, max_length=50, choices=NAME_CHOICES, )
    date_run_cron = models.DateField(blank=True, null=True, help_text='Although year, month and date are entered, the system uses only the month and the date internally')
    enabled = models.BooleanField(default=False, verbose_name='Apply by cronjob', help_text='Sets whether the annual fee is applied to the sites by the cron job')
    enabled_for_new_site = models.BooleanField(default=False, verbose_name='Apply when approved', help_text='Sets whether the annual fee is applied when an application is approved')

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Annual Site Fee Issue Date'

    def __str__(self):
        try:
            return '{}: {} {}'.format(self.name, self.date_run_cron.strftime('%B'), self.date_run_cron.day)
        except:
            return '{}'.format(self.name)


class ApiarySite(models.Model):
    id = models.IntegerField(primary_key=True, editable=False)

    site_guid = models.CharField(max_length=50, blank=True)
    latest_proposal_link = models.ForeignKey('disturbance.ApiarySiteOnProposal', blank=True, null=True, on_delete=models.SET_NULL)
    latest_approval_link = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True, on_delete=models.SET_NULL)
    # Store the proposal link intermediate object this apiary site transitioned from when got the 'vacant' status
    proposal_link_for_vacant = models.ForeignKey('disturbance.ApiarySiteOnProposal', blank=True, null=True, related_name='vacant_apiary_site', on_delete=models.SET_NULL)
    # Store the approval link intermediate object this apiary site transitioned from when got the 'vacant' status
    approval_link_for_vacant = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True, related_name='vacant_apiary_site', on_delete=models.SET_NULL)
    is_vacant = models.BooleanField(default=False)

    exempt_from_radius_restriction = models.BooleanField(default=False) #does not require restricted radius validation on proposal

    def get_relevant_applicant_name(self):
        relevant_name = ''

        try:
            if not self.is_vacant:
                if self.latest_approval_link and self.latest_approval_link.site_status in [SITE_STATUS_CURRENT, SITE_STATUS_SUSPENDED, SITE_STATUS_NOT_TO_BE_REISSUED,]:
                    relevant_name = self.latest_approval_link.approval.relevant_applicant_name
                elif self.latest_proposal_link and self.latest_proposal_link.site_status in [SITE_STATUS_PENDING, SITE_STATUS_DENIED,]:
                    relevant_name = self.latest_proposal_link.proposal_apiary.proposal.relevant_applicant_name
        except Exception as e:
            logger.error('Exception raised when retrieving the relevant applicant name for the apiary site: {}. Error: {}'.format(self, e))

        return relevant_name

    def __str__(self):
        return '{}'.format(self.id,)

    def save(self, **kwargs):
        if not self.id:
            max = ApiarySite.objects.aggregate(id_max=Max('id'))['id_max']
            self.id = int(max) + 1 if max is not None else 1

        #kwargs.pop('force_insert')
        #kwargs.update({'force_update': True})
        super().save(kwargs)

    def delete(self, using=None, keep_parents=False):
        super(ApiarySite, self).delete(using, keep_parents)
        print('ApiarySite: {}({}) has been deleted.'.format(self.id, self.is_vacant))

    @property
    def can_be_deleted_from_the_system(self):
        """
        We can delete the apiary site from the system only when it has never been applied.
        """
        can_be_deleted = False

        if self.proposal_apiary_set.count() <= 1 and self.approval_set.count() == 0 and not self.is_vacant:
            if not self.latest_proposal_link.application_fee_paid and self.latest_proposal_link.site_status == SITE_STATUS_DRAFT:
                # application_fee_paid == False means that this apiary site has never been submitted
                can_be_deleted = True

        return can_be_deleted

    def make_vacant(self, vacant, relation):
        self.is_vacant = vacant
        from disturbance.components.approvals.models import ApiarySiteOnApproval
        if isinstance(relation, ApiarySiteOnProposal):
            self.proposal_link_for_vacant = relation if vacant else None
            self.approval_link_for_vacant = None  # make sure either proposal_link_for_vacant or approval_link_for_vacant is True at most.
        elif isinstance(relation, ApiarySiteOnApproval):
            self.proposal_link_for_vacant = None  # make sure either proposal_link_for_vacant or approval_link_for_vacant is True at most.
            self.approval_link_for_vacant = relation if vacant else None
        self.save()

    def get_relation(self, proposal_apiary_or_approval):
        if isinstance(proposal_apiary_or_approval, ProposalApiary):
            return ApiarySiteOnProposal.objects.get(apiary_site=self, proposal_apiary=proposal_apiary_or_approval)
        else:
            from disturbance.components.approvals.models import ApiarySiteOnApproval
            return ApiarySiteOnApproval.objects.get(apiary_site=self, approval=proposal_apiary_or_approval)

    def get_current_application_fee_per_site(self):
        current_fee = self.site_category.current_application_fee_per_site
        return current_fee

    #latest relevant coords
    @property
    def coordinates(self):
        latest_link = None
        latest_link_date = None

        if self.latest_proposal_link and self.latest_proposal_link.wkb_geometry_processed:
            latest_link_date = self.latest_proposal_link.created_at
            latest_link = self.latest_proposal_link.wkb_geometry_processed.coords

        if self.latest_approval_link and self.latest_approval_link.wkb_geometry and (not latest_link_date or self.latest_approval_link.created_at >= latest_link_date):
            latest_link_date = self.latest_approval_link.created_at
            latest_link = self.latest_approval_link.wkb_geometry.coords

        if self.proposal_link_for_vacant and self.latest_proposal_link.wkb_geometry_processed and (not latest_link_date or self.proposal_link_for_vacant.created_at >= latest_link_date):
            latest_link_date = self.proposal_link_for_vacant.created_at
            latest_link = self.proposal_link_for_vacant.wkb_geometry_processed.coords

        if self.approval_link_for_vacant and self.latest_approval_link.wkb_geometry and (not latest_link_date or self.approval_link_for_vacant.created_at >= latest_link_date):
            latest_link_date = self.approval_link_for_vacant.created_at
            latest_link = self.approval_link_for_vacant.wkb_geometry.coords

        return latest_link

    class Meta:
        app_label = 'disturbance'


# class ApiarySiteLocation(models.Model):
#     TYPE_DRAFT = 'draft'
#     TYPE_PROCESSED = 'processed'
#     TYPE_APPROVED = 'approved'
#     TYPE_CHOICES = (
#         (TYPE_DRAFT, 'Draft'),
#         (TYPE_PROCESSED, 'Processed'),
#         (TYPE_APPROVED, 'Approved'),
#     )
#     wkb_geometry = PointField(srid=4326, blank=True, null=True)
#     type = models.CharField(max_length=40, choices=TYPE_CHOICES, default=TYPE_CHOICES[0][0])
#     apiary_site = models.ForeignKey('ApiarySite', blank=True, null=True)
#     proposal_apiary = models.ForeignKey('ProposalApiary', blank=True, null=True)
#     approval = models.ForeignKey('Approval', blank=True, null=True)
#     created_at = models.DateTimeField(auto_now_add=True)
#     modified_at = models.DateTimeField(auto_now=True)
#     objects = GeoManager()
#
#     class Meta:
#         app_label = 'disturbance'
#         ordering = ['-modified_at', '-created_at',]


class ApiarySiteFeeRemainder(models.Model):
    '''
    A record of this model represents e site is left

    You have to check the validity of the record by date_expiry and date_used fields
    '''
    site_category = models.ForeignKey(SiteCategory)
    apiary_site_fee_type = models.ForeignKey(ApiarySiteFeeType)
    applicant = models.ForeignKey(Organisation, null=True, blank=True)
    proxy_applicant = models.ForeignKey(EmailUser, null=True, blank=True)
    datetime_created = models.DateTimeField(auto_now_add=True)
    date_expiry = models.DateField(null=True, blank=True)
    date_used = models.DateField(null=True, blank=True)

    def __str__(self):
        #return 'Remainder: {} - {} - {} - {} site(s)'.format(self.applicant, self.site_category, self.apiary_site_fee_type, self.number_of_sites_left)
        return 'Remainder: {} - {} - {} - site(s)'.format(self.applicant, self.site_category, self.apiary_site_fee_type)

    class Meta:
        app_label = 'disturbance'


class OnSiteInformation(models.Model):
    # apiary_site = models.ForeignKey(ApiarySite, null=True, blank=True)
    apiary_site_on_approval = models.ForeignKey('ApiarySiteOnApproval', blank=True, null=True)
    period_from = models.DateField(null=True, blank=True)
    period_to = models.DateField(null=True, blank=True)
    hives_loc = models.TextField(blank=True)
    hives_num = models.SmallIntegerField(blank=True, null=True)
    people_names = models.TextField(blank=True)
    flora = models.TextField(blank=True)
    comments = models.TextField(blank=True)
    datetime_deleted = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return 'OnSiteInfo id: {}, date: {} to {}'.format(self.id, self.period_from, self.period_to)

    class Meta:
        app_label = 'disturbance'


class ProposalApiaryTemporaryUse(models.Model):
    from_date = models.DateField('Period From Date', blank=True, null=True)
    to_date = models.DateField('Period To Date', blank=True, null=True)
    proposal = models.OneToOneField(Proposal, related_name='apiary_temporary_use', null=True, blank=True)
    # proposal_apiary_base = models.ForeignKey(Proposal, related_name='apiary_temporary_use_set', null=True, blank=True)
    temporary_occupier_name = models.CharField(max_length=255, blank=True, null=True)
    temporary_occupier_phone = models.CharField(max_length=50, blank=True, null=True)
    temporary_occupier_mobile = models.CharField(max_length=50, blank=True, null=True)
    temporary_occupier_email = models.EmailField(blank=True, null=True)
    loaning_approval = models.ForeignKey('disturbance.Approval', blank=True, null=True)

    # def __str__(self):
    #     if self.proposal.proposal_apiary:
    #         return 'id:{} - {}'.format(self.id, self.proposal.proposal_apiary.title)
    #     else:
            # Should not reach here
            # return 'id:{}'.format(self.id)

    class Meta:
        app_label = 'disturbance'

    def period_valid_for_temporary_use(self, period):
        detail = {}
        valid = True

        # Check if the period sits in the approval valid period
        if period[0] < self.loaning_approval.start_date or self.loaning_approval.expiry_date < period[1]:
            valid = False
            if not valid:
                detail['period'] = {}
                detail['period']['from_date'] = self.loaning_approval.start_date
                detail['period']['to_date'] = self.loaning_approval.expiry_date
                detail['reason'] = 'out_of_range_of_licence'
                return valid, detail

        # TODO: Check if the period submitted overlaps with the existing temprary use periods
        #qs = TemporaryUseApiarySite.objects.filter(apiary_site=self, selected=True, proposal_apiary_temporary_use__proposal__processing_status=Proposal.PROCESSING_STATUS_APPROVED)
        #for temp_site in qs:
        #    valid = (period[0] <= period[1] < temp_site.proposal_apiary_temporary_use.from_date) or (temp_site.proposal_apiary_temporary_use.to_date < period[0] <= period[1])
        #    if not valid:
        #        detail['period'] = {}
        #        detail['period']['from_date'] = temp_site.proposal_apiary_temporary_use.from_date
        #        detail['period']['to_date'] = temp_site.proposal_apiary_temporary_use.to_date
        #        detail['apiary_site'] = temp_site.apiary_site
        #        detail['reason'] = 'overlap_existing'
        #        return valid, detail

        return valid, detail


class TemporaryUseApiarySite(models.Model):
    """
    Apiary sites under a proposal can be partially used as temporary site
    """
    proposal_apiary_temporary_use = models.ForeignKey(ProposalApiaryTemporaryUse, blank=True, null=True, related_name='temporary_use_apiary_sites')
    # apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True)
    apiary_site_on_approval = models.ForeignKey('ApiarySiteOnApproval', blank=True, null=True)
    selected = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'


class SiteTransferApiarySite(models.Model):
    proposal_apiary = models.ForeignKey(ProposalApiary, blank=True, null=True, related_name='site_transfer_apiary_sites')
    # apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True)
    apiary_site_on_approval = models.ForeignKey('disturbance.ApiarySiteOnApproval', blank=True, null=True)
    internal_selected = models.BooleanField(default=False)
    customer_selected = models.BooleanField(default=False)

    class Meta:
        app_label = 'disturbance'


# TODO: remove if no longer required
class ApiarySiteApproval(models.Model):
    """
    This is intermediate table between ApiarySite and Approval to hold an approved apiary site under a certain approval
    """
    apiary_site = models.ForeignKey(ApiarySite, blank=True, null=True, related_name='apiary_site_approval_set')
    approval = models.ForeignKey('disturbance.Approval', blank=True, null=True, related_name='apiary_site_approval_set')

    class Meta:
        app_label = 'disturbance'


# TODO: remove if no longer required
#class ProposalApiarySiteTransfer(models.Model):
#    email = models.EmailField('Email of Transferee', max_length=254, blank=True, null=True)
#    proposal = models.OneToOneField(Proposal, related_name='apiary_site_transfer', null=True)
#    transferee = models.ForeignKey(EmailUser, blank=True, null=True, related_name='transferee')
#
#    def __str__(self):
#        if self.proposal.proposal_apiary:
#            return 'id:{} - {}'.format(self.id, self.proposal.proposal_apiary.title)
#        else:
#            # Should not reach here
#            return 'id:{}'.format(self.id)
#
#    #def __str__(self):
#     #   return '{}'.format(self.title)
#
#    class Meta:
#        app_label = 'disturbance'


class ProposalApiaryDocument(DefaultDocument):
    proposal = models.ForeignKey('Proposal', related_name='apiary_documents')
    _file = models.FileField(upload_to=update_apiary_doc_filename, max_length=512, storage=private_storage)

    def delete(self):
        if self.can_delete:
            return super(ProposalApiaryDocument, self).delete()


class DeedPollDocument(Document):
    DOC_TYPE_NAME = 'deed_poll_documents'

    proposal = models.ForeignKey(ProposalApiary, related_name='deed_poll_documents', blank=True, null=True)
    base_proposal = models.ForeignKey(Proposal, related_name='deed_poll_documents', blank=True, null=True)
    _file = models.FileField(max_length=255, storage=private_storage)
    input_name = models.CharField(max_length=255, blank=True, null=True)
    # after initial submit prevent document from being deleted
    can_delete = models.BooleanField(default=True)
    #version_comment = models.CharField(max_length=255, blank=True, null=True)
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(DeedPollDocument, self).delete()

    class Meta:
        app_label = 'disturbance'


class PublicLiabilityInsuranceDocument(Document):
    DOC_TYPE_NAME = 'public_liability_document'

    proposal = models.ForeignKey(ProposalApiary, related_name='public_liability_insurance_documents', blank=True, null=True)
    _file = models.FileField(max_length=255, storage=private_storage)
    input_name = models.CharField(max_length=255, blank=True, null=True)
    can_delete = models.BooleanField(default=True)
    visible = models.BooleanField(default=True)

    class Meta:
        app_label = 'disturbance'


class TemporaryUsePublicLiabilityInsuranceDocument(Document):
    DOC_TYPE_NAME = 'public_liability_document'

    proposal = models.ForeignKey(ProposalApiaryTemporaryUse, related_name='public_liability_insurance_documents', blank=True, null=True)
    _file = models.FileField(max_length=255, storage=private_storage)
    input_name = models.CharField(max_length=255, blank=True, null=True)
    can_delete = models.BooleanField(default=True)
    visible = models.BooleanField(default=True)

    class Meta:
        app_label = 'disturbance'



class SupportingApplicationDocument(Document):
    DOC_TYPE_NAME = 'supporting_application_document'

    proposal = models.ForeignKey(ProposalApiary, related_name='supporting_application_documents', blank=True, null=True)
    _file = models.FileField(max_length=255, storage=private_storage)
    input_name = models.CharField(max_length=255, blank=True, null=True)
    can_delete = models.BooleanField(default=True)
    visible = models.BooleanField(default=True)

    class Meta:
        app_label = 'disturbance'


def export_file_path(instance, filename):
    return f'{settings.GEO_EXPORT_FOLDER}/{filename}'

class OldFileExportManager(models.Manager):
    def get_queryset(self):
        days_ago = timezone.now() - datetime.timedelta(days=settings.CLEAR_AFTER_DAYS_FILE_EXPORT)
        return super().get_queryset().filter(created__lt=days_ago)


class ExportDocument(models.Model):
    _file = models.FileField(upload_to=export_file_path, max_length=255, storage=private_storage)
    requester = models.ForeignKey(EmailUser, related_name='+')
    created = models.DateTimeField(default=timezone.now, editable=False)
    proposal = models.ForeignKey('Proposal', blank=True, null=True)
  
    objects = models.Manager()
    old_files = OldFileExportManager()

    class Meta:
        app_label = 'disturbance'

    def __str__(self):
        return f'Document {self._file}'

    @property
    def filename(self):
        return os.path.basename(self._file.name)


#class DeedPollDocument(DefaultDocument):
#    proposal = models.ForeignKey('Proposal', related_name='deed_poll_documents')
#    _file = models.FileField(max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(DeedPollDocument, self).delete()


#class ApiaryChecklistQuestion(models.Model):
class ApiaryChecklistQuestion(RevisionedMixin):
    ANSWER_TYPE_CHOICES = (
        ('yes_no', 'Yes/No type'),
        ('free_text','Free text type'),
    )
    CHECKLIST_TYPE_CHOICES = (
        ('apiary', 'Apiary'),
        ('apiary_per_site', 'Apiary per site'),
        ('site_transfer', 'Site Transfer'),
        ('site_transfer_per_site', 'Site Transfer per site'),
    )
    CHECKLIST_ROLE_CHOICES = (
        ('assessor', 'Assessor'),
        ('applicant', 'Applicant'),
        ('referrer', 'Referrer'),
    )
    text = models.TextField()
    answer_type = models.CharField('Answer Type',
                                   max_length=30,
                                   choices=ANSWER_TYPE_CHOICES,
                                   default=ANSWER_TYPE_CHOICES[0][0])
    checklist_type = models.CharField('Checklist Type',
                                   max_length=30,
                                   choices=CHECKLIST_TYPE_CHOICES,
                                   #default=ANSWER_TYPE_CHOICES[0][0]
                                   )
    checklist_role = models.CharField('Checklist Role',
                                   max_length=30,
                                   choices=CHECKLIST_ROLE_CHOICES,
                                   #default=ANSWER_TYPE_CHOICES[0][0]
                                   )
    order = models.PositiveIntegerField(default=1)

    def __str__(self):
        return self.text

    class Meta:
        app_label = 'disturbance'
        ordering = ['order', 'id']


class ApiaryChecklistAnswer(models.Model):
    question=models.ForeignKey(ApiaryChecklistQuestion, related_name='answers')
    answer = models.NullBooleanField()
    proposal = models.ForeignKey(ProposalApiary, related_name="apiary_checklist")
    apiary_referral = models.ForeignKey('ApiaryReferral', related_name="apiary_checklist_referral", blank=True, null=True)
    #text_answer= models.CharField(max_length=256, blank=True, null=True)
    text_answer = models.TextField(blank=True, null=True)
    # to delete
    site=models.ForeignKey(ApiarySiteOnProposal, blank=True, null=True)
    apiary_site=models.ForeignKey(ApiarySite, blank=True, null=True)

    def __str__(self):
        return self.question.text

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'CheckList answer'
        verbose_name_plural = 'CheckList answers'

#class ApiaryTemporaryUseDocument(DefaultDocument):
#    temporary_use = models.ForeignKey('ProposalApiaryTemporaryUse', related_name='apiary_temporary_use_documents')
#    _file = models.FileField(upload_to=update_temporary_use_doc_filename, max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(ApiarySiteLocationDocument, self).delete()
#
#class ApiarySiteTransferDocument(DefaultDocument):
#    site_transfer = models.ForeignKey('ProposalApiarySiteTransfer', related_name='apiary_site_transfer_documents')
#    _file = models.FileField(upload_to=update_site_transfer_doc_filename, max_length=512)
#
#    def delete(self):
#        if self.can_delete:
#            return super(ApiarySiteLocationDocument, self).delete()


class ApiaryAssessorGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        return 'Apiary Assessors Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    class Meta:
        app_label = 'disturbance'
        verbose_name_plural = 'Apiary Assessors Group'

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]


class ApiaryApproverGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        return 'Apiary Approvers Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    class Meta:
        app_label = 'disturbance'
        verbose_name_plural = 'Apiary Approvers Group'

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]



class ApiaryReferral(RevisionedMixin):
    #SENT_CHOICES = (
    #    (1,'Sent From Assessor'),
    #    (2,'Sent From Referral')
    #)
    #PROCESSING_STATUS_CHOICES = (
    #                             ('with_referral', 'Awaiting'),
    #                             ('recalled', 'Recalled'),
    #                             ('completed', 'Completed'),
    #                             )
    #lodged_on = models.DateTimeField(auto_now_add=True)
    #proposal = models.ForeignKey(ProposalApiary,related_name='referrals')
    #sent_by = models.ForeignKey(EmailUser,related_name='disturbance_apiary_assessor_referrals')
    #referral = models.ForeignKey(EmailUser,null=True,blank=True,related_name='disturbance_apiary_referalls')
    #referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='referral_groups')
    #linked = models.BooleanField(default=False)
    #sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    #processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
    #                                     default=PROCESSING_STATUS_CHOICES[0][0])
    #text = models.TextField(blank=True) #Assessor text
    #referral_text = models.TextField(blank=True)
    ## is document required?
    ##document = models.ForeignKey(ReferralDocument, blank=True, null=True, related_name='referral_document')

    referral = models.OneToOneField(Referral, related_name='apiary_referral', null=True)
    referral_group = models.ForeignKey(ApiaryReferralGroup,null=True,blank=True,related_name='referral_groups')
    assigned_officer = models.ForeignKey(EmailUser, blank=True, null=True, related_name='apiary_referrals_assigned', on_delete=models.SET_NULL)

    class Meta:
        app_label = 'disturbance'
        #ordering = ('-lodged_on',)

    def __str__(self):
        return 'Apiary Application {} - Referral {}'.format(
                self.referral.proposal.id,
                self.referral.id
                )

    # Methods
    #@property
    #def latest_referrals(self):
     #   return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[:2]

    #@property
    #def referral_assessment(self):
     #   qs=self.assessment.filter(referral_assessment=True, referral_group=self.referral_group)
      #  if qs:
       #     return qs[0]
        #else:
         #   return None

    #@property
    #def can_be_completed(self):
     #   return True
        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
      #  qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
       # if qs:
        #    return False
        #else:
         #   return True

    def can_assign(self, user):
        if self.referral.processing_status=='with_referral':
            group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
            #user=request.user
            if group and group[0] in user.apiaryreferralgroup_set.all():
                return True
            else:
                return False
        return False

    def can_process(self, user):
        if self.referral.processing_status=='with_referral':
            group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
            #user=request.user
            if group and group[0] in user.apiaryreferralgroup_set.all():
                if not self.assigned_officer or self.assigned_officer == user:
                    return True
            #else:
                #return False
        return False

    def recall(self,request):
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.referral.processing_status = 'recalled'
            self.referral.save()
            # TODO Log proposal action
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_RECALL_REFERRAL.format(
                    self.referral.id,
                    self.referral.proposal.lodgement_number
                    ),
                request
                )
            # TODO log organisation action
            applicant_field=getattr(
                    self.referral.proposal,
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                ProposalUserAction.APIARY_RECALL_REFERRAL.format(
                    self.referral.id,
                    self.referral.proposal.lodgement_number
                    ),
                request
                )

    def remind(self,request):
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_ACTION_REMIND_REFERRAL.format(
                self.referral.id,
                self.referral.proposal.lodgement_number,'{}'.format(self.referral_group.name)
                ),
                request
                )
            # Create a log entry for the organisation
            applicant_field=getattr(
                    self.referral.proposal,
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                ProposalUserAction.APIARY_ACTION_REMIND_REFERRAL.format(
                self.referral.id,
                self.referral.proposal.lodgement_number,'{}'.format(self.referral_group.name)
                ),
                request
                )
            # send email
            recipients = self.referral_group.members_list
            send_apiary_referral_email_notification(self.referral,recipients,request,reminder=True)

    def resend(self,request):
        with transaction.atomic():
            if not self.referral.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.referral.processing_status = 'with_referral'
            self.referral.proposal.processing_status = 'with_referral'
            self.referral.proposal.save()
            self.sent_from = 1
            self.save()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.referral.proposal.log_user_action(
                ProposalUserAction.APIARY_ACTION_RESEND_REFERRAL_TO.format(
                    self.referral.id,
                    self.referral.proposal.lodgement_number,'{}'.format(self.referral_group.name)
                    ),
                request)
            # Create a log entry for the organisation
            #self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            applicant_field=getattr(
                    self.referral.proposal,
                    self.referral.proposal.applicant_field
                    )
            applicant_field.log_user_action(
                    ProposalUserAction.APIARY_ACTION_RESEND_REFERRAL_TO.format(
                        self.referral.id,
                        self.referral.proposal.lodgement_number,
                        '{}'.format(
                            self.referral_group.name)
                        ),
                    request
                    )
            # send email
            recipients = self.referral_group.members_list
            send_apiary_referral_email_notification(self.referral,recipients,request)

    def complete(self,request):
        with transaction.atomic():
            try:
                #if request.user != self.referral:
                group =  ApiaryReferralGroup.objects.filter(id=self.referral_group.id)
                #print u.referralrecipientgroup_set.all()
                user=request.user
                if group and group[0] not in user.apiaryreferralgroup_set.all():
                    raise exceptions.ReferralNotAuthorized()
                self.referral.processing_status = 'completed'
                #self.referral.referral = request.user
                self.referral.referral_text = request.user.get_full_name() + ': ' + request.data.get('referral_comment')
                #self.add_referral_document(request)
                self.referral.save()
                # TODO Log proposal action
                #self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                self.referral.proposal.log_user_action(
                        ProposalUserAction.APIARY_CONCLUDE_REFERRAL.format(
                            request.user.get_full_name(),
                            self.referral.id,
                            self.referral.proposal.lodgement_number,
                            '{}'.format(
                                self.referral_group.name)
                            ),
                        request
                        )
                # TODO log organisation action
                #self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                applicant_field=getattr(
                        self.referral.proposal,
                        self.referral.proposal.applicant_field
                        )
                applicant_field.log_user_action(
                        ProposalUserAction.APIARY_CONCLUDE_REFERRAL.format(
                            request.user.get_full_name(),
                            self.referral.id,
                            self.referral.proposal.lodgement_number,
                            '{}'.format(self.referral_group.name)
                            ),
                        request
                        )
                send_apiary_referral_complete_email_notification(self.referral, request, request.user)
            except:
                raise

    def assign_officer(self,request,officer):
    #def assign_officer(self,request):
        with transaction.atomic():
            try:
                #if not self.can_process(request.user):
                 #   raise exceptions.ProposalNotAuthorized()
                if not self.can_assign(request.user):
                    raise ValidationError('The selected person is not authorised to assign referrals')
                elif request.user != self.assigned_officer:
                    self.assigned_officer = officer
                    self.save()
                    # Create a log entry for the proposal
                    self.referral.proposal.log_user_action(ProposalUserAction.APIARY_REFERRAL_ASSIGN_TO_ASSESSOR.format(
                        self.referral.id,self.referral.proposal.lodgement_number, '{}({})'.format(
                            officer.get_full_name(), officer.email)
                        ), request)
            except:
                raise

    def unassign(self,request):
        with transaction.atomic():
            try:
                #if not self.can_process(request.user):
                 #   raise exceptions.ProposalNotAuthorized()
                if not self.can_assign(request.user):
                    raise ValidationError('The selected person is not authorised to change referral assignments')
                elif self.assigned_officer:
                    self.assigned_officer = None
                    self.save()
                    # Create a log entry for the proposal
                    self.referral.proposal.log_user_action(ProposalUserAction.APIARY_REFERRAL_UNASSIGN_ASSESSOR.format(
                        self.referral.id,self.referral.proposal.lodgement_number), request)
            except:
                raise

    # Properties
    @property
    def region(self):
        return self.referral.proposal.region

    @property
    def activity(self):
        return self.referral.proposal.activity

    @property
    def title(self):
        return self.referral.proposal.title

    # @property
    # def applicant(self):
    #     return self.proposal.applicant.name

    @property
    def applicant(self):
        return self.referral.proposal.applicant

    @property
    def can_be_processed(self):
        return self.referral.processing_status == 'with_referral'

    def can_assess_referral(self,user):
       return self.referral.processing_status == 'with_referral'

    @property
    def allowed_assessors(self):
        group = self.referral_group
        return group.members.all() if group else []

# --------------------------------------------------------------------------------------
# Apiary Models End
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# Generate JSON schema models start
# --------------------------------------------------------------------------------------
@python_2_unicode_compatible
class QuestionOption(models.Model):
    label = models.CharField(max_length=255, unique=True)
    value = models.CharField(max_length=255)

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Schema Question Option'

    def __str__(self):
        return self.label 

from ckeditor.fields import RichTextField
@python_2_unicode_compatible
class MasterlistQuestion(models.Model):
    ANSWER_TYPE_CHECKBOX = 'checkbox'
    ANSWER_TYPE_RADIO = 'radiobuttons'
    ANSWER_TYPE_SELECT = 'select'
    ANSWER_TYPE_MULTI = 'multi-select'

    ANSWER_TYPE_CHOICES=(('text', 'Text'),
                         (ANSWER_TYPE_RADIO, 'Radio button'),
                         (ANSWER_TYPE_CHECKBOX, 'Checkbox'),
                         
                         #('text_info', 'Text Info'),
                         #('iframe', 'IFrame'),
                         ('number','Number'),
                         ('email','Email'),
                         ('select', 'Select'),
                         ('multi-select','Multi-select'),
                         ('text_area','Text area'),
                         ('label', 'Label'),
                         #('section', 'Section'),
                         ('declaration', 'Declaration'),
                         ('file', 'File'),
                         ('date', 'Date'),
                        )
    ANSWER_TYPE_OPTIONS = [
        ANSWER_TYPE_CHECKBOX,
        # ANSWER_TYPE_SELECT,
        # ANSWER_TYPE_MULTI,
        ANSWER_TYPE_RADIO,
    ]
    ANSWER_TYPE_OPTIONS_NEW = [
        ANSWER_TYPE_CHECKBOX,
        ANSWER_TYPE_SELECT,
        ANSWER_TYPE_MULTI,
        ANSWER_TYPE_RADIO,
    ]
    name = models.CharField(max_length=100)
    question = models.TextField()
    #answer_type= models.CharField(max_length=100)
    option = models.ManyToManyField(QuestionOption, blank=True)
    answer_type = models.CharField('Answer Type', max_length=40, choices=ANSWER_TYPE_CHOICES,
                                        default=ANSWER_TYPE_CHOICES[0][0])
    # help_text_url=models.CharField(max_length=400, blank=True, null=True)
    # help_text_assessor_url=models.CharField(max_length=400, blank=True, null=True)
    help_text_url=models.BooleanField(default=False)
    help_text_assessor_url=models.BooleanField(default=False)
    help_text=RichTextField(null=True, blank=True)
    help_text_assessor=RichTextField(null=True, blank=True)
    property_cache = JSONField(null=True, blank=True, default={})

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Schema Masterlist Question'

    def __str__(self):
        return self.question

    def get_options(self):
        '''
        Property field for Question Options.
        '''
        option_list = []
        options = self.get_property_cache_options()
        for o in options:
            qo = QuestionOption(label=o['label'], value=o['value'])
            option_list.append(qo)
        return option_list

    def get_property_cache_options(self):
        '''
        Getter for options on the property cache.

        NOTE: only used for presentation purposes.

        :return options_list of QuestionOption values.
        '''
        options = []
        try:

            options = self.property_cache['options']

        except KeyError:
            pass

        return options

    def set_property_cache_options(self, options):
        '''
        Setter for options on the property cache.

        NOTE: only used for presentation purposes.

        :param  options is QuerySet of QuestionOption or List of option value
                string.
        '''
        class MasterlistOptionEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, list):
                    options = []
                    for o in obj:
                        option = {
                            'label': o['label'],
                            'value': o['value'],
                        }
                        options.append(option)
                    return options

            def encode_list(self, obj, iter=None):
                if isinstance(obj, (list)):
                    return self.default(obj)
                else:
                    return super(
                        MasterlistOptionEncoder, self).encode_list(obj, iter)

        if not isinstance(options, list) and self.id:
            logger.warn('{0} - MasterlistQuestion: {1}'.format(
                'set_property_cache_options() NOT LIST', self.id))
            return

        if self.id:
            data = MasterlistOptionEncoder().encode_list(options)
            self.property_cache['options'] = data

    def get_headers(self):
        '''
        Property field for Question Table Headers.
        '''
        header_list = []
        headers = self.get_property_cache_headers()
        # for h in headers:
        #     qh = QuestionOption(label=h, value='')
        #     header_list.append(qo)
        return headers

    def get_property_cache_headers(self):
        '''
        Getter for headers on the property cache.

        NOTE: only used for presentation purposes.

        :return headers_list of QuestionOption values.
        '''
        headers = []
        try:

            headers = self.property_cache['headers']

        except KeyError:
            pass

        return headers

    def set_property_cache_headers(self, headers):
        '''
        Setter for options on the property cache.

        NOTE: only used for presentation purposes.

        :param  options is QuerySet of MasterlistQuestion or List of ids.
        '''
        class TableHeaderEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, list):
                    headers = []
                    for h in obj:
                        header = {
                            'label': h['label'],
                            'value': h['value'],
                        }
                        headers.append(header)
                    return headers

            def encode_list(self, obj, iter=None):
                if isinstance(obj, (list)):
                    return self.default(obj)
                else:
                    return super(
                        TableHeaderEncoder, self).encode_list(obj, iter)

        if not isinstance(headers, list) and self.id:
            logger.warn('{0} - MasterlistQuestion: {1}'.format(
                'set_property_cache_headers() NOT LIST', self.id))
            return

        if self.id:
            data = TableHeaderEncoder().encode_list(headers)
            self.property_cache['headers'] = data

    def get_expanders(self):
        '''
        Property field for Question Table Expanders.
        '''
        expander_list = []
        expanders = self.get_property_cache_expanders()
        # for h in headers:
        #     qh = QuestionOption(label=h, value='')
        #     header_list.append(qo)
        return expanders

    def get_property_cache_expanders(self):
        '''
        Getter for options on the property cache.

        NOTE: only used for presentation purposes.

        :return options_list of QuestionOption values.
        '''
        expanders = []
        try:

            expanders = self.property_cache['expanders']

        except KeyError:
            pass

        return expanders

    def set_property_cache_expanders(self, expanders):
        '''
        Setter for options on the property cache.

        NOTE: only used for presentation purposes.

        :param  options is QuerySet of QuestionOption or List of option value
                string.
        '''
        class TableExpanderEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, list):
                    expanders = []
                    for e in obj:
                        expander = {
                            'label': e['label'],
                            'value': e['value'],
                        }
                        expanders.append(expander)
                    return expanders

            def encode_list(self, obj, iter=None):
                if isinstance(obj, (list)):
                    return self.default(obj)
                else:
                    return super(
                        TableExpanderEncoder, self).encode_list(obj, iter)

        if not isinstance(expanders, list) and self.id:
            logger.warn('{0} - MasterlistQuestion: {1}'.format(
                'set_property_cache_expanders() NOT LIST', self.id))
            return

        if self.id:
            data = TableExpanderEncoder().encode_list(expanders)
            self.property_cache['expanders'] = data


@python_2_unicode_compatible
class ProposalTypeSection(models.Model):
    section_name = models.CharField(max_length=100)
    section_label = models.CharField(max_length=100)
    index = models.IntegerField(blank=True, default=0)
    proposal_type=models.ForeignKey(ProposalType, related_name='sections', on_delete=models.PROTECT)
    

    class Meta:
        app_label = 'disturbance'
        verbose_name = 'Schema Proposal Type Section'

    def __str__(self):
        return '{} - {}'.format(self.section_label, self.proposal_type)

def limit_sectionquestion_choices_another():
   return {'id__in':MasterlistQuestion.objects.filter(option__isnull=False).distinct('option__label').all().values_list('id', flat=True)}

from django.db import connection
def limit_sectionquestion_choices_sql():
    sql='''
            select m.id from disturbance_masterlistquestion as m 
            INNER JOIN disturbance_masterlistquestion_option as p ON m.id = p.masterlistquestion_id 
            INNER JOIN disturbance_questionoption as o ON o.id = p.questionoption_id
            WHERE o.label IS NOT NULL
    '''

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            row = set([item[0] for item in cursor.fetchall()])
                                
        return dict(id__in=row)
    except:
        return {}

@python_2_unicode_compatible
class SectionQuestion(models.Model):
    TAG_CHOICES=(('isCopiedToPermit', 'isCopiedToPermit'),
                 ('isRequired', 'isRequired'),
                 ('canBeEditedByAssessor', 'canBeEditedByAssessor'),
                 ('isRepeatable', 'isRepeatable'),
                 ('isTitleColumnForDashboard', 'isTitleColumnForDashboard'),
                )
    section=models.ForeignKey(ProposalTypeSection, related_name='section_questions', on_delete=models.PROTECT)
    question=models.ForeignKey(MasterlistQuestion, related_name='question_sections',on_delete=models.PROTECT)
    parent_question = ChainedForeignKey(
        'disturbance.MasterlistQuestion',
        chained_field='section',
        chained_model_field='question_sections__section',
        show_all=False,
        null=True,
        blank=True,
        related_name='children_question',
        #limit_choices_to=Q(option__isnull=False)
        limit_choices_to=limit_sectionquestion_choices_sql(),
        on_delete=models.SET_NULL
    )
    #parent_question=models.ForeignKey('disturbance.MasterlistQuestion', related_name='children_question', null=True, blank=True, on_delete=models.SET_NULL)
    
    #parent_answer=models.ForeignKey(QuestionOption, null=True, blank=True)
    parent_answer = ChainedForeignKey(
        'disturbance.QuestionOption',
        chained_field='parent_question',
        chained_model_field='masterlistquestion',
        show_all=False,
        null=True,
        blank=True,
        related_name='options',
    )
    # parent_question_another = ChainedForeignKey(
    #     'disturbance.MasterlistQuestion',
    #     chained_field='section',
    #     chained_model_field='question_sections__section',
    #     show_all=False,
    #     null=True,
    #     blank=True,
    #     related_name='parentquestionanother',
    #     #limit_choices_to=Q(option__isnull=False)
    #     limit_choices_to=limit_sectionquestion_choices_sql()
    # )
    # parent_answer = ChainedManyToManyField(
    #     'disturbance.QuestionOption',
    #     chained_field='parent_question',
    #     chained_model_field='parent_question',
    # )
    tag= MultiSelectField(choices=TAG_CHOICES, max_length=400,max_choices=10, null=True, blank=True)
    order = models.PositiveIntegerField(default=1)
    property_cache = JSONField(null=True, blank=True, default={})



    class Meta:
        app_label = 'disturbance'
        verbose_name='Schema Section Question'

    def __str__(self):
        return str(self.id)  

    def clean(self):

        if self.question and self.parent_question:
            if self.question==self.parent_question:
                raise ValidationError('Question cannot be linked to itself.')

    @property
    def question_options(self):
        #return self.question.option.all()
        return self.question.get_options()

    def get_options(self):
        '''
        '''
        options = self.get_property_cache_options()

        return options

    def get_property_cache_options(self):
        '''
        Getter for options on the property cache.

        NOTE: only used for presentation purposes.

        :return options_list of QuestionOption values.
        '''
        options = []
        try:

            options = self.property_cache['options']

        except KeyError:
            pass

        return options

    def set_property_cache_options(self, options):
        '''
        Setter for options on the property cache.

        NOTE: only used for presentation purposes.

        :param  options is QuerySet of QuestionOption or List of option value
                string.
        '''
        class QuestionOptionEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, list):
                    options = []
                    for o in obj:
                        # o_conditions = [
                        #     {
                        #         'label': c['label'], 'value': c['value']
                        #     } for c in o['conditions']
                        # ]
                        option = {
                            'label': o['label'],
                            'value': o['value'],
                            #'conditions': o_conditions,
                        }
                        options.append(option)
                    return options

            def encode_list(self, obj, iter=None):
                if isinstance(obj, (list)):
                    return self.default(obj)
                else:
                    return super(
                        QuestionOptionEncoder, self).encode_list(obj, iter)

        if not isinstance(options, list) and self.id:
            logger.warn('{0} - SectionQuestion: {1}'.format(
                'set_property_cache_options() NOT LIST', self.id))
            return

        if self.id:
            data = QuestionOptionEncoder().encode_list(options)
            self.property_cache['options'] = data
    


# --------------------------------------------------------------------------------------
# Generate JSON schema models start
# --------------------------------------------------------------------------------------

#class CurrentSpatialQueryQuestionManager(models.Manager):
#    ''' Return queryset with non-expired SpatialQueryQuestions '''
#    def get_queryset(self):
#        return super().get_queryset().exclude(expiry__lt=datetime.datetime.now().date())

class SpatialQueryQuestion(RevisionedMixin):
                        
    question = models.ForeignKey(MasterlistQuestion, related_name='questions', on_delete=models.PROTECT)
    answer_mlq = models.ForeignKey(QuestionOption, related_name='question_options', on_delete=models.PROTECT, blank=True, null=True)
    group = models.ForeignKey(CddpQuestionGroup, related_name='groups', on_delete=models.CASCADE)
    other_data = JSONField('Additional/Misc Data', blank=True, null=True)
                               
    objects = models.Manager()
#    current_questions = CurrentSpatialQueryQuestionManager()

    class Meta:
        app_label = 'disturbance'
        unique_together = ('question', 'answer_mlq',)
        ordering = ['-id']

    def __str__(self):
        return f'{self.question}'

    def __cddp_group(self):
        try:
            check_group = CddpQuestionGroup.objects.filter(id=self.group.id)
            if check_group:
                return check_group[0]
        except CddpQuestionGroup.DoesNotExist:
            pass
        default_group = CddpQuestionGroup.objects.get(default=True)

        return default_group

    @property
    def layer_name(self):
        return self.spatial_query_layer.layer_name

    @property
    def allowed_editors(self):
        group = self.__cddp_group()
        return group.members.all() if group else []


class CurrentSpatialQueryLayerManager(models.Manager):
    ''' Return queryset with non-expired SpatialQueryLayer's '''
    def get_queryset(self):
        return super().get_queryset().exclude(expiry__lt=datetime.datetime.now().date())


class SpatialQueryLayer(RevisionedMixin):
    OVERLAPPING = 'Overlapping'
    OUTSIDE     = 'Outside'
    INSIDE      = 'Inside'
    HOW_CHOICES=(
        (OVERLAPPING, 'Overlapping'),
        (OUTSIDE, 'Outside'),
        (INSIDE, 'Inside'),
    )
                         
    ALL = 'All'
    REGION_CHOICES=(
        (ALL, 'All'),
    )

    EQUALS      = 'Equals'
    CONTAINS    = 'Contains'
    OR          = 'OR'
    LIKE        = 'Like'
    GREATERTHAN = 'GreaterThan'
    LESSTHAN    = 'LessThan'
    ISNOTNULL   = 'IsNotNull'
    #ISNULL = 'IsNull'
    OPERATOR_CHOICES=(
        (EQUALS, 'Equals'),
        (CONTAINS, 'Contains'),
        (OR, 'OR'),
        (LIKE, 'Like'),
        (GREATERTHAN, 'Greather than'),
        (LESSTHAN, 'Less than'),
    #    (ISNULL, 'Is null'),
        (ISNOTNULL, 'Is not null'),
    )

    NONE  = '' 
    TEXT  = 'Text'
    INT   = 'Int'
    FLOAT = 'Float'
    VALUE_TYPE_CHOICES=(
        (NONE, ''),
        (TEXT, 'Text String'),
        (INT, 'Integer'),
        (FLOAT, 'Float'),
    )
                         
    layer = models.ForeignKey(DASMapLayer, related_name='layers', on_delete=models.CASCADE) #, blank=True, null=True)
    expiry = models.DateField('Expiry Date', blank=True, null=True)
    visible_to_proponent = models.BooleanField(default=False)
    buffer = models.PositiveIntegerField(blank=True, null=True)
    how = models.CharField('Overlapping/Outside/Inside', max_length=40, choices=HOW_CHOICES, default=HOW_CHOICES[0][0])
    column_name = models.CharField('Name of layer attribute/field', max_length=100)
    operator = models.CharField('Operator', max_length=40, choices=OPERATOR_CHOICES, default=OPERATOR_CHOICES[0][0])
    value = models.CharField(max_length=100, blank=True, null=True)

    prefix_answer = models.TextField(blank=True, null=True)
    #no_polygons_proponent = models.IntegerField('No. of polygons to process (Proponent)', default=-1, blank=True)
    answer = models.TextField(blank=True, null=True)
    prefix_info = models.CharField(max_length=100, blank=True, null=True)
    #no_polygons_assessor = models.IntegerField('No. of polygons to process (Assessor)', default=-1, blank=True)
    assessor_info = models.TextField(blank=True, null=True)

    proponent_items = JSONField('Proponent response set', default=[{}])
    assessor_items = JSONField('Assessor response set', default=[{}])

    #regions = models.CharField('Regions', max_length=40, choices=REGION_CHOICES, default=REGION_CHOICES[0][0], blank=True)

    spatial_query_question = models.ForeignKey(SpatialQueryQuestion, related_name='spatial_query_layers', on_delete=models.CASCADE)

    objects = models.Manager()
    current_layers = CurrentSpatialQueryLayerManager()
                                
    class Meta:
        app_label = 'disturbance'
        #ordering = ['-id']

    def __str__(self):
        return f'{self.layer_name}'

    @property
    def layer_name(self):
        return self.layer.layer_name


class SpatialQueryMetrics(models.Model):
                         
    proposal = models.ForeignKey(Proposal, related_name='metrics', on_delete=models.PROTECT)
    when = models.DateTimeField()
    system = models.CharField('Application System Name', max_length=64)
    request_type = models.CharField(max_length=40, choices=RequestTypeEnum.REQUEST_TYPE_CHOICES)
    sqs_response = JSONField('Response from SQS', default=[{}])
    time_taken = models.DecimalField('Total time for request/response', max_digits=9, decimal_places=3)
    response_cached = models.NullBooleanField()

    #when = models.DateTimeField(auto_now_add=True, null=False, blank=False)

    class Meta:
        app_label = 'disturbance'
        ordering = ['-id']

    def __str__(self):
        return f'{self.system}|{self.proposal.lodgement_number}|{self.when.strftime(DATETIME_FMT)}'

    @property
    def metrics(self):
        try:
            return self.sqs_response['metrics']['spatial_query'] 
        except Exception as e:
            logger.error(f'Metrics not found in sqs_response: {e}')
        return None

    @property
    def total_query_time(self):
        ''' Total time taken for all Spatial Query Intersection - sum of each intersection query
        '''
        try:
            return self.sqs_response['metrics']['total_query_time']
        except Exception as e:
            logger.error(f'Total Query Time not found in sqs_response: {e}')
        return None

    def metrics_table(self):
        ''' Tabulated summary of metrics '''
        
        try:
            #print(f"Metric ID|Lodgement Number|Question|Answer|Layer name|Layer cached|Condition|Expired|Error|Time retrieve layer|Time taken")
            print(f"Metric ID|Lodgement Number|Question|Answer|Layer name|Condition|Expired|Error|Time retrieve layer|Time taken")
            count = 0
            for idx, m in enumerate(self.metrics, 1):
                #print(f"{self.id}|{self.proposal.lodgement_number}|{m['question']}|{m['answer_mlq']}|{m['layer_name']}|{m['layer_cached']}|{m['condition']}|{m['expired']}|{m['error']}|{m['time_retrieve_layer']}|{m['time']}")
                print(f"{self.id}|{self.proposal.lodgement_number}|{m['question']}|{m['answer_mlq']}|{m['layer_name']}|{m['condition']}|{m['expired']}|{m['error']}|{m['time_retrieve_layer']}|{m['time']}")
                count += 1

            print(f'Total Query Time:        {self.total_query_time}')
            print(f'Total API Request Time:  {self.time_taken}')
            print(f'Total Responses/Results: {count}')

        except Exception as e:
            logger.error(f'Total Query Time not found in sqs_response: {e}')


#class DASGeometry(models.Model):
#    #proposal = models.ForeignKey(Proposal, unique=True)
#    org = models.CharField(max_length=128)
#    app_no = models.CharField(max_length=9)
#    prop_title = models.CharField(max_length=255)
#    appissdate = models.CharField(max_length=10)
#    appstadate = models.CharField(max_length=10)
#    appexpdate = models.CharField(max_length=10)
#    appstatus = models.CharField(max_length=30)
#    assocprop = models.CharField(max_length=512)
#    proptype = models.CharField(max_length=64)
#    propurl = models.CharField(max_length=512)
#    prop_activ = models.CharField(max_length=255)
#    geometry = GeometryField(srid=4326)
#
#    class Meta:
#        app_label = 'disturbance'
#
#    def __str__(self):
#        return f'{self.app_no}'




import reversion
reversion.register(Proposal, follow=['requirements', 'documents', 'compliances', 'referrals', 'approvals', 'proposal_apiary'])
#reversion.register(Proposal, follow=['proposal_apiary'])
reversion.register(ProposalType)
reversion.register(ProposalRequirement)            # related_name=requirements
reversion.register(ProposalStandardRequirement)    # related_name=proposal_requirements
reversion.register(ProposalDocument)               # related_name=documents
reversion.register(ProposalLogEntry)
reversion.register(ProposalUserAction)
reversion.register(ComplianceRequest)
reversion.register(AmendmentRequest)
reversion.register(Assessment)
reversion.register(Referral)
reversion.register(HelpPage)
reversion.register(ApplicationType)
# reversion.register(ProposalApiary, follow=['apiary_sites'])
reversion.register(ProposalApiary)
reversion.register(ApiaryChecklistQuestion)
reversion.register(ApiarySite)

# added 07-Jan-2021
reversion.register(ProposalApiaryDocument)
reversion.register(DeedPollDocument)
reversion.register(PublicLiabilityInsuranceDocument)
reversion.register(SupportingApplicationDocument)
reversion.register(ApiaryChecklistAnswer)
reversion.register(ApiaryAssessorGroup)
reversion.register(ApiaryApproverGroup)
reversion.register(ApiaryReferral)
reversion.register(ApiaryReferralGroup)
reversion.register(ApiarySiteApproval)
reversion.register(SiteTransferApiarySite)
reversion.register(TemporaryUseApiarySite)
reversion.register(ProposalApiaryTemporaryUse)
reversion.register(OnSiteInformation)
reversion.register(ApiarySiteFeeRemainder)
reversion.register(ApiaryAnnualRentalFeeRunDate)
reversion.register(ApiaryAnnualRentalFeePeriodStartDate)
reversion.register(ApiaryAnnualRentalFee)
reversion.register(ApiarySiteFee)
reversion.register(ApiarySiteFeeType)
reversion.register(SiteCategory)
reversion.register(ApiarySiteOnProposal)

#JSON schema models
reversion.register(MasterlistQuestion)
reversion.register(QuestionOption)
reversion.register(ProposalTypeSection)
reversion.register(SectionQuestion)

#CDDP Spatial model
reversion.register(SpatialQueryQuestion)
reversion.register(SpatialQueryLayer)

reversion.register(ExportDocument)

