from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date, timedelta
from django.conf import settings
from disturbance.components.proposals.models import Proposal
from disturbance.components.main.models import GlobalSettings
from disturbance.components.proposals.email import send_assessment_reminder_email_notification
from ledger.accounts.models import EmailUser
import datetime

import itertools

import logging
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Send weekly notification emails for proposals which has not been assessed yet and assessment reminder has sent.'

    def handle(self, *args, **options):
        try:
            user = EmailUser.objects.get(email=settings.CRON_EMAIL)
        except:
            user = EmailUser.objects.create(email=settings.CRON_EMAIL, password = '')

        today = timezone.localtime(timezone.now()).date()
        logger.info('Running command {}'.format(__name__))
        
        #Send weekly reminder after ther first reminder if the Proposal is still pending for assessement    
        weekly_reminder_conditions = {
            'processing_status': 'with_assessor',
            'assessment_reminder_sent': True,
            'assigned_officer__isnull': True,
        }
        assessment_days_record= GlobalSettings.objects.filter(key='assessment_reminder_days')
        if assessment_days_record:
            assessment_days_record=assessment_days_record[0]
            assessment_reminder_days=assessment_days_record.value
            assessment_reminder_days=int(assessment_reminder_days)
        else:
            assessment_reminder_days= settings.ASSESSMENT_REMINDER_DAYS
        qs=Proposal.objects.filter(**weekly_reminder_conditions)
        for proposal in qs:
            compare_date=None
            if proposal.lodgement_date:
                if proposal.weekly_reminder_sent_date:
                    compare_date=proposal.weekly_reminder_sent_date + timedelta(days=7)
                    if compare_date < today:
                        try:
                            send_assessment_reminder_email_notification(proposal)
                            proposal.weekly_reminder_sent_date=today
                            proposal.save()
                            logger.info('sending weekly reminder Proposal {}'.format(proposal.lodgement_number))

                        except Exception as e:
                            logger.info('Error sending weekly reminder Proposal {}\n{}'.format(proposal.lodgement_number, e))

                else: 
                    compare_date=proposal.lodgement_date.date() + timedelta(days=assessment_reminder_days)+ timedelta(days=7)
                    if compare_date < today:
                        try:
                            send_assessment_reminder_email_notification(proposal)
                            proposal.weekly_reminder_sent_date=today
                            proposal.save()
                        except Exception as e:
                            logger.info('Error sending weekly reminder Proposal {}\n{}'.format(proposal.lodgement_number, e))
        logger.info('Command {} completed'.format(__name__))
