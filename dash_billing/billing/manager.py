# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Openstack, LLC.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 NTT
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Billing Service
"""

import os
import logging
import json
import settings
import math
from datetime import datetime

os.environ['DJANGO_SETTINGS_MODULE'] = 'dashboard.settings'

from django.core.management import execute_manager
from django_openstack import api
from django_openstack.middleware.keystone import User

from django.db.models.aggregates import Sum

from openstackx.api import exceptions as api_exceptions
from dash_billing.syspanel.models import  AccountRecord
from dash_billing.syspanel.models import EventLog

from nova import db
from nova import flags
from nova import log as logging
from nova import manager
from nova import rpc
from nova import utils
from nova.compute import instance_types
from nova.scheduler import zone_manager

LOG = logging.getLogger('django_openstack.cron')

#TODO fix this later(nati)
TENANT = '1'
USER = os.environ['NOVA_USERNAME']
PASSWORD = os.environ['NOVA_PASSWORD']

class FakeRequest:
    def __init__(self,user):
        self.user = user

LOG = logging.getLogger('billing.manager')
FLAGS = flags.FLAGS

class PriceList:
    def base_instance_price(self, cpus, memory):
        return cpus*1 + math.floor(memory/(1024*2))

    def active_instance_price(self, cpus, memory):
        return - self.base_instance_price(cpus, memory)

    def create_instance_price(self):
        return - 100

class BillingManager(manager.Manager):
    def __init__(self, *args, **kwargs):
        self.token = api.token_create(None, TENANT, USER, PASSWORD)
        self.user = User(self.token.id,
                USER,
                TENANT,
                True,
                self.token.serviceCatalog
        )
        self.request = FakeRequest(self.user)
        self.price_list = PriceList()


    def periodic_tasks(self, context=None):
        self._add_record_for_active_instance()
        self._check_tenant_all_bill()

    def _add_record_for_active_instance(self):
        now =  datetime.now()
        instances = []
        try:
            instances = api.admin_server_list(self.request)
        except Exception as e:
            LOG.error('Unspecified error in instance index', exc_info=True)
            messages.error(request, 'Unable to get instance list: %s' % e.message)
        for instance in instances:
            if instance.status == 'ACTIVE':
                self._add_record(instance.attrs.tenant_id, self.price_list.active_instance_price(instance.attrs.vcpus, instance.attrs.memory_mb), 'instance %s is running at %s' % (instance.id,now))

    def _check_tenant_all_bill(self):
        tenants = api.tenant_list(self.request)
        LOG.debug("Checking tenant bill")
        #TODO (nati) This code is slow. FIX this later
        for tenant in tenants:
           self._check_tenant_bill(tenant.id)

    def _check_tenant_bill(self,tenant_id):
        balance = AccountRecord.objects.filter(tenant_id=tenant_id).aggregate(Sum('amount'))['amount__sum']
        if not balance:
            balance = 0
        api.admin_api(self.request).quota_sets.update(tenant_id, instances=-int(balance/self.price_list.create_instance_price()))

    def _add_record(self, tenant_id, amount, memo):
        accountRecord = AccountRecord(tenant_id=tenant_id, amount=amount, memo=memo)
        accountRecord.save()
        self._check_tenant_bill(tenant_id)

    def compute_instance_create(self, message):
        self._add_record(message['payload']['project_id'], self.price_list.create_instance_price(), 'create instance')

    def notify(self, message, context=None):
        event_type = message['event_type'].replace('.','_')
        if hasattr(self,event_type):
            method = getattr(self,event_type)
            method(message)
        LOG.debug(json.dumps(message))

        tenant_id = 0
        user_id = 0
        request_id = 0
        try:
            request_id = message['payload']['context']['request_id']
        except:
            pass
        #TODO fix notify decorator
        try:
            tenant_id = message['payload']['project_id']
        except:
            pass

        try:
            tenant_id = message['payload']['context']['project_id']
        except:
            pass

        try:
            user_id = message['payload']['user_id']
        except:
            pass

        try:
            user_id = message['payload']['context']['user_id']
        except:
            pass

        if not tenant_id:
            tenant_id = 0

        if not user_id:
            user_id = 0

        eventlog = EventLog(event_type=message['event_type'],
                            priority=message['priority'],
                            message_id=message['message_id'],
                            publisher_id=message['publisher_id'],
                            message=json.dumps(message['payload']),
                            request_id=request_id,
                            user_id=user_id,
                            tenant_id=tenant_id
                            )
        eventlog.save()
